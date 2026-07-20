"""Optional media download pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from zerg.http import Fetch
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
    """Download URLs from item fields into ``data/<spider>/images/``."""

    def __init__(
        self,
        field: str = "images",
        subdir: str = "images",
        name_field: str = "title",
        concurrency: int = 5,
        timeout: float = 60.0,
        max_files: int | None = 20,
        fetcher: Any | None = None,
    ):
        self.field = field
        self.subdir = subdir
        self.name_field = name_field
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_files = max_files
        self._external = fetcher
        self._fetch: Any | None = None
        self._own_fetch = False
        self._root: Path | None = None
        self._sem: asyncio.Semaphore | None = None

    async def open(self, spider: Any) -> None:
        base = getattr(spider, "data_dir", None) or Path("data") / spider.name
        self._root = Path(base) / self.subdir
        self._root.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(max(1, self.concurrency))

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

    async def process_item(
        self, item: dict[str, Any], spider: Any
    ) -> dict[str, Any]:
        urls = item.get(self.field) or []
        if not urls or self._fetch is None or self._root is None:
            return item

        if self.max_files is not None:
            urls = list(urls)[: self.max_files]

        name = _slug(str(item.get(self.name_field, "item")))
        folder = self._root / name
        folder.mkdir(parents=True, exist_ok=True)
        assert self._sem is not None

        async def _one(i: int, url: str) -> str | None:
            if not isinstance(url, str) or not url:
                return None
            url = normalize_media_url(url)
            async with self._sem:  # type: ignore[union-attr]
                resp = await self._fetch.get(url)  # type: ignore[union-attr]
            if resp is None or resp.status >= 400:
                print(f"  [media] ✗ {name} [{i}]")
                return None
            ext = sniff_ext(url, resp.header("content-type"))
            path = folder / f"{i:03d}{ext}"
            await asyncio.to_thread(path.write_bytes, resp.content)
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
    fetcher: Any | None = None,
) -> MediaPipeline:
    """Build a MediaPipeline."""
    return MediaPipeline(
        field=field,
        subdir=subdir,
        name_field=name_field,
        concurrency=concurrency,
        max_files=max_files,
        fetcher=fetcher,
    )
