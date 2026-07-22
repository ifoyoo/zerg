"""In-memory request queue."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from zerg.models import Request


def _host_allowed(host: str, allowed_domains: list[str]) -> bool:
    """Host matches allowed domain or its subdomain."""
    host = host.lower().rstrip(".")
    for domain in allowed_domains:
        d = domain.lower().lstrip(".").rstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


class Scheduler:
    """FIFO queue with fingerprint / domain / depth filters."""

    def __init__(
        self,
        *,
        allowed_domains: list[str] | None = None,
        max_depth: int | None = None,
        max_pending: int = 0,
    ) -> None:
        self._queue: asyncio.Queue[Request] = asyncio.Queue(maxsize=max(0, max_pending))
        self._seen: set[str] = set()
        self._allowed_domains = list(allowed_domains or [])
        self._max_depth = max_depth
        self.filtered: int = 0
        self.rejected: int = 0
        self.queue_peak: int = 0

    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def _admit(self, request: Request) -> str | None:
        depth = int(request.meta.get("depth", 0))
        if self._max_depth is not None and depth > self._max_depth:
            self.filtered += 1
            return None

        if self._allowed_domains:
            host = urlsplit(request.url).hostname or ""
            if not _host_allowed(host, self._allowed_domains):
                self.filtered += 1
                return None

        if request.dont_filter:
            return ""
        fingerprint = request.fingerprint()
        if fingerprint in self._seen:
            self.filtered += 1
            return None
        self._seen.add(fingerprint)
        return fingerprint

    def _rollback(self, fingerprint: str | None) -> None:
        if fingerprint:
            self._seen.discard(fingerprint)

    def _note_peak(self) -> None:
        self.queue_peak = max(self.queue_peak, self._queue.qsize())

    def push(self, request: Request) -> bool:
        """Enqueue immediately; reject safely when the frontier is full."""
        fingerprint = self._admit(request)
        if fingerprint is None:
            return False
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            self._rollback(fingerprint)
            self.rejected += 1
            return False
        except BaseException:
            self._rollback(fingerprint)
            raise
        self._note_peak()
        return True

    async def enqueue(self, request: Request) -> bool:
        """Enqueue with backpressure while preserving dedup atomicity."""
        fingerprint = self._admit(request)
        if fingerprint is None:
            return False
        try:
            await self._queue.put(request)
        except BaseException:
            self._rollback(fingerprint)
            raise
        self._note_peak()
        return True

    async def pop(self) -> Request:
        return await self._queue.get()

    def task_done(self) -> None:
        """Mark the current request complete for ``join()`` coordination."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until every accepted request has completed."""
        await self._queue.join()
