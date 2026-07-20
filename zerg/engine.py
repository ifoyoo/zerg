"""Crawl engine."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from zerg.http import Fetch
from zerg.log import zlog
from zerg.models import (
    REASON_CALLBACK,
    REASON_DOWNLOAD,
    REASON_ERRBACK,
    REASON_HTTP,
    REASON_PARSE,
    REASON_YIELD,
    Callback,
    Failure,
    Request,
    Stats,
)
from zerg.pipeline import Pipeline
from zerg.scheduler import Scheduler
from zerg.spider import Spider


async def _iterate_results(result: Any) -> AsyncIterator[Any]:
    """Turn callback output into an async stream."""
    if result is None:
        return

    if hasattr(result, "__anext__"):
        async for item in result:
            yield item
        return

    if asyncio.iscoroutine(result):
        result = await result
        if result is None:
            return

    if isinstance(result, (Request, dict)):
        yield result
        return

    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, dict)):
        for item in result:
            yield item


def _resolve_callback(spider: Spider, cb: Callback) -> Callable[..., Any] | None:
    """Resolve callback name or callable."""
    if callable(cb):
        return cb
    if isinstance(cb, str):
        return getattr(spider, cb, None)
    return None


def _build_fetcher(spider: Spider) -> Any:
    """Build Fetch from spider settings."""
    headers = dict(spider.headers or {})
    n = max(1, spider.concurrency)

    if spider.use_impersonate:
        from zerg.http import ImpersonateFetch

        return ImpersonateFetch(
            concurrency=n,
            timeout=spider.timeout,
            max_retries=spider.max_retries,
            impersonate=spider.impersonate,
            headers=headers,
            proxy=spider.proxy,
        )

    return Fetch(
        concurrency=n,
        timeout=spider.timeout,
        max_retries=spider.max_retries,
        headers=headers,
        proxy=spider.proxy,
    )


class Engine:
    """Run one spider to completion."""

    def __init__(
        self,
        spider: Spider,
        *,
        pipelines: list[Any] | None = None,
        fetcher: Any | None = None,
        data_dir: str | Path | None = None,
    ):
        self.spider = spider
        self._pipeline = Pipeline(*(pipelines or []))
        self._fetcher = fetcher
        self._data_dir = Path(data_dir) if data_dir else None

    async def run(self) -> dict[str, Any]:
        spider = self.spider
        spider.data_dir = self._data_dir or Path("data") / spider.name
        spider.data_dir.mkdir(parents=True, exist_ok=True)

        own_fetcher = self._fetcher is None
        fetcher = self._fetcher or _build_fetcher(spider)

        if own_fetcher:
            async with fetcher:
                return await self._crawl(spider, fetcher)
        return await self._crawl(spider, fetcher)

    async def _crawl(self, spider: Spider, fetcher: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        stats = Stats(spider=spider.name, data_dir=str(spider.data_dir))
        scheduler = Scheduler(
            allowed_domains=spider.allowed_domains,
            max_depth=spider.max_depth,
        )

        pending = 0
        condition = asyncio.Condition()

        async def enqueue(req: Request) -> None:
            nonlocal pending
            if not scheduler.push(req):
                return
            async with condition:
                pending += 1

        async def handle_yields(raw: Any, *, source: str) -> None:
            async for result in _iterate_results(raw):
                if isinstance(result, Request):
                    await enqueue(result)
                elif isinstance(result, dict):
                    out = await self._pipeline.process(result, spider)
                    if out is not None:
                        stats.items += 1
                else:
                    stats.errors += 1
                    stats.bump(REASON_YIELD)
                    zlog(
                        spider.name,
                        "ignore yield type %s from %s",
                        type(result).__name__,
                        source,
                    )

        async def dispatch_failure(failure: Failure) -> None:
            stats.errors += 1
            stats.bump(failure.reason)
            fn = _resolve_callback(spider, failure.request.errback)
            if fn is None:
                fn = getattr(spider, "errback", None)
            if fn is None:
                zlog(spider.name, "fail %s", failure)
                return
            try:
                raw = fn(failure)
                await handle_yields(raw, source="errback")
            except Exception as e:
                stats.errors += 1
                stats.bump(REASON_ERRBACK)
                zlog(spider.name, "errback error %s: %s", failure.url, e)

        async def worker() -> None:
            nonlocal pending
            while True:
                req = await scheduler.pop()
                try:
                    if spider.delay:
                        await asyncio.sleep(spider.delay)

                    req = spider.prepare_request(req)
                    stats.requests += 1

                    resp = await fetcher.fetch(req)
                    if resp is None:
                        await dispatch_failure(
                            Failure(request=req, reason=REASON_DOWNLOAD)
                        )
                        continue
                    if resp.status >= 400:
                        await dispatch_failure(
                            Failure(
                                request=req,
                                reason=REASON_HTTP,
                                status=resp.status,
                                response=resp,
                            )
                        )
                        continue

                    callback = _resolve_callback(spider, req.callback)
                    if callback is None:
                        await dispatch_failure(
                            Failure(
                                request=req,
                                reason=REASON_CALLBACK,
                                status=resp.status,
                                response=resp,
                                exception=AttributeError(
                                    f"missing callback {req.callback!r}"
                                ),
                            )
                        )
                        continue

                    try:
                        raw = callback(resp)
                        source = (
                            req.callback
                            if isinstance(req.callback, str)
                            else getattr(req.callback, "__name__", "callback")
                        )
                        await handle_yields(raw, source=str(source))
                    except Exception as e:
                        await dispatch_failure(
                            Failure(
                                request=req,
                                reason=REASON_PARSE,
                                status=resp.status,
                                response=resp,
                                exception=e,
                            )
                        )
                finally:
                    async with condition:
                        pending -= 1
                        condition.notify_all()

        await self._pipeline.open(spider)

        n_workers = max(1, spider.concurrency)
        workers = [asyncio.create_task(worker()) for _ in range(n_workers)]
        try:
            async for req in spider.start():
                await enqueue(req)

            async with condition:
                while pending > 0:
                    await condition.wait()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await self._pipeline.close(spider)

        stats.filtered = scheduler.filtered
        stats.duration_s = round(time.perf_counter() - t0, 3)
        return stats.as_dict()


async def crawl(
    spider: Spider | type[Spider],
    *,
    pipelines: list[Any] | None = None,
    fetcher: Any | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run a spider and return stats."""
    if isinstance(spider, type):
        spider = spider()
    engine = Engine(
        spider,
        pipelines=pipelines,
        fetcher=fetcher,
        data_dir=data_dir,
    )
    return await engine.run()


