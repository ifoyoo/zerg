"""Backpressure, rate limiting, and transfer-safety tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from zerg import Failure, Request, Response, Spider, crawl
from zerg.http import Fetch, StreamResponse
from zerg.media import MediaPipeline
from zerg.models import REASON_DOWNLOAD, DownloadError
from zerg.rate import RateLimiter
from zerg.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_bounded_enqueue_rolls_back_on_cancel():
    scheduler = Scheduler(max_pending=1)
    assert scheduler.push(Request("https://ex.com/one"))
    pending = asyncio.create_task(scheduler.enqueue(Request("https://ex.com/two")))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    first = await scheduler.pop()
    assert first.url.endswith("/one")
    scheduler.task_done()
    assert scheduler.push(Request("https://ex.com/two"))
    assert scheduler.queue_peak == 1
    assert not scheduler.push(Request("https://ex.com/three"))
    assert scheduler.rejected == 1


@pytest.mark.asyncio
async def test_rate_limiter_spaces_shared_workers():
    starts: list[float] = []
    limiter = RateLimiter(100.0, burst=1)

    async def acquire():
        await limiter.acquire()
        starts.append(asyncio.get_running_loop().time())

    await asyncio.gather(*(acquire() for _ in range(3)))
    assert starts[1] - starts[0] >= 0.008
    assert starts[2] - starts[1] >= 0.008


@pytest.mark.asyncio
async def test_fetch_enforces_content_length_limit():
    request = Request("https://ex.com/large")
    fetch = Fetch(max_response_bytes=4)
    fetch._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-length": "10"}, content=b"0123456789"
            )
        )
    )
    try:
        with pytest.raises(DownloadError) as caught:
            await fetch.fetch(request)
    finally:
        await fetch.__aexit__()
    assert caught.value.kind == "response_too_large"
    assert caught.value.limit == 4


@pytest.mark.asyncio
async def test_fetch_reports_retry_attempts(monkeypatch):
    calls = 0

    async def no_sleep(*args):
        return None

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, content=b"ok")

    monkeypatch.setattr("zerg.http._backoff", no_sleep)
    fetch = Fetch(max_retries=2)
    fetch._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await fetch.fetch(Request("https://ex.com/retry"))
    finally:
        await fetch.__aexit__()
    assert response.status == 200
    assert response.attempts == 2
    assert response.retries == 1
    assert response.bytes_received == 2


@pytest.mark.asyncio
async def test_retry_status_does_not_buffer_body(monkeypatch):
    consumed = False
    calls = 0

    class ExplodingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal consumed
            consumed = True
            raise AssertionError("retry body must not be consumed")
            yield b""  # pragma: no cover

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, stream=ExplodingBody())
        return httpx.Response(200, content=b"ok")

    async def no_sleep(*args):
        return None

    monkeypatch.setattr("zerg.http._backoff", no_sleep)
    fetch = Fetch(max_retries=2, max_response_bytes=4)
    fetch._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await fetch.fetch(Request("https://ex.com/retry-large"))
    finally:
        await fetch.__aexit__()
    assert response.status == 200
    assert not consumed


@pytest.mark.asyncio
async def test_fetch_enforces_streamed_limit_without_header():
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    fetch = Fetch(max_response_bytes=5)
    fetch._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Body()))
    )
    try:
        with pytest.raises(DownloadError) as caught:
            await fetch.fetch(Request("https://ex.com/chunked"))
    finally:
        await fetch.__aexit__()
    assert caught.value.kind == "response_too_large"
    assert caught.value.received == 6


@pytest.mark.asyncio
async def test_engine_fanout_stays_bounded(tmp_path: Path):
    class MemoryFetch:
        async def fetch(self, request):
            return Response.from_http(request, request.url, 200, {}, b"{}")

    class Fanout(Spider):
        name = "fanout"
        start_urls = ["https://ex.com/root"]
        concurrency = 1
        max_pending_requests = 2

        async def parse(self, response):
            if response.url.endswith("/root"):
                for index in range(100):
                    yield Request(f"https://ex.com/{index}")
            else:
                yield {"url": response.url}

    stats = await asyncio.wait_for(
        crawl(Fanout, fetcher=MemoryFetch(), data_dir=tmp_path), timeout=2
    )
    assert stats["requests"] + stats["queue_rejected"] == 101
    assert stats["items"] + stats["queue_rejected"] == 100
    assert stats["queue_rejected"] > 0
    assert stats["queue_peak"] <= 2
    assert stats["healthy"] is False


@pytest.mark.asyncio
async def test_engine_deep_chain_does_not_recurse(tmp_path: Path):
    depth = 2_000

    class MemoryFetch:
        async def fetch(self, request):
            return Response.from_http(request, request.url, 200, {}, b"{}")

    class Chain(Spider):
        name = "chain"
        start_urls = ["https://ex.com/0"]
        concurrency = 1
        max_pending_requests = 1

        async def parse(self, response):
            index = int(response.url.rsplit("/", 1)[-1])
            if index < depth:
                yield Request(f"https://ex.com/{index + 1}")
            else:
                yield {"depth": index}

    stats = await asyncio.wait_for(
        crawl(Chain, fetcher=MemoryFetch(), data_dir=tmp_path), timeout=5
    )
    assert stats["requests"] == depth + 1
    assert stats["items"] == 1
    assert stats["queue_peak"] <= 1


@pytest.mark.asyncio
async def test_engine_preserves_download_error(tmp_path: Path):
    failures: list[Failure] = []

    class BrokenFetch:
        async def fetch(self, request):
            raise DownloadError(
                request,
                kind="timeout",
                attempts=3,
                cause=TimeoutError("slow"),
            )

    class S(Spider):
        name = "broken"
        start_urls = ["https://ex.com/"]

        async def errback(self, failure):
            failures.append(failure)

    stats = await crawl(S, fetcher=BrokenFetch(), data_dir=tmp_path)
    assert stats["errors"] == 1
    assert stats["retries"] == 2
    assert stats["timeouts"] == 1
    assert stats["by_reason"][REASON_DOWNLOAD] == 1
    assert isinstance(failures[0].exception, DownloadError)


class StreamFetch:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies

    @asynccontextmanager
    async def stream(self, request):
        body = self.bodies[request.url]

        async def chunks():
            for start in range(0, len(body), 3):
                await asyncio.sleep(0)
                yield body[start : start + 3]

        yield StreamResponse(
            request.url,
            200,
            {"content-type": "image/jpeg"},
            chunks(),
        )


@pytest.mark.asyncio
async def test_media_streams_atomically_with_total_budget(tmp_path: Path):
    fetch = StreamFetch(
        {"https://ex.com/a.jpg": b"a" * 8, "https://ex.com/b.jpg": b"b" * 8}
    )
    spider = type("S", (), {"name": "media", "data_dir": tmp_path})()
    pipeline = MediaPipeline(
        fetcher=fetch,
        concurrency=2,
        max_file_bytes=10,
        max_total_bytes=10,
    )
    await pipeline.open(spider)
    item = await pipeline.process_item(
        {
            "title": "item",
            "images": ["https://ex.com/a.jpg", "https://ex.com/b.jpg"],
        },
        spider,
    )
    await pipeline.close(spider)

    assert item["files_count"] == 1
    assert sum(path.stat().st_size for path in tmp_path.rglob("*.jpg")) == 8
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_media_isolates_stream_transport_error(tmp_path: Path):
    class BrokenStream:
        @asynccontextmanager
        async def stream(self, request):
            raise DownloadError(request, kind="network")
            yield  # pragma: no cover

    spider = type("S", (), {"name": "media", "data_dir": tmp_path})()
    pipeline = MediaPipeline(fetcher=BrokenStream())
    await pipeline.open(spider)
    item = await pipeline.process_item(
        {"title": "item", "images": ["https://ex.com/a.jpg"]}, spider
    )
    await pipeline.close(spider)
    assert item["files_count"] == 0


@pytest.mark.asyncio
async def test_media_removes_partial_oversize_file(tmp_path: Path):
    fetch = StreamFetch({"https://ex.com/a.jpg": b"a" * 12})
    spider = type("S", (), {"name": "media", "data_dir": tmp_path})()
    pipeline = MediaPipeline(
        fetcher=fetch,
        max_file_bytes=5,
        max_total_bytes=100,
    )
    await pipeline.open(spider)
    item = await pipeline.process_item(
        {"title": "item", "images": ["https://ex.com/a.jpg"]}, spider
    )
    await pipeline.close(spider)
    assert item["files_count"] == 0
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.jpg"))
