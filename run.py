#!/usr/bin/env python3
"""Optional multi-spider runner for a local ``spiders/`` package.

Core package is ``zerg`` only. Put site spiders next to this script if you
want a fleet; ``spiders/`` is gitignored by default.

Usage:
    uv run python run.py --list
    uv run python run.py my_spider
    uv run python run.py --all --max-spiders 3
    uv run python run.py --tag rss
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from zerg import crawl_many, discover, jsonl

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description="zerg multi-spider runner")
    ap.add_argument("names", nargs="*", help="Spider names to run")
    ap.add_argument("--all", action="store_true", help="Run all discovered spiders")
    ap.add_argument("--tag", action="append", default=[], help="Filter by spider.tags")
    ap.add_argument("--list", action="store_true", help="List spiders and exit")
    ap.add_argument("--max-spiders", type=int, default=3, help="Parallel spiders")
    ap.add_argument(
        "--report",
        type=Path,
        default=DATA / "run_report.json",
        help="Write aggregate stats JSON",
    )
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    spiders = discover("spiders")

    if args.list or (not args.names and not args.all and not args.tag):
        print(f"Discovered {len(spiders)} spiders:\n")
        for name in sorted(spiders):
            cls = spiders[name]
            tags = ",".join(getattr(cls, "tags", []) or [])
            print(f"  {name:24s}  tags={tags}")
        return 0

    selected: list = []
    if args.all:
        selected = list(spiders.values())
    elif args.tag:
        tagset = set(args.tag)
        for cls in spiders.values():
            if tagset.intersection(getattr(cls, "tags", []) or []):
                selected.append(cls)
    if args.names:
        for n in args.names:
            if n not in spiders:
                print(f"Unknown spider: {n}", file=sys.stderr)
                return 1
            selected.append(spiders[n])

    seen: set[str] = set()
    uniq = []
    for cls in selected:
        if cls.name not in seen:
            seen.add(cls.name)
            uniq.append(cls)
    selected = uniq

    if not selected:
        print("No spiders selected.")
        return 1

    print(f"Running {len(selected)} spiders (max_spiders={args.max_spiders})...")

    def pipes():
        return [jsonl(mode="w")]

    results = asyncio.run(
        crawl_many(
            selected,
            pipelines_factory=pipes,
            data_dir=DATA,
            max_spiders=args.max_spiders,
        )
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    for name, stats in results.items():
        marker = "x" if stats.get("exception") else "+"
        print(
            f"[{marker}] {name}: items={stats.get('items', 0)} "
            f"req={stats.get('requests', 0)} err={stats.get('errors', 0)} "
            f"({stats.get('duration_s', 0)}s)"
        )

    ok = sum(
        1 for s in results.values() if s.get("items", 0) > 0 and not s.get("exception")
    )
    total_items = sum(s.get("items", 0) for s in results.values())
    total_err = sum(s.get("errors", 0) for s in results.values())
    print(
        f"\n=== summary: spiders_ok={ok}/{len(results)} "
        f"items={total_items} errors={total_err} → {args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
