"""HTTP backends: httpx and optional curl_cffi."""

from __future__ import annotations

import asyncio
import random
from typing import Any, Protocol, runtime_checkable

from zerg.models import Request, Response

DEFAULT_UA = "zerg/0.1 (+https://github.com/ifoyoo/zerg)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
IMPERSONATE_TARGETS = (
    "chrome124",
    "chrome123",
    "chrome120",
    "safari17_0",
    "firefox116",
    "edge101",
)


@runtime_checkable
class Fetcher(Protocol):
    """Download backend."""

    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, *args: Any) -> Any: ...
    async def fetch(self, request: Request) -> Response | None: ...


def _merge_headers(
    base: dict[str, str], request: Request, *, default_ua: bool = True
) -> dict[str, str]:
    headers = {**base, **request.headers}
    if default_ua and not any(k.lower() == "user-agent" for k in headers):
        headers["user-agent"] = DEFAULT_UA
    return headers


async def _backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after and retry_after.isdigit():
        await asyncio.sleep(min(int(retry_after), 30))
    else:
        await asyncio.sleep(2**attempt * 0.5)


class Fetch:
    """httpx client with pooling, HTTP/2, and retries.

    ``concurrency`` sizes the connection pool only.
    """

    def __init__(
        self,
        concurrency: int = 50,
        timeout: float = 30.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ):
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._headers = headers or {}
        self._proxy = proxy
        self._client: Any = None

    async def __aenter__(self) -> Fetch:
        import httpx

        n = self.concurrency
        limits = httpx.Limits(
            max_connections=n,
            max_keepalive_connections=n,
        )
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout),
            "limits": limits,
            "http2": True,
            "follow_redirects": True,
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, request: Request) -> Response | None:
        if self._client is None:
            raise RuntimeError("Use `async with Fetch() as f:` context manager")

        import httpx

        headers = _merge_headers(self._headers, request)
        last = self.max_retries - 1

        for attempt in range(self.max_retries):
            try:
                resp = await self._client.request(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                )
                if resp.status_code in RETRY_STATUSES and attempt < last:
                    await _backoff(attempt, resp.headers.get("retry-after"))
                    continue
                return Response.from_http(
                    request=request,
                    url=str(resp.url),
                    status=resp.status_code,
                    headers=dict(resp.headers),
                    content=resp.content,
                    header_encoding=resp.encoding,
                )
            except httpx.TimeoutException:
                if attempt == last:
                    return None
                await _backoff(attempt)
            except httpx.RequestError:
                if attempt == last:
                    return None
                await asyncio.sleep(1.0)
        return None

    async def get(self, url: str, **kwargs: Any) -> Response | None:
        headers = kwargs.pop("headers", {})
        return await self.fetch(Request(url, headers=headers))


class ImpersonateFetch:
    """curl_cffi fetcher with browser TLS fingerprints.

    Requires: ``uv sync --extra impersonate``.
    """

    def __init__(
        self,
        concurrency: int = 20,
        timeout: float = 30.0,
        max_retries: int = 3,
        impersonate: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ):
        try:
            from curl_cffi.requests import AsyncSession  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "curl_cffi is required. Install with: "
                "uv sync --extra impersonate"
            ) from e
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._impersonate = impersonate
        self._headers = headers or {}
        self._proxy = proxy
        self._session: Any = None
        self._browser: str = ""

    async def __aenter__(self) -> ImpersonateFetch:
        from curl_cffi.requests import AsyncSession

        self._browser = self._impersonate or random.choice(IMPERSONATE_TARGETS)
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "impersonate": self._browser,
            "verify": False,
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._session = AsyncSession(**kwargs)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, request: Request) -> Response | None:
        if self._session is None:
            raise RuntimeError(
                "Use `async with ImpersonateFetch() as f:` context manager"
            )

        headers = _merge_headers(self._headers, request)
        headers.setdefault(
            "accept-language", "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"
        )
        last = self.max_retries - 1

        for attempt in range(self.max_retries):
            try:
                resp = await self._session.request(
                    request.method,
                    request.url,
                    headers=headers,
                    data=request.body,
                )
                status = int(resp.status_code)
                if status in RETRY_STATUSES and attempt < last:
                    await _backoff(attempt)
                    continue

                raw_headers = getattr(resp, "headers", {}) or {}
                try:
                    hdrs = dict(raw_headers)
                except Exception:
                    hdrs = {str(k): str(raw_headers[k]) for k in raw_headers}

                content = resp.content
                if isinstance(content, str):
                    content = content.encode("utf-8", errors="replace")

                return Response.from_http(
                    request=request,
                    url=str(resp.url),
                    status=status,
                    headers=hdrs,
                    content=content,
                    header_encoding=None,
                )
            except Exception:
                if attempt == last:
                    return None
                await _backoff(attempt)
        return None

    async def get(self, url: str, **kwargs: Any) -> Response | None:
        headers = kwargs.pop("headers", {})
        return await self.fetch(Request(url, headers=headers))

    @property
    def browser(self) -> str:
        return self._browser