async def crawl_many(
    spiders: Sequence[Spider | type[Spider]],
    *,
    pipelines_factory: Any | None = None,
    pipelines: list[Any] | None = None,
    data_dir: str | Path | None = None,
    max_spiders: int = 3,
) -> dict[str, dict[str, Any]]:
    """Run spiders with bounded parallelism. Failures stay isolated."""
    sem = asyncio.Semaphore(max(1, max_spiders))
    results: dict[str, dict[str, Any]] = {}

    async def _one(spec: Spider | type[Spider]) -> None:
        spider = spec() if isinstance(spec, type) else spec
        name = spider.name
        async with sem:
            try:
                if pipelines_factory is not None:
                    pipes = list(pipelines_factory())
                elif pipelines is not None:
                    pipes = list(pipelines)
                else:
                    pipes = []
                dd = Path(data_dir) / name if data_dir else None
                st = await crawl(spider, pipelines=pipes, data_dir=dd)
                results[name] = st
                reasons = st.get("by_reason") or {}
                reason_s = f" reasons={reasons}" if reasons else ""
                print(
                    f"[异虫] ✓ {name}: items={st['items']} "
                    f"req={st['requests']} err={st['errors']} "
                    f"({st['duration_s']}s){reason_s}"
                )
            except Exception as e:
                results[name] = Stats(spider=name, errors=1).as_dict() | {
                    "exception": repr(e)
                }
                print(f"[异虫] ✗ {name}: {e}")

    await asyncio.gather(*[_one(s) for s in spiders])
    return results
