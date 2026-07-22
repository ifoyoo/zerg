"""Optional media download pipeline with streaming byte budgets."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from zerg.http import Fetch
from zerg.models import DownloadError, Request
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
    """Stream item media into ``data/<spider>/images/`` with hard budgets."""

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
        self._budget_lock: asyncio.Lock | None = None
        self._written = 0
        self._reserved = 0
        self._skipped_budget = 0

    async def open(self, spider: Any) -> None:
        base = getattr(spider, "data_dir", None) or Path("data") / spider.name
        self._root = Path(base) / self.subdir
        if not self.urls_only:
            self._root.mkdir(parents=True, exist_ok=True)
            self._written = dir_size(self._root)
        self._sem = asyncio.Semaphore(max(1, self.concurrency))
        self._budget_lock = asyncio.Lock()
        self._reserved = 0

        if self.urls_only:
            return
        if self._external is not None:
            self._fetch = self._external
            return

        headers = dict(getattr(spider, "headers", {}) or {})
        proxy = getattr(spider, "proxy", None)
        self._fetch = Fetch(
            concurrency=self.concurrency,
            timeout=self.timeout,
            headers=headers,
            proxy=proxy,
            max_response_bytes=self.max_file_bytes,
        )
        await self._fetch.__aenter__()
        self._own_fetch = True

    def _over_budget(self) -> bool:
        return (
            self.max_total_bytes is not None
            and self._written + self._reserved >= self.max_total_bytes
        )

    async def _reserve(self, amount: int) -> bool:
        assert self._budget_lock is not None
        async with self._budget_lock:
            if self.max_total_bytes is not None and (
                self._written + self._reserved + amount > self.max_total_bytes
            ):
                self._skipped_budget += 1
                return False
            self._reserved += amount
            return True

    async def _release(self, amount: int, *, commit: bool = False) -> None:
        assert self._budget_lock is not None
        async with self._budget_lock:
            self._reserved = max(0, self._reserved - amount)
            if commit:
                self._written += amount

    async def _commit_file(self, temp: Path, path: Path, amount: int) -> None:
        await asyncio.to_thread(os.replace, temp, path)
        await self._release(amount, commit=True)

    async def _write_and_commit(self, temp: Path, path: Path, body: bytes) -> None:
        await asyncio.to_thread(temp.write_bytes, body)
        await self._commit_file(temp, path, len(body))

    async def _shield_transaction(self, transaction: Any) -> None:
        task = asyncio.create_task(transaction)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _stream_to_file(self, url: str, request: Request, path: Path) -> bool:
        assert self._fetch is not None
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        received = 0
        committed = False
        try:
            async with self._fetch.stream(request) as response:
                if response.status >= 400:
                    return False
                declared_raw = response.header("content-length")
                try:
                    declared = int(declared_raw) if declared_raw else None
                except ValueError:
                    declared = None
                if (
                    self.max_file_bytes is not None
                    and declared is not None
                    and declared > self.max_file_bytes
                ):
                    return False
                path = path.with_suffix(sniff_ext(url, response.header("content-type")))
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
                with temp.open("wb") as output:
                    async for chunk in response.chunks:
                        if self.max_file_bytes is not None and (
                            received + len(chunk) > self.max_file_bytes
                        ):
                            return False
                        if not await self._reserve(len(chunk)):
                            return False
                        received += len(chunk)
                        output.write(chunk)
            await self._shield_transaction(self._commit_file(temp, path, received))
            committed = True
            return True
        except asyncio.CancelledError:
            # The shielded commit completed before cancellation was re-raised.
            committed = True
            raise
        except (DownloadError, OSError):
            return False
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
            if received and not committed:
                await self._release(received)

    async def _buffered_to_file(self, url: str, request: Request, path: Path) -> bool:
        assert self._fetch is not None
        try:
            response = await self._fetch.fetch(request)
        except DownloadError:
            return False
        if response is None or response.status >= 400:
            return False
        body = response.content
        if self.max_file_bytes is not None and len(body) > self.max_file_bytes:
            return False
        if not await self._reserve(len(body)):
            return False
        path = path.with_suffix(sniff_ext(url, response.header("content-type")))
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            await self._shield_transaction(self._write_and_commit(temp, path, body))
            return True
        except OSError:
            await self._release(len(body))
            return False
        finally:
            temp.unlink(missing_ok=True)

    async def process_item(self, item: dict[str, Any], spider: Any) -> dict[str, Any]:
        urls = item.get(self.field) or []
        if isinstance(urls, str):
            urls = [urls]
        if self.max_files is not None:
            urls = list(urls)[: self.max_files]

        valid_urls = [
            normalize_media_url(url) for url in urls if isinstance(url, str) and url
        ]
        if not valid_urls:
            return item
        if self.urls_only:
            return {
                **item,
                "files": [],
                "files_count": 0,
                "image_urls": valid_urls,
            }
        if self._fetch is None or self._root is None or self._over_budget():
            self._skipped_budget += 1
            return {
                **item,
                "files": [],
                "files_count": 0,
                "media_skipped": "budget",
            }

        name = _slug(str(item.get(self.name_field, "item")))
        folder = self._root / name
        folder.mkdir(parents=True, exist_ok=True)
        assert self._sem is not None

        async def download(index: int, url: str) -> str | None:
            digest = hashlib.sha1(url.encode()).hexdigest()[:8]
            unique = uuid.uuid4().hex[:6]
            stem = f"{index:03d}-{digest}-{unique}"
            base_path = folder / f"{stem}.bin"
            request = Request(url)
            async with self._sem:
                if callable(getattr(self._fetch, "stream", None)):
                    saved = await self._stream_to_file(url, request, base_path)
                else:
                    saved = await self._buffered_to_file(url, request, base_path)
            if not saved:
                return None
            matches = list(folder.glob(f"{stem}.*"))
            return str(matches[0]) if matches else None

        results = await asyncio.gather(
            *(download(i, url) for i, url in enumerate(valid_urls, 1))
        )
        saved = [result for result in results if isinstance(result, str)]
        return {**item, "files": saved, "files_count": len(saved)}

    async def close(self, spider: Any) -> None:
        if self._skipped_budget:
            print(
                f"  [media] budget stop: wrote {human_bytes(self._written)} "
                f"skipped_items~{self._skipped_budget}"
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
    *,
    timeout: float = 60.0,
) -> MediaPipeline:
    return MediaPipeline(
        field=field,
        subdir=subdir,
        name_field=name_field,
        concurrency=concurrency,
        timeout=timeout,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        urls_only=urls_only,
        fetcher=fetcher,
    )
