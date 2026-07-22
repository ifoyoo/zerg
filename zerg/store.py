"""Disk / retention helpers — keep multi-site crawls from filling the box.

Defaults bias toward *not* hoarding:
- media is opt-in and byte-capped
- jsonl can cap item count
- ``gc()`` prunes old spider dirs, media, and evo signals
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── defaults (global safety net) ─────────────────────────────────────

DEFAULT_MAX_JSONL_ITEMS = 50_000
DEFAULT_MAX_MEDIA_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB / file
DEFAULT_MAX_MEDIA_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MiB / spider media tree
DEFAULT_MAX_SPIDER_DIR_BYTES = 500 * 1024 * 1024  # 500 MiB / spider
DEFAULT_MAX_DATA_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB / data root
DEFAULT_EVO_SIGNAL_LINES = 2000
DEFAULT_KEEP_SPIDERS = None  # None = keep all names; int = newest N by mtime
DEFAULT_MAX_AGE_DAYS = None  # None = no age prune


def dir_size(path: Path) -> int:
    """Total size of files under path (bytes). Missing → 0."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(x) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(x)}{unit}"
            return f"{x:.1f}{unit}"
        x /= 1024
    return f"{n}B"


@dataclass
class Usage:
    path: str
    bytes: int
    files: int = 0
    children: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["human"] = human_bytes(self.bytes)
        d["children_human"] = {k: human_bytes(v) for k, v in self.children.items()}
        return d


def usage(root: str | Path = "data") -> Usage:
    """Summarize disk use under data root."""
    root = Path(root)
    children: dict[str, int] = {}
    files = 0
    if root.exists():
        for child in sorted(root.iterdir()):
            if child.name.startswith("."):
                continue
            children[child.name] = dir_size(child)
            if child.is_file():
                files += 1
            else:
                files += sum(1 for p in child.rglob("*") if p.is_file())
    total = sum(children.values()) if children else dir_size(root)
    return Usage(path=str(root), bytes=total, files=files, children=children)


@dataclass
class GCPolicy:
    """What ``gc`` is allowed to delete."""

    max_age_days: float | None = DEFAULT_MAX_AGE_DAYS
    keep_spiders: int | None = DEFAULT_KEEP_SPIDERS
    max_data_bytes: int | None = DEFAULT_MAX_DATA_BYTES
    max_spider_bytes: int | None = DEFAULT_MAX_SPIDER_DIR_BYTES
    drop_media: bool = False
    media_only: bool = False  # only purge media/ subdirs
    evo_signal_lines: int = DEFAULT_EVO_SIGNAL_LINES
    dry_run: bool = False
    protect: tuple[str, ...] = ("evo",)  # never delete these top-level names entirely


@dataclass
class GCResult:
    freed: int = 0
    removed: list[str] = field(default_factory=list)
    trimmed: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "freed": self.freed,
            "freed_human": human_bytes(self.freed),
            "removed": self.removed,
            "trimmed": self.trimmed,
            "dry_run": self.dry_run,
        }


def _rm(path: Path, result: GCResult) -> None:
    size = dir_size(path)
    if result.dry_run:
        result.removed.append(f"{path} ({human_bytes(size)})")
        result.freed += size
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)
    result.removed.append(str(path))
    result.freed += size


def _trim_jsonl(path: Path, keep_lines: int, result: GCResult) -> None:
    if not path.exists() or keep_lines <= 0:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.splitlines()
    if len(lines) <= keep_lines:
        return
    drop = len(lines) - keep_lines
    # approximate freed: avg line size
    old = path.stat().st_size
    kept = "\n".join(lines[-keep_lines:]) + "\n"
    if result.dry_run:
        result.trimmed.append(f"{path} drop={drop} lines")
        result.freed += max(0, old - len(kept.encode()))
        return
    path.write_text(kept, encoding="utf-8")
    new = path.stat().st_size
    result.trimmed.append(f"{path} drop={drop} lines")
    result.freed += max(0, old - new)


