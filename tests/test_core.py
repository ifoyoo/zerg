"""Core unit tests — no network."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from zerg import (
    Failure,
    Request,
    Response,
    Spider,
    crawl,
    crawl_many,
    jsonl,
)
from zerg.models import REASON_HTTP, REASON_YIELD
from zerg.parser import Parser
from zerg.scheduler import Scheduler
from zerg.util import absolute_url, paginate, parse_link_header, slug

# ── Models ──────────────────────────────────────────────────────────


def test_request_params_and_fingerprint():
    r = Request("https://ex.com/a", params={"q": "1"})
    assert "q=1" in r.url
    assert r.params is None
    assert r.meta["depth"] == 0
    assert r.fingerprint() == "GET:https://ex.com/a?q=1"

    r2 = Request("https://ex.com/a#frag")
    assert r2.fingerprint() == "GET:https://ex.com/a"

    body = b'{"a":1}'
    r3 = Request("https://ex.com/a", method="POST", body=body)
    assert r3.fingerprint().startswith("POST:https://ex.com/a:")


def test_response_follow_depth_and_css():
    html = b"""
    <html><body>
      <h1>Hi</h1>
      <a class="item" href="/p/1">one</a>
      <a class="item" href="https://other.com/x">out</a>
      <img src="//cdn.ex.com/a.jpg"/>
    </body></html>
    """
    req = Request("https://ex.com/")
    resp = Response.from_http(
        request=req,
        url="https://ex.com/",
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=html,
        header_encoding="utf-8",
    )
    assert resp.css("h1") == "Hi"
    assert resp.links("a.item") == [
        "https://ex.com/p/1",
        "https://other.com/x",
    ]
    child = resp.follow("/p/1", callback="parse_detail")
    assert child.url == "https://ex.com/p/1"
    assert child.meta["depth"] == 1
    assert child.callback == "parse_detail"


def test_parser_extract_all():
    p = Parser(
        """
        <div class="card"><h2>A</h2><a href="/a">x</a></div>
        <div class="card"><h2>B</h2><a href="/b">y</a></div>
        """
    )
    rows = p.extract_all(
        "div.card",
        {"title": "h2", "url": ("a", "href")},
    )
    assert rows == [
        {"title": "A", "url": "/a"},
        {"title": "B", "url": "/b"},
    ]


def _row_scoped_extract_all(html: str, selector: str, rules: dict) -> list[dict]:
    """Reference semantics: search every row on its own."""
    from selectolax.lexbor import LexborHTMLParser

    out = []
    for row in LexborHTMLParser(html).css(selector):
        item = {}
        for key, spec in rules.items():
            if isinstance(spec, tuple):
                node = row.css_first(spec[0])
                item[key] = node.attrs.get(spec[1], None) if node else None
            else:
                node = row.css_first(spec)
                item[key] = node.text(strip=True) if node else ""
        out.append(item)
    return out


def test_extract_all_matches_row_scoped_semantics():
    """Batched extraction must equal per-row searching, nesting included."""
    cases = [
        (
            # an inner row is visible to its enclosing row as well
            "<div class='c'><div class='c'><h2>inner</h2></div></div>"
            "<div class='c'><h2>t</h2><a href='/1'>1</a><a href='/2'>2</a></div>",
            "div.c",
            {
                "title": "h2",
                "first": ("a", "href"),
                "self": "div.c",
                "none": (".zz", "href"),
            },
        ),
        (
            "<div class='c'><h2></h2><h2>second</h2></div>"
            "<div class='c'><p>x</p><h2>t</h2></div>",
            "div.c",
            {"title": "h2", "first_child": ":first-child", "has_p": "div:has(p)"},
        ),
        (
            "<div class='wrap'><div class='c'><h2>t</h2></div></div>"
            "<div class='wrap'><div class='c'><a href='/x'>x</a></div></div>",
            "div.wrap div.c",
            {"title": "h2", "url": ("a", "href"), "body": "body"},
        ),
    ]
    for html, selector, rules in cases:
        assert Parser(html).extract_all(selector, rules) == _row_scoped_extract_all(
            html, selector, rules
        )


def test_extract_all_matches_row_scoped_semantics_random():
    """Randomized shapes: nested rows, missing fields, many matches per row."""
    rng = random.Random(20260919)
    for _ in range(150):
        parts = []
        for index in range(rng.randint(0, 25)):
            inner = "<div class='c'><h2>inner</h2></div>" if rng.random() < 0.4 else ""
            title = f"<h2>t{index}</h2>" if rng.random() < 0.8 else ""
            links = "".join(
                f"<a href='/{index}/{n}'>l</a>"
                for n in range(rng.randint(0, 3) if rng.random() < 0.5 else 1)
            )
            parts.append(f"<div class='c'>{title}{links}{inner}</div>")
        html = "<html><body>" + "".join(parts) + "</body></html>"
        rules: dict = {}
        for key, spec in (
            ("title", "h2"),
            ("self", "div.c"),
            ("deep", ".c"),
            ("missing", ".zz"),
            ("url", ("a", "href")),
        ):
            if rng.random() < 0.7:
                rules[key] = spec
        assert Parser(html).extract_all("div.c", rules) == _row_scoped_extract_all(
            html, "div.c", rules
        )


# ── Scheduler ───────────────────────────────────────────────────────


def test_scheduler_depth_meta_tolerates_strings():
    s = Scheduler(max_depth=1)
    assert s.push(Request("https://ex.com/a", meta={"depth": "1"}))
    assert not s.push(Request("https://ex.com/b", meta={"depth": "2"}))
    assert s.filtered == 1
    # junk depth is treated as 0 instead of raising
    assert s.push(Request("https://ex.com/c", meta={"depth": "junk"}))


def test_response_depth_tolerates_junk():
    req = Request("https://ex.com/x", meta={"depth": "3"})
    resp = Response.from_http(
        request=req, url="https://ex.com/x", status=200, headers={}, content=b""
    )
    assert resp.depth == 3
    junk = Request("https://ex.com/y", meta={"depth": "nope"})
    assert (
        Response.from_http(
            request=junk, url="https://ex.com/y", status=200, headers={}, content=b""
        ).depth
        == 0
    )


def test_scheduler_dedup_domain_depth():
    s = Scheduler(allowed_domains=["ex.com"], max_depth=1)
    assert s.push(Request("https://ex.com/a"))
    assert not s.push(Request("https://ex.com/a"))  # dedup
    assert s.filtered == 1

    assert not s.push(Request("https://evil.com/x"))  # domain
    assert s.filtered == 2

    deep = Request("https://ex.com/deep", meta={"depth": 2})
    assert not s.push(deep)
    assert s.filtered == 3

    # subdomain allowed
    assert s.push(Request("https://www.ex.com/b"))

    # dont_filter bypasses dedup
    assert s.push(Request("https://ex.com/a", dont_filter=True))


# ── Utils ───────────────────────────────────────────────────────────


def test_as_int_as_float_coercion():
    from zerg.util import as_float, as_int

    assert as_int(3) == 3
    assert as_int("4") == 4
    assert as_int("junk") == 0
    assert as_int(None, -1) == -1
    assert as_float("0.25") == 0.25
    assert as_float("junk", 0.5) == 0.5


def test_retry_after_seconds():
    from zerg.http import _retry_after_seconds

    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("") is None
    assert _retry_after_seconds("5") == 5.0
    assert _retry_after_seconds("  7 ") == 7.0
    assert _retry_after_seconds("9999") == 30.0
    # negatives, floats, and HTTP-dates fall back to exponential backoff
    assert _retry_after_seconds("-1") is None
    assert _retry_after_seconds("1.5") is None
    assert _retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") is None


def test_util_helpers():
    assert absolute_url("https://ex.com/a/", "../b") == "https://ex.com/b"
    assert list(paginate("https://ex.com?p={page}", 1, 3)) == [
        "https://ex.com?p=1",
        "https://ex.com?p=2",
        "https://ex.com?p=3",
    ]
    assert parse_link_header(
        '<https://ex.com/2>; rel="next", <https://ex.com/1>; rel="prev"'
    ) == {"next": "https://ex.com/2", "prev": "https://ex.com/1"}
    assert slug('a/b:c*?"') == "a_b_c___"


# ── Engine with fake fetcher ────────────────────────────────────────


class _FakeFetch:
    """Deterministic in-memory fetcher for engine tests."""

    def __init__(self, pages: dict[str, tuple[int, bytes]]):
        self.pages = pages
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def fetch(self, request: Request) -> Response | None:
        self.calls.append(request.url)
        hit = self.pages.get(request.url)
        if hit is None:
            return None
        status, content = hit
        return Response.from_http(
            request=request,
            url=request.url,
            status=status,
            headers={"content-type": "text/html"},
            content=content,
            header_encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_engine_crawl_items_and_follow(tmp_path: Path):
    home = b'<html><body><a class="item" href="/p/1">1</a></body></html>'
    detail = b"<html><body><h1>Item 1</h1></body></html>"
    fake = _FakeFetch(
        {
            "https://ex.com/": (200, home),
            "https://ex.com/p/1": (200, detail),
        }
    )

    class S(Spider):
        name = "t"
        start_urls = ["https://ex.com/"]
        concurrency = 2
        allowed_domains = ["ex.com"]
        max_depth = 1

        async def parse(self, response):
            for href in response.links("a.item"):
                yield response.follow(href, callback=self.parse_detail)

        async def parse_detail(self, response):
            yield {"title": response.css("h1"), "url": response.url}

    out = tmp_path / "items.jsonl"
    stats = await crawl(S, pipelines=[jsonl(out)], fetcher=fake, data_dir=tmp_path)
    assert stats["items"] == 1
    assert stats["requests"] == 2
    assert stats["errors"] == 0
    text = out.read_text()
    assert "Item 1" in text


@pytest.mark.asyncio
async def test_engine_http_error_errback(tmp_path: Path):
    fake = _FakeFetch({"https://ex.com/": (404, b"nope")})

    class S(Spider):
        name = "err"
        start_urls = ["https://ex.com/"]
        concurrency = 1

        async def parse(self, response):
            yield {"should": "not-run"}

        async def errback(self, failure: Failure):
            assert failure.reason == REASON_HTTP
            yield {"fallback": True, "status": failure.status}

    stats = await crawl(
        S,
        pipelines=[jsonl(tmp_path / "i.jsonl")],
        fetcher=fake,
        data_dir=tmp_path,
    )
    assert stats["items"] == 1
    assert stats["errors"] == 1
    assert stats["by_reason"].get("http") == 1


@pytest.mark.asyncio
async def test_engine_bad_yield(tmp_path: Path):
    fake = _FakeFetch({"https://ex.com/": (200, b"<html></html>")})

    class S(Spider):
        name = "bad"
        start_urls = ["https://ex.com/"]
        concurrency = 1

        async def parse(self, response):
            yield "not-a-dict"  # type: ignore[misc]

    stats = await crawl(S, fetcher=fake, data_dir=tmp_path)
    assert stats["items"] == 0
    assert stats["errors"] == 1
    assert stats["by_reason"].get(REASON_YIELD) == 1


@pytest.mark.asyncio
async def test_engine_callback_shapes(tmp_path: Path):
    """Callbacks may return a list, a single Request, or a coroutine."""
    fake = _FakeFetch(
        {
            "https://ex.com/list": (200, b"<html></html>"),
            "https://ex.com/one": (200, b"<html></html>"),
        }
    )

    class S(Spider):
        name = "shapes"
        start_urls = ["https://ex.com/list"]
        concurrency = 1
        max_depth = 1

        def parse(self, response):  # sync callback returning a list
            return [
                {"url": response.url},
                Request("https://ex.com/one", callback="parse_one"),
            ]

        async def parse_one(self, response):  # async callback returning a dict
            return {"url": response.url}

    stats = await crawl(S, fetcher=fake, data_dir=tmp_path)
    assert stats["requests"] == 2
    assert stats["items"] == 2
    assert stats["errors"] == 0
    assert fake.calls == ["https://ex.com/list", "https://ex.com/one"]


@pytest.mark.asyncio
async def test_engine_tolerates_string_depth_from_start(tmp_path: Path):
    """A str meta["depth"] used to raise out of the seed loop."""
    fake = _FakeFetch(
        {
            "https://ex.com/a": (200, b"<html></html>"),
            "https://ex.com/b": (200, b"<html></html>"),
        }
    )

    class S(Spider):
        name = "strdepth"
        concurrency = 1
        max_depth = 1

        async def start(self):
            yield Request("https://ex.com/a", meta={"depth": "0"})
            yield Request("https://ex.com/b", meta={"depth": "1"})

        async def parse(self, response):
            yield {"url": response.url}

    stats = await crawl(S, fetcher=fake, data_dir=tmp_path)
    assert stats["requests"] == 2
    assert stats["items"] == 2
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_engine_health_threshold_tolerates_strings(tmp_path: Path):
    fake = _FakeFetch({"https://ex.com/": (200, b"<html></html>")})

    class S(Spider):
        name = "health"
        start_urls = ["https://ex.com/"]
        concurrency = 1
        health_error_rate = "1"

        async def parse(self, response):
            yield {"ok": True}

    assert (await crawl(S, fetcher=fake, data_dir=tmp_path))["healthy"] is True

    class Junk(S):
        name = "health_junk"
        health_error_rate = "nonsense"

    # unparsable threshold falls back to the documented 0.5
    assert (await crawl(Junk, fetcher=fake, data_dir=tmp_path))["healthy"] is True


@pytest.mark.asyncio
async def test_engine_depth_filter(tmp_path: Path):
    home = b'<html><a class="item" href="/p/1">1</a></html>'
    fake = _FakeFetch(
        {
            "https://ex.com/": (200, home),
            "https://ex.com/p/1": (200, b"<h1>x</h1>"),
        }
    )

    class S(Spider):
        name = "depth"
        start_urls = ["https://ex.com/"]
        concurrency = 2
        max_depth = 0  # seeds only — follow must be filtered

        async def parse(self, response):
            for href in response.links("a.item"):
                yield response.follow(href, callback="parse")
            yield {"page": response.url}

    stats = await crawl(S, fetcher=fake, data_dir=tmp_path)
    assert stats["requests"] == 1
    assert stats["items"] == 1
    assert stats["filtered"] >= 1


@pytest.mark.asyncio
async def test_challenge_statuses_and_healthy(tmp_path: Path):
    """521 in challenge_statuses → callback path, not hard http error."""

    class _Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def fetch(self, request: Request) -> Response | None:
            # simplify: always 521 with jsl-ish body for seed
            return Response.from_http(
                request=request,
                url=request.url,
                status=521,
                headers={"content-type": "text/html"},
                content=b"<script>document.cookie=('_')+('x')+('=')+('1');location</script>go({\"a\":1})",
                header_encoding="utf-8",
            )

    class S(Spider):
        name = "chal"
        start_urls = ["https://ex.com/"]
        concurrency = 1
        challenge_statuses = [521]
        health_error_rate = 0.5

        async def parse(self, response):
            yield {"status": response.status, "n": 1}

    stats = await crawl(S, fetcher=_Fake(), data_dir=tmp_path)
    assert stats["items"] == 1
    assert stats["challenges"] == 1
    assert stats["errors"] == 0
    assert stats["by_reason"].get("challenge") == 1
    assert stats["healthy"] is True
    assert stats["error_rate"] == 0.0


@pytest.mark.asyncio
async def test_require_keys_pipeline(tmp_path: Path):
    from zerg import require_keys

    class _Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def fetch(self, request: Request) -> Response | None:
            return Response.from_http(
                request=request,
                url=request.url,
                status=200,
                headers={},
                content=b"<html></html>",
                header_encoding="utf-8",
            )

    class S(Spider):
        name = "rk"
        start_urls = ["https://ex.com/"]
        concurrency = 1

        async def parse(self, response):
            yield {"title": "a", "url": "u"}
            yield {"title": "b"}  # missing url → drop
            yield {"title": "c", "url": "u2"}

    rk = require_keys("title", "url")
    stats = await crawl(S, pipelines=[rk], fetcher=_Fake(), data_dir=tmp_path)
    assert stats["items"] == 2
    assert rk.dropped == 1


@pytest.mark.asyncio
async def test_observer_receives_domain_objects(tmp_path: Path):
    fake = _FakeFetch({"https://ex.com/": (200, b"<html></html>")})
    events: list[tuple[str, object]] = []

    class Observer:
        def on_start(self, spider):
            events.append(("start", spider))

        def on_response(self, response):
            events.append(("response", response))

        def on_item(self, item):
            events.append(("item", item))

        def on_finish(self, spider, stats):
            events.append(("finish", stats))

    class S(Spider):
        name = "observed"
        start_urls = ["https://ex.com/"]

        async def parse(self, response):
            yield {"url": response.url}

    stats = await crawl(S, fetcher=fake, data_dir=tmp_path, observers=[Observer()])
    assert stats["items"] == 1
    assert [name for name, _ in events] == ["start", "response", "item", "finish"]
    assert isinstance(events[1][1], Response)


@pytest.mark.asyncio
async def test_crawl_many_isolates_failures_and_builds_observers(tmp_path: Path):
    observed: list[str] = []

    class Good(Spider):
        name = "good"
        start_urls = ["https://ex.com/"]

        async def parse(self, response):
            yield {"ok": True}

    class Broken(Spider):
        name = "broken"

        async def start(self):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    class Observer:
        def __init__(self, name: str):
            self.name = name

        def on_start(self, spider):
            observed.append(self.name)

    fake = _FakeFetch({"https://ex.com/": (200, b"<html></html>")})
    results = await crawl_many(
        [Good, Broken],
        fetcher_factory=lambda spider: fake,
        data_dir=tmp_path,
        observers_factory=lambda spider: [Observer(spider.name)],
        max_spiders=2,
    )

    assert results["good"]["items"] == 1
    assert "exception" in results["broken"]
    assert set(observed) == {"good", "broken"}


def test_detect_encoding_gbk_over_wrong_header():
    from zerg.models import Request, Response, _detect_encoding

    # classic GBK bytes for "你好"
    raw = "你好spider".encode("gbk")
    assert _detect_encoding(raw, "utf-8") in {"gbk", "gb18030"}
    resp = Response.from_http(
        request=Request("https://ex.com/x.js"),
        url="https://ex.com/x.js",
        status=200,
        headers={"content-type": "application/javascript"},
        content=raw,
        header_encoding="utf-8",
    )
    assert "你好" in resp.text
