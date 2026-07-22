"""Deterministic scheduler, parser, and engine microbenchmarks."""

from __future__ import annotations

import asyncio
from pathlib import Path

from zerg import Request, Response, Spider, crawl
from zerg.parser import Parser
from zerg.scheduler import Scheduler


def test_scheduler_admission(benchmark):
    def run():
        scheduler = Scheduler()
        for index in range(10_000):
            scheduler.push(Request(f"https://bench.local/{index}"))
        return scheduler.seen_count

    assert benchmark(run) == 10_000


def test_parser_extract(benchmark):
    html = "".join(
        f'<div class="item"><h2>item {i}</h2><a href="/{i}">link</a></div>'
        for i in range(1_000)
    )

    def run():
        return Parser(html).extract_all(".item", {"title": "h2", "url": ("a", "href")})

    assert len(benchmark(run)) == 1_000


def test_engine_throughput(benchmark, tmp_path: Path):
    total = 5_000

    class MemoryFetch:
        async def fetch(self, request):
            return Response.from_http(request, request.url, 200, {}, b"{}")

    class BenchSpider(Spider):
        name = "engine_bench"
        concurrency = 100
        max_pending_requests = 256

        async def start(self):
            for index in range(total):
                yield Request(f"https://bench.local/{index}")

        async def parse(self, response):
            yield {"url": response.url}

    def run():
        return asyncio.run(crawl(BenchSpider, fetcher=MemoryFetch(), data_dir=tmp_path))

    stats = benchmark.pedantic(run, rounds=3, iterations=1)
    assert stats["requests"] == total
    assert stats["queue_peak"] <= 256


def test_bounded_fanout(benchmark, tmp_path: Path):
    fanout = 1_000

    class MemoryFetch:
        async def fetch(self, request):
            return Response.from_http(request, request.url, 200, {}, b"{}")

    class FanoutSpider(Spider):
        name = "fanout_bench"
        start_urls = ["https://bench.local/root"]
        concurrency = 8
        max_pending_requests = 32

        async def parse(self, response):
            if response.url.endswith("/root"):
                for index in range(fanout):
                    yield Request(f"https://bench.local/{index}")
            else:
                yield {"url": response.url}

    def run():
        return asyncio.run(
            crawl(FanoutSpider, fetcher=MemoryFetch(), data_dir=tmp_path)
        )

    stats = benchmark.pedantic(run, rounds=3, iterations=1)
    assert stats["requests"] + stats["queue_rejected"] == fanout + 1
    assert stats["queue_peak"] <= 32
