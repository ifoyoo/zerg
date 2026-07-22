"""Crawl engine."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
    DownloadError,
    Failure,
    Request,
    Response,
    Stats,
)
from zerg.pipeline import Pipeline
from zerg.rate import RateLimiter
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
            max_response_bytes=spider.max_response_bytes,
        )

    return Fetch(
        concurrency=n,
        timeout=spider.timeout,
        max_retries=spider.max_retries,
        headers=headers,
        proxy=spider.proxy,
        max_response_bytes=spider.max_response_bytes,
    )


@runtime_checkable
class CrawlObserver(Protocol):
    """Optional crawl event sink for metrics, tracing, or progress reporting."""

    def on_start(self, spider: Spider) -> None: ...

    def on_response(self, response: Response) -> None: ...

    def on_failure(self, failure: Failure) -> None: ...

    def on_item(self, item: dict[str, Any]) -> None: ...

    def on_request(self, request: Request) -> None: ...

    def on_finish(self, spider: Spider, stats: dict[str, Any]) -> None: ...


def _call_observers(observers: Sequence[Any], method: str, *args: Any) -> None:
    for obs in observers:
        fn = getattr(obs, method, None)
        if fn is None:
            continue
        try:
            fn(*args)
        except Exception as e:
            name = type(obs).__name__
            zlog("engine", "observer %s.%s failed: %s", name, method, e)


class Engine:
    """Run one spider to completion."""

    def __init__(
        self,
        spider: Spider,
        *,
        pipelines: list[Any] | None = None,
        fetcher: Any | None = None,
        data_dir: str | Path | None = None,
        observers: Sequence[CrawlObserver] | None = None,
        on_finish: Callable[[Spider, dict[str, Any]], None] | None = None,
    ):
        self.spider = spider
        self._pipeline = Pipeline(*(pipelines or []))
        self._fetcher = fetcher
        self._data_dir = Path(data_dir) if data_dir else None
        self._observers = list(observers or [])
        self._on_finish = on_finish

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
            max_pending=spider.max_pending_requests,
        )
        observers = self._observers
        _call_observers(observers, "on_start", spider)

        rate = spider.requests_per_second
        if rate is None and spider.delay > 0:
            rate = 1.0 / spider.delay
        limiter = RateLimiter(rate, spider.burst)
        n_workers = max(1, spider.concurrency)
        challenge_set = set(spider.challenge_statuses or [])

        async def enqueue_seed(req: Request) -> None:
            if await scheduler.enqueue(req):
                _call_observers(observers, "on_request", req)

        def enqueue_child(req: Request) -> None:
            if scheduler.push(req):
                _call_observers(observers, "on_request", req)

        async def handle_yields(raw: Any, *, source: str) -> None:
            async for result in _iterate_results(raw):
                if isinstance(result, Request):
                    enqueue_child(result)
                elif isinstance(result, dict):
                    _call_observers(observers, "on_item", result)
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
            _call_observers(observers, "on_failure", failure)
            fn = _resolve_callback(spider, failure.request.errback)
            if fn is None:
                fn = getattr(spider, "errback", None)
            if fn is None:
                zlog(spider.name, "fail %s", failure)
                return
            try:
                await handle_yields(fn(failure), source="errback")
            except Exception as exc:
                stats.errors += 1
                stats.bump(REASON_ERRBACK)
                zlog(spider.name, "errback error %s: %s", failure.url, exc)

        async def process_request(req: Request) -> None:
            try:
                req = spider.prepare_request(req)
                await limiter.acquire()
                stats.requests += 1
                try:
                    resp = await fetcher.fetch(req)
                except asyncio.CancelledError:
                    raise
                except DownloadError as exc:
                    stats.retries += exc.retries
                    if exc.kind == "timeout":
                        stats.timeouts += 1
                    await dispatch_failure(
                        Failure(
                            request=req,
                            reason=REASON_DOWNLOAD,
                            exception=exc,
                        )
                    )
                    return
                except Exception as exc:
                    error = DownloadError(req, kind="backend", cause=exc)
                    await dispatch_failure(
                        Failure(
                            request=req,
                            reason=REASON_DOWNLOAD,
                            exception=error,
                        )
                    )
                    return
                if resp is None:
                    await dispatch_failure(Failure(request=req, reason=REASON_DOWNLOAD))
                    return
                stats.retries += resp.retries
                stats.downloaded_bytes += resp.bytes_received
                status = str(resp.status)
                stats.status_counts[status] = stats.status_counts.get(status, 0) + 1
                _call_observers(observers, "on_response", resp)
                if resp.status >= 400:
                    if resp.status in challenge_set:
                        stats.challenges += 1
                        stats.bump("challenge")
                    else:
                        await dispatch_failure(
                            Failure(
                                request=req,
                                reason=REASON_HTTP,
                                status=resp.status,
                                response=resp,
                            )
                        )
                        return

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
                    return
                try:
                    source = (
                        req.callback
                        if isinstance(req.callback, str)
                        else getattr(req.callback, "__name__", "callback")
                    )
                    await handle_yields(callback(resp), source=str(source))
                except Exception as exc:
                    await dispatch_failure(
                        Failure(
                            request=req,
                            reason=REASON_PARSE,
                            status=resp.status,
                            response=resp,
                            exception=exc,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = DownloadError(req, kind="backend", cause=exc)
                await dispatch_failure(
                    Failure(
                        request=req,
                        reason=REASON_DOWNLOAD,
                        exception=error,
                    )
                )

        async def worker() -> None:
            while True:
                req = await scheduler.pop()
                try:
                    await process_request(req)
                finally:
                    scheduler.task_done()

        pipeline_open = False
        workers: list[asyncio.Task[Any]] = []
        try:
            await self._pipeline.open(spider)
            pipeline_open = True
            workers = [asyncio.create_task(worker()) for _ in range(n_workers)]
            async for req in spider.start():
                await enqueue_seed(req)
            await scheduler.join()
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if pipeline_open:
                await self._pipeline.close(spider)

        stats.filtered = scheduler.filtered
        stats.queue_peak = scheduler.queue_peak
        stats.queue_rejected = scheduler.rejected
        stats.duration_s = round(time.perf_counter() - t0, 3)
        out = stats.as_dict()
        req_n = int(out.get("requests") or 0)
        err_n = int(out.get("errors") or 0)
        err_rate = (err_n / req_n) if req_n else 0.0
        out["error_rate"] = round(err_rate, 4)
        threshold = getattr(spider, "health_error_rate", 0.5)
        if threshold is None:
            out["healthy"] = True
        else:
            out["healthy"] = (
                err_rate <= float(threshold)
                and int(out.get("queue_rejected") or 0) == 0
                and (
                    int(out.get("items") or 0) > 0
                    or int(out.get("challenges") or 0) > 0
                    or req_n == 0
                )
            )
        _call_observers(observers, "on_finish", spider, out)
        if self._on_finish is not None:
            try:
                self._on_finish(spider, out)
            except Exception as e:
                zlog(spider.name, "on_finish failed: %s", e)
        return out


async def crawl(
    spider: Spider | type[Spider],
    *,
    pipelines: list[Any] | None = None,
    fetcher: Any | None = None,
    data_dir: str | Path | None = None,
    observers: Sequence[CrawlObserver] | None = None,
    on_finish: Callable[[Spider, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a spider and return crawl statistics."""
    if isinstance(spider, type):
        spider = spider()
    engine = Engine(
        spider,
        pipelines=pipelines,
        fetcher=fetcher,
        data_dir=data_dir,
        observers=observers,
        on_finish=on_finish,
    )
    return await engine.run()


