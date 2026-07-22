"""Extractor unit tests — no network."""

from __future__ import annotations

from zerg.extract import (
    embedded_json,
    feed_items,
    json_ld,
    re_first,
    sitemap_urls,
    table_rows,
)
from zerg.models import Request, Response


def _resp(content: bytes | str, url: str = "https://ex.com/") -> Response:
    if isinstance(content, str):
        content = content.encode()
    return Response.from_http(
        request=Request(url),
        url=url,
        status=200,
        headers={},
        content=content,
    )


def test_feed_rss():
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>T1</title>
        <link>https://ex.com/1</link>
        <description>D</description>
        <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
        <guid>1</guid>
        <category>news</category>
      </item>
    </channel></rss>
    """
    items = feed_items(_resp(xml))
    assert len(items) == 1
    assert items[0]["title"] == "T1"
    assert items[0]["source"] == "rss"
    assert items[0]["categories"] == ["news"]


def test_feed_atom():
    xml = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>A1</title>
        <link href="https://ex.com/a" rel="alternate"/>
        <id>a1</id>
        <updated>2024-01-01T00:00:00Z</updated>
        <summary>S</summary>
        <author><name>Bob</name></author>
      </entry>
    </feed>
    """
    items = feed_items(_resp(xml))
    assert len(items) == 1
    assert items[0]["title"] == "A1"
    assert items[0]["link"] == "https://ex.com/a"
    assert items[0]["author"] == "Bob"
    assert items[0]["source"] == "atom"


def test_sitemap_urls():
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/1</loc><lastmod>2024-01-01</lastmod></url>
      <url><loc>https://ex.com/2</loc></url>
    </urlset>
    """
    rows = sitemap_urls(_resp(xml))
    assert len(rows) == 2
    assert rows[0]["loc"] == "https://ex.com/1"
    assert rows[0]["type"] == "url"


def test_table_rows():
    html = """
    <table>
      <tr><th>Name</th><th>Age</th></tr>
      <tr><td>Ada</td><td>36</td></tr>
      <tr><td>Bob</td><td>42</td></tr>
    </table>
    """
    rows = table_rows(_resp(html))
    assert rows == [
        {"Name": "Ada", "Age": "36"},
        {"Name": "Bob", "Age": "42"},
    ]


def test_embedded_json_and_ld():
    html = """
    <script id="__NEXT_DATA__" type="application/json">{"page":1}</script>
    <script type="application/ld+json">{"@type":"WebPage"}</script>
    """
    r = _resp(html)
    assert embedded_json(r) == {"page": 1}
    assert json_ld(r) == [{"@type": "WebPage"}]
    assert re_first(r, r'"page":(\d+)') == "1"


def test_detect_waf_kinds():
    from zerg.util import detect_waf

    assert (
        detect_waf(
            "<script>document.cookie=('_')+('_')+('jsl');location</script>",
            status=521,
        )["kind"]
        == "jsl"
    )
    assert (
        detect_waf(
            '<script src="/abc/probe.js"></script> var buid="x"',
            status=202,
        )["kind"]
        == "waf_probe"
    )
    assert detect_waf("本站开启了验证码保护 captcha", status=200)["kind"] == "captcha"
    assert detect_waf("<html>" + "x" * 600, status=200)["kind"] == "ok"


def test_strip_jsonp():
    from zerg.extract import strip_jsonp

    assert strip_jsonp('{"a":1}') == {"a": 1}
    assert strip_jsonp('data_callback([{"x":1}])') == [{"x": 1}]
    assert strip_jsonp('cb({"k": "v"});') == {"k": "v"}


def test_form_body():
    from zerg.util import form_body

    assert form_body({"kw": "spider"}) == b"kw=spider"


def test_rate_limit_headers():
    from zerg.util import rate_limit_headers

    h = rate_limit_headers(
        {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "12",
            "X-RateLimit-Reset": "1784644310",
            "X-RateLimit-Used": "48",
            "X-RateLimit-Resource": "core",
            "Retry-After": "30",
        }
    )
    assert h["limit"] == 60
    assert h["remaining"] == 12
    assert h["used"] == 48
    assert h["resource"] == "core"
    assert h["retry_after"] == "30"


def test_detect_waf_no_false_jsl_on_large_html():
    from zerg.util import detect_waf

    huge = (
        "<!DOCTYPE html><html><head><title>shop</title></head><body>"
        + ("var x=1; document.cookie; location.href; " * 200)
        + "</body></html>"
    )
    assert detect_waf(huge, status=200)["kind"] == "ok"
    assert (
        detect_waf(
            "<script>document.cookie=('_')+('=')+('1');location.href=1</script>",
            status=521,
        )["kind"]
        == "jsl"
    )
    assert (
        detect_waf(
            "<html>" + ("word captcha widget " * 500) + "</html>",
            status=200,
        )["kind"]
        == "ok"
    )
