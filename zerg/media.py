"""Optional media download pipeline (byte-budgeted)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from zerg.http import Fetch
from zerg.store import (
    DEFAULT_MAX_MEDIA_FILE_BYTES,
    DEFAULT_MAX_MEDIA_TOTAL_BYTES,
    dir_size,
    human_bytes,
)
from zerg.util import slug as _slug

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
}


def sniff_ext(url: str, content_type: str | None = None) -> str:
    """Guess file extension from URL or Content-Type."""
    ext = Path(url.split("?", 1)[0]).suffix.lower()
    if ext and len(ext) <= 5:
        return ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(ctype, ".bin")


def normalize_media_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


class MediaPipeline:
    """Download URLs from item fields into ``data/<spider>/images/``.

    Resource guards (defaults are conservative):
    - ``max_files`` per item
    - ``max_file_bytes`` skip/truncate oversized bodies
    - ``max_total_bytes`` stop downloading once spider media tree is full
    - ``urls_only=True`` never hits disk — only keeps remote URLs on the item
    """

    def __init__(
        self,
        field: str = "images",
        subdir: str = "images",
        name_field: str = "title",
        concurrency: int = 5,
        timeout: float = 60.0,
        max_files: int | None = 20,
        max_file_bytes: int | None = DEFAULT_MAX_MEDIA_FILE_BYTES,
        max_total_bytes: int | None = DEFAULT_MAX_MEDIA_TOTAL_BYTES,
        urls_only: bool = False,
        fetcher: Any | None = None,
    ):
        self.field = field
        self.subdir = subdir
        self.name_field = name_field
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.urls_only = urls_only
        self._external = fetcher
        self._fetch: Any | None = None
        self._own_fetch = False
        self._root: Path | None = None
        self._sem: asyncio.Semaphore | None = None
        self._written = 0
        self._skipped_budget = 0

    async def open(self, spider: Any) -> None:
        base = getattr(spider, "data_dir", None) or Path("data") / spider.name
        self._root = Path(base) / self.subdir
        if not self.urls_only:
            self._root.mkdir(parents=True, exist_ok=True)
            self._written = dir_size(self._root)
        self._sem = asyncio.Semaphore(max(1, self.concurrency))

        if self.urls_only:
            return

        if self._external is not None:
            self._fetch = self._external
            self._own_fetch = False
            return

        headers = dict(getattr(spider, "headers", {}) or {})
        proxy = getattr(spider, "proxy", None)
        self._fetch = Fetch(
            concurrency=self.concurrency,
            timeout=self.timeout,
            headers=headers,
            proxy=proxy,
        )
        await self._fetch.__aenter__()
        self._own_fetch = True

    def _over_budget(self) -> bool:
        if self.max_total_bytes is None:
            return False
        return self._written >= self.max_total_bytes

    async def process_item(
        self, item: dict[str, Any], spider: Any
    ) -> dict[str, Any]:
        urls = item.get(self.field) or []
        if not urls:
            return item

        if self.max_files is not None:
            urls = list(urls)[: self.max_files]

        # URLs only — zero disk, zero extra HTTP
        if self.urls_only:
            item = dict(item)
            item["files"] = []
            item["files_count"] = 0
            item["image_urls"] = [
                normalize_media_url(u) for u in urls if isinstance(u, str) and u
            ]
            return item

        if self._fetch is None or self._root is None:
            return item

        if self._over_budget():
            self._skipped_budget += 1
            item = dict(item)
            item["files"] = []
            item["files_count"] = 0
            item["media_skipped"] = "budget"
            return item

        name = _slug(str(item.get(self.name_field, "item")))
        folder = self._root / name
        folder.mkdir(parents=True, exist_ok=True)
        assert self._sem is not None

        async def _one(i: int, url: str) -> str | None:
            if not isinstance(url, str) or not url:
                return None
            if self._over_budget():
                return None
            url = normalize_media_url(url)
            async with self._sem:  # type: ignore[union-attr]
                resp = await self._fetch.get(url)  # type: ignore[union-attr]
            if resp is None or resp.status >= 400:
                print(f"  [media] ✗ {name} [{i}]")
                return None
            body = resp.content
            if (
                self.max_file_bytes is not None
                and len(body) > self.max_file_bytes
            ):
                print(
                    f"  [media] skip large {name} [{i}] "
                    f"{human_bytes(len(body))} > "
                    f"{human_bytes(self.max_file_bytes)}"
                )
                return None
            if self.max_total_bytes is not None and (
                self._written + len(body) > self.max_total_bytes
            ):
                self._skipped_budget += 1
                return None
            ext = sniff_ext(url, resp.header("content-type"))
            path = folder / f"{i:03d}{ext}"
            await asyncio.to_thread(path.write_bytes, body)
            self._written += len(body)
            return str(path)

        results = await asyncio.gather(
            *[_one(i, u) for i, u in enumerate(urls, 1)],
            return_exceptions=True,
        )
        saved = [r for r in results if isinstance(r, str)]

        item = dict(item)
        item["files"] = saved
        item["files_count"] = len(saved)
        return item

    async def close(self, spider: Any) -> None:
        if self._skipped_budget:
            print(
                f"  [media] budget stop: wrote {human_bytes(self._written)} "
                f"skipped_items≈{self._skipped_budget}"
            )
        if self._own_fetch and self._fetch is not None:
            await self._fetch.__aexit__(None, None, None)
        self._fetch = None
        self._own_fetch = False


def media(
    field: str = "images",
    subdir: str = "images",
    name_field: str = "title",
    concurrency: int = 5,
    max_files: int | None = 20,
    max_file_bytes: int | None = DEFAULT_MAX_MEDIA_FILE_BYTES,
    max_total_bytes: int | None = DEFAULT_MAX_MEDIA_TOTAL_BYTES,
    urls_only: bool = False,
    fetcher: Any | None = None,
) -> MediaPipeline:
    """Build a MediaPipeline. Prefer ``urls_only=True`` when disk is tight."""
    return MediaPipeline(
        field=field,
        subdir=subdir,
        name_field=name_field,
        concurrency=concurrency,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        urls_only=urls_only,
        fetcher=fetcher,
    )