async def crawl_many(
    spiders: Sequence[Spider | type[Spider]],
    *,
    pipelines_factory: Any | None = None,
    pipelines: list[Any] | None = None,
    fetcher_factory: Callable[[Spider], Any] | None = None,
    data_dir: str | Path | None = None,
    max_spiders: int = 3,
    observers_factory: Callable[[Spider], Iterable[CrawlObserver]] | None = None,
    observers: Sequence[CrawlObserver] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run spiders with bounded parallelism and isolated failures."""
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
                if observers_factory is not None:
                    obs = list(observers_factory(spider))
                elif observers is not None:
                    obs = list(observers)
                else:
                    obs = []
                dd = Path(data_dir) / name if data_dir else None
                if fetcher_factory is None:
                    st = await crawl(
                        spider,
                        pipelines=pipes,
                        data_dir=dd,
                        observers=obs,
                    )
                else:
                    fetcher = fetcher_factory(spider)
                    async with fetcher:
                        st = await crawl(
                            spider,
                            pipelines=pipes,
                            fetcher=fetcher,
                            data_dir=dd,
                            observers=obs,
                        )
                results[name] = st
            except Exception as e:
                results[name] = Stats(spider=name, errors=1).as_dict() | {
                    "exception": repr(e)
                }

    await asyncio.gather(*[_one(s) for s in spiders])
    return results