def gc(
    root: str | Path = "data",
    policy: GCPolicy | None = None,
) -> GCResult:
    """Prune data root according to policy. Safe by default (dry_run=False deletes)."""
    root = Path(root)
    pol = policy or GCPolicy()
    result = GCResult(dry_run=pol.dry_run)
    if not root.exists():
        return result

    now = time.time()
    protect = set(pol.protect)

    # 1) media-only purge or drop_media under each spider
    if pol.drop_media or pol.media_only:
        for child in list(root.iterdir()):
            if not child.is_dir() or child.name in protect:
                continue
            for media_dir in child.rglob("images"):
                if media_dir.is_dir():
                    _rm(media_dir, result)
            # also common alt name
            for media_dir in child.rglob("media"):
                if media_dir.is_dir():
                    _rm(media_dir, result)
        if pol.media_only:
            _trim_jsonl(
                root / "evo" / "signals.jsonl", pol.evo_signal_lines, result
            )
            return result

    # 2) age-based spider dir prune
    if pol.max_age_days is not None:
        cutoff = now - float(pol.max_age_days) * 86400
        for child in list(root.iterdir()):
            if not child.is_dir() or child.name in protect:
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                _rm(child, result)

    # 3) keep only newest N spider dirs
    if pol.keep_spiders is not None:
        dirs = [
            c
            for c in root.iterdir()
            if c.is_dir() and c.name not in protect
        ]
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in dirs[pol.keep_spiders :]:
            _rm(old, result)

    # 4) per-spider size cap — delete largest media first, then whole dir if still over
    if pol.max_spider_bytes is not None:
        for child in list(root.iterdir()):
            if not child.is_dir() or child.name in protect:
                continue
            if not child.exists():
                continue
            size = dir_size(child)
            if size <= pol.max_spider_bytes:
                continue
            # prefer stripping images/
            for sub in ("images", "media"):
                p = child / sub
                if p.exists():
                    _rm(p, result)
            size = dir_size(child)
            if size > pol.max_spider_bytes:
                _rm(child, result)

    # 5) total data cap — remove oldest non-protected dirs until under budget
    if pol.max_data_bytes is not None:
        while dir_size(root) > pol.max_data_bytes:
            dirs = [
                c
                for c in root.iterdir()
                if c.is_dir() and c.name not in protect and c.exists()
            ]
            if not dirs:
                break
            dirs.sort(key=lambda p: p.stat().st_mtime)  # oldest first
            _rm(dirs[0], result)

    # 6) evo signals rotation
    _trim_jsonl(root / "evo" / "signals.jsonl", pol.evo_signal_lines, result)

    return result


def print_usage(root: str | Path = "data") -> Usage:
    u = usage(root)
    print(f"[异虫.store] {u.path}: {human_bytes(u.bytes)} in {u.files} files")
    for name, n in sorted(u.children.items(), key=lambda kv: -kv[1]):
        print(f"  {name:20s} {human_bytes(n)}")
    return u


# ── pipeline: cap jsonl rows ─────────────────────────────────────────


class CapItems:
    """Pipeline that drops items after ``max_items`` (still counts as processed None)."""

    def __init__(self, max_items: int = DEFAULT_MAX_JSONL_ITEMS):
        self.max_items = max(0, max_items)
        self._n = 0
        self.dropped = 0

    async def process_item(
        self, item: dict[str, Any], spider: Any
    ) -> dict[str, Any] | None:
        if self.max_items and self._n >= self.max_items:
            self.dropped += 1
            return None
        self._n += 1
        return item


def cap_items(max_items: int = DEFAULT_MAX_JSONL_ITEMS) -> CapItems:
    return CapItems(max_items=max_items)


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="zerg storage / gc")
    ap.add_argument(
        "--root", type=Path, default=Path("data"), help="data root"
    )
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("usage", help="show disk usage")

    p_gc = sub.add_parser("gc", help="prune data root")
    p_gc.add_argument("--dry-run", action="store_true")
    p_gc.add_argument("--media-only", action="store_true")
    p_gc.add_argument("--drop-media", action="store_true")
    p_gc.add_argument("--max-age-days", type=float, default=None)
    p_gc.add_argument("--keep-spiders", type=int, default=None)
    p_gc.add_argument(
        "--max-data-gb",
        type=float,
        default=None,
        help="cap total data size in GiB",
    )
    p_gc.add_argument(
        "--max-spider-mb",
        type=float,
        default=None,
        help="cap each spider dir in MiB",
    )
    p_gc.add_argument(
        "--evo-lines",
        type=int,
        default=DEFAULT_EVO_SIGNAL_LINES,
        help="keep last N evo signal lines",
    )

    args = ap.parse_args(argv)
    cmd = args.cmd or "usage"

    if cmd == "usage":
        print_usage(args.root)
        return 0

    if cmd == "gc":
        max_data = (
            int(args.max_data_gb * 1024**3)
            if args.max_data_gb is not None
            else DEFAULT_MAX_DATA_BYTES
        )
        max_spider = (
            int(args.max_spider_mb * 1024**2)
            if args.max_spider_mb is not None
            else DEFAULT_MAX_SPIDER_DIR_BYTES
        )
        pol = GCPolicy(
            max_age_days=args.max_age_days,
            keep_spiders=args.keep_spiders,
            max_data_bytes=max_data,
            max_spider_bytes=max_spider,
            drop_media=args.drop_media,
            media_only=args.media_only,
            evo_signal_lines=args.evo_lines,
            dry_run=args.dry_run,
        )
        before = usage(args.root)
        result = gc(args.root, pol)
        after = usage(args.root)
        print(
            f"[异虫.store] gc dry_run={result.dry_run} "
            f"freed≈{human_bytes(result.freed)} "
            f"{human_bytes(before.bytes)} → {human_bytes(after.bytes)}"
        )
        for line in result.removed[:30]:
            print(f"  - {line}")
        for line in result.trimmed[:10]:
            print(f"  ~ {line}")
        if args.dry_run:
            print("  (dry-run: nothing deleted)")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
