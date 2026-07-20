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
    ) -> None:
        self._queue: asyncio.Queue[Request] = asyncio.Queue()
        self._seen: set[str] = set()
        self._allowed_domains = list(allowed_domains or [])
        self._max_depth = max_depth
        self.filtered: int = 0

    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def push(self, request: Request) -> bool:
        """Enqueue request. False if filtered."""
        depth = int(request.meta.get("depth", 0))
        if self._max_depth is not None and depth > self._max_depth:
            self.filtered += 1
            return False

        if self._allowed_domains:
            host = urlsplit(request.url).hostname or ""
            if not _host_allowed(host, self._allowed_domains):
                self.filtered += 1
                return False

        if not request.dont_filter:
            fp = request.fingerprint()
            if fp in self._seen:
                self.filtered += 1
                return False
            self._seen.add(fp)

        self._queue.put_nowait(request)
        return True

    async def pop(self) -> Request:
        return await self._queue.get()
