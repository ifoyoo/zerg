"""HTTP backends: httpx and optional curl_cffi."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from zerg.models import DownloadError, Request, Response

DEFAULT_UA = "zerg/0.2 (+https://github.com/ifoyoo/zerg)"
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
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


@dataclass(slots=True)
class StreamResponse:
    url: str
    status: int
    headers: dict[str, str]
    chunks: AsyncIterator[bytes]

    def header(self, name: str, default: str | None = None) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return default


@runtime_checkable
class StreamingFetcher(Protocol):
    """Optional capability used by streaming pipelines."""

    def stream(self, request: Request) -> Any: ...


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


def _content_length(headers: Any) -> int | None:
    value = headers.get("content-length") if headers else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _read_limited(
    response: Any,
    request: Request,
    limit: int | None,
    attempts: int,
) -> bytes:
    declared = _content_length(response.headers)
    if limit is not None and declared is not None and declared > limit:
        raise DownloadError(
            request,
            kind="response_too_large",
            attempts=attempts,
            limit=limit,
            received=declared,
        )
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if limit is not None and len(body) > limit:
            raise DownloadError(
                request,
                kind="response_too_large",
                attempts=attempts,
                limit=limit,
                received=len(body),
            )
    return bytes(body)


class Fetch:
    """httpx client with pooling, HTTP/2, retries, and body limits."""

    def __init__(
        self,
        concurrency: int = 50,
        timeout: float = 30.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        max_response_bytes: int | None = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.max_response_bytes = max_response_bytes
        self._headers = headers or {}
        self._proxy = proxy
        self._client: Any = None

    async def __aenter__(self) -> Fetch:
        import httpx

        n = self.concurrency
        limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
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

    async def fetch(self, request: Request) -> Response:
        if self._client is None:
            raise RuntimeError("Use `async with Fetch() as f:` context manager")

        import httpx

        headers = _merge_headers(self._headers, request)
        last = self.max_retries - 1
        for attempt in range(self.max_retries):
            attempts = attempt + 1
            try:
                async with self._client.stream(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                ) as raw:
                    if raw.status_code in RETRY_STATUSES and attempt < last:
                        await _backoff(attempt, raw.headers.get("retry-after"))
                        continue
                    content = await _read_limited(
                        raw, request, self.max_response_bytes, attempts
                    )
                    return Response.from_http(
                        request=request,
                        url=str(raw.url),
                        status=raw.status_code,
                        headers=dict(raw.headers),
                        content=content,
                        header_encoding=raw.encoding,
                        attempts=attempts,
                    )
            except DownloadError:
                raise
            except httpx.TimeoutException as exc:
                if attempt == last:
                    raise DownloadError(
                        request,
                        kind="timeout",
                        attempts=attempts,
                        cause=exc,
                    ) from exc
                await _backoff(attempt)
            except httpx.RequestError as exc:
                if attempt == last:
                    raise DownloadError(
                        request,
                        kind="network",
                        attempts=attempts,
                        cause=exc,
                    ) from exc
                await asyncio.sleep(1.0)
        raise AssertionError("unreachable")

    async def get(self, url: str, **kwargs: Any) -> Response:
        headers = kwargs.pop("headers", {})
        return await self.fetch(Request(url, headers=headers))

    @asynccontextmanager
    async def stream(self, request: Request):
        """Yield response metadata and decoded chunks with status retries."""
        if self._client is None:
            raise RuntimeError("Use `async with Fetch() as f:` context manager")
        import httpx

        headers = _merge_headers(self._headers, request)
        last = self.max_retries - 1
        for attempt in range(self.max_retries):
            attempts = attempt + 1
            try:
                async with self._client.stream(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                ) as raw:
                    if raw.status_code in RETRY_STATUSES and attempt < last:
                        await _backoff(attempt, raw.headers.get("retry-after"))
                        continue
                    try:
                        yield StreamResponse(
                            url=str(raw.url),
                            status=raw.status_code,
                            headers=dict(raw.headers),
                            chunks=raw.aiter_bytes(),
                        )
                    except httpx.TimeoutException as exc:
                        raise DownloadError(
                            request,
                            kind="timeout",
                            attempts=attempts,
                            cause=exc,
                        ) from exc
                    except httpx.RequestError as exc:
                        raise DownloadError(
                            request,
                            kind="network",
                            attempts=attempts,
                            cause=exc,
                        ) from exc
                    return
            except DownloadError:
                raise
            except httpx.TimeoutException as exc:
                if attempt == last:
                    raise DownloadError(
                        request,
                        kind="timeout",
                        attempts=attempts,
                        cause=exc,
                    ) from exc
                await _backoff(attempt)
            except httpx.RequestError as exc:
                if attempt == last:
                    raise DownloadError(
                        request,
                        kind="network",
                        attempts=attempts,
                        cause=exc,
                    ) from exc
                await asyncio.sleep(1.0)
        raise AssertionError("unreachable")


class ImpersonateFetch:
    """curl_cffi fetcher with browser TLS fingerprints."""

    def __init__(
        self,
        concurrency: int = 20,
        timeout: float = 30.0,
        max_retries: int = 3,
        impersonate: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        max_response_bytes: int | None = DEFAULT_MAX_RESPONSE_BYTES,
        verify: bool = True,
    ):
        try:
            from curl_cffi.requests import AsyncSession  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "curl_cffi is required. Install with: uv sync --extra impersonate"
            ) from exc
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.max_response_bytes = max_response_bytes
        self._impersonate = impersonate
        self._headers = headers or {}
        self._proxy = proxy
        self._verify = verify
        self._session: Any = None
        self._browser: str = ""

    async def __aenter__(self) -> ImpersonateFetch:
        from curl_cffi.requests import AsyncSession

        self._browser = self._impersonate or random.choice(IMPERSONATE_TARGETS)
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "impersonate": self._browser,
            "verify": self._verify,
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._session = AsyncSession(**kwargs)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, request: Request) -> Response:
        if self._session is None:
            raise RuntimeError(
                "Use `async with ImpersonateFetch() as f:` context manager"
            )

        from curl_cffi.requests.errors import RequestException

        headers = _merge_headers(self._headers, request)
        headers.setdefault("accept-language", "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7")
        last = self.max_retries - 1
        for attempt in range(self.max_retries):
            attempts = attempt + 1
            try:
                raw = await self._session.request(
                    request.method,
                    request.url,
                    headers=headers,
                    data=request.body,
                )
                status = int(raw.status_code)
                if status in RETRY_STATUSES and attempt < last:
                    await _backoff(attempt, raw.headers.get("retry-after"))
                    continue
                hdrs = {str(k): str(v) for k, v in (raw.headers or {}).items()}
                content = raw.content
                if isinstance(content, str):
                    content = content.encode("utf-8", errors="replace")
                if (
                    self.max_response_bytes is not None
                    and len(content) > self.max_response_bytes
                ):
                    raise DownloadError(
                        request,
                        kind="response_too_large",
                        attempts=attempts,
                        limit=self.max_response_bytes,
                        received=len(content),
                    )
                return Response.from_http(
                    request=request,
                    url=str(raw.url),
                    status=status,
                    headers=hdrs,
                    content=content,
                    header_encoding=None,
                    attempts=attempts,
                )
            except DownloadError:
                raise
            except asyncio.CancelledError:
                raise
            except RequestException as exc:
                if attempt == last:
                    kind = "timeout" if "timeout" in str(exc).lower() else "network"
                    raise DownloadError(
                        request,
                        kind=kind,
                        attempts=attempts,
                        cause=exc,
                    ) from exc
                await _backoff(attempt)
        raise AssertionError("unreachable")

    async def get(self, url: str, **kwargs: Any) -> Response:
        headers = kwargs.pop("headers", {})
        return await self.fetch(Request(url, headers=headers))

    @property
    def browser(self) -> str:
        return self._browser
