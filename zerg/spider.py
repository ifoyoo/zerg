"""Spider base class."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

from zerg.log import zlog
from zerg.models import Failure, Request, Response


class Spider:
    """One site, one spider.

    Implement ``parse`` (and extra callbacks). Yield ``Request`` or ``dict``.
    Mutable class defaults are copied in ``__init__``.
    """

    name: str = "spider"
    start_urls: list[str] = []
    concurrency: int = 10
    delay: float = 0.0  # legacy alias: 1 / delay requests per second
    requests_per_second: float | None = None
    burst: int = 1
    max_pending_requests: int = 1000
    headers: dict[str, str] = {}
    proxy: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    max_response_bytes: int | None = 10 * 1024 * 1024
    allowed_domains: list[str] = []
    max_depth: int | None = None
    use_impersonate: bool = False
    impersonate: str | None = None
    # HTTP statuses that still invoke callback (not errback), e.g. jsl 521
    challenge_statuses: list[int] = []
    # error_rate above this → stats["healthy"]=False (None disables check)
    health_error_rate: float | None = 0.5
    tags: ClassVar[list[str]] = []

    data_dir: Path  # set by Engine

    def __init__(self) -> None:
        self.start_urls = list(type(self).start_urls)
        self.headers = dict(type(self).headers)
        self.allowed_domains = list(type(self).allowed_domains)
        self.challenge_statuses = list(type(self).challenge_statuses)

    def prepare_request(self, request: Request) -> Request:
        """Merge spider headers onto the request."""
        if self.headers:
            request.headers = {**self.headers, **request.headers}
        return request

    async def start(self) -> AsyncIterator[Request]:
        """Yield seed requests."""
        for url in self.start_urls:
            yield Request(url)

    async def parse(
        self, response: Response
    ) -> AsyncIterator[Request | dict[str, Any]]:
        """Default callback."""
        raise NotImplementedError(f"{type(self).__name__}.parse() not implemented")

    async def errback(
        self, failure: Failure
    ) -> AsyncIterator[Request | dict[str, Any]] | None:
        """Handle fetch/callback failure. May yield Request or dict."""
        zlog(self.name, "fail %s", failure)
        return None
