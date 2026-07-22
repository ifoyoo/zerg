"""Extractors for feeds, sitemaps, tables, embedded JSON."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

import orjson

from zerg.models import Response

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_JSONP_RE = re.compile(
    r"^\s*[\w$.]+\s*\(\s*(.*)\s*\)\s*;?\s*$",
    re.S,
)


def strip_jsonp(text: str) -> Any:
    """Parse JSON or JSONP (``cb({...})`` / ``cb([...])``) into Python objects."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty jsonp body")
    try:
        return orjson.loads(raw.encode())
    except orjson.JSONDecodeError:
        pass
    m = _JSONP_RE.match(raw)
    if not m:
        # looser: first ( ... last )
        a, b = raw.find("("), raw.rfind(")")
        if a < 0 or b <= a:
            raise ValueError("not json/jsonp")
        payload = raw[a + 1 : b].strip()
    else:
        payload = m.group(1).strip()
    return orjson.loads(payload.encode())


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _local(el: ET.Element) -> str:
    return _strip_ns(el.tag)


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _find_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in list(el):
        if _local(child) == name:
            return child
    return None


def _find_children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in list(el) if _local(c) == name]


def feed_items(response: Response) -> list[dict[str, Any]]:
    """Parse RSS/Atom items."""
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return []

    root_name = _local(root)
    items: list[dict[str, Any]] = []

    channel = _find_child(root, "channel")
    if root_name == "rss" or channel is not None:
        if channel is None:
            channel = root
        for item in _find_children(channel, "item"):
            enclosure = _find_child(item, "enclosure")
            items.append(
                {
                    "title": _text(_find_child(item, "title")),
                    "link": _text(_find_child(item, "link")),
                    "description": _text(_find_child(item, "description")),
                    "pubDate": _text(_find_child(item, "pubDate")),
                    "guid": _text(_find_child(item, "guid")),
                    "author": _text(_find_child(item, "author"))
                    or _text(_find_child(item, "creator")),
                    "enclosure": (
                        enclosure.get("url") if enclosure is not None else ""
                    ),
                    "categories": [
                        _text(c) for c in _find_children(item, "category") if _text(c)
                    ],
                    "source": "rss",
                }
            )
        return items

    if root_name == "feed":
        for entry in _find_children(root, "entry"):
            link = ""
            for ln in _find_children(entry, "link"):
                href = ln.get("href") or ""
                rel = ln.get("rel", "alternate")
                if href and rel in ("alternate", None, ""):
                    link = href
                    break
                if not link and href:
                    link = href
            content_el = _find_child(entry, "content") or _find_child(entry, "summary")
            author_el = _find_child(entry, "author")
            items.append(
                {
                    "title": _text(_find_child(entry, "title")),
                    "link": link,
                    "description": _text(content_el),
                    "pubDate": _text(_find_child(entry, "updated"))
                    or _text(_find_child(entry, "published")),
                    "guid": _text(_find_child(entry, "id")),
                    "author": _text(
                        _find_child(author_el, "name")
                        if author_el is not None
                        else None
                    ),
                    "enclosure": "",
                    "categories": [
                        (c.get("term") or _text(c))
                        for c in _find_children(entry, "category")
                    ],
                    "source": "atom",
                }
            )
        return items

    return items


def sitemap_urls(
    response: Response, *, limit: int | None = None
) -> list[dict[str, str]]:
    """Parse sitemap entries into ``{loc, lastmod, type}``."""
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return []

    out: list[dict[str, str]] = []
    for el in root.iter():
        name = _local(el)
        if name not in {"url", "sitemap"}:
            continue
        loc = _text(_find_child(el, "loc"))
        if not loc:
            continue
        out.append(
            {
                "loc": loc,
                "lastmod": _text(_find_child(el, "lastmod")),
                "type": "sitemap" if name == "sitemap" else "url",
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def table_rows(
    response: Response,
    selector: str = "table",
    *,
    header: bool = True,
) -> list[dict[str, str]]:
    """Extract table rows as dicts."""
    table = response.css_first(selector)
    if table is None:
        nodes = response.parser._tree.css("table")
        table = nodes[0] if nodes else None
    if table is None:
        return []

    rows = table.css("tr")
    if not rows:
        return []

    def row_cells(row: Any) -> list[str]:
        out: list[str] = []
        for child in row.iter(include_text=False):
            if child.tag in {"th", "td"}:
                out.append(child.text(strip=True))
        if out:
            return out
        for child in row.css("th"):
            out.append(child.text(strip=True))
        for child in row.css("td"):
            out.append(child.text(strip=True))
        return out

    parsed = [row_cells(r) for r in rows]
    parsed = [r for r in parsed if any(c.strip() for c in r)]
    if not parsed:
        return []

    if header:
        headers = [h or f"col{i}" for i, h in enumerate(parsed[0])]
        body = parsed[1:]
    else:
        headers = [f"col{i}" for i in range(len(parsed[0]))]
        body = parsed

    results: list[dict[str, str]] = []
    for row in body:
        item = {
            headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))
        }
        results.append(item)
    return results


def embedded_json(
    response: Response,
    script_id: str = "__NEXT_DATA__",
) -> Any | None:
    """Extract JSON from a script tag by id."""
    if script_id == "__NEXT_DATA__":
        m = _NEXT_DATA_RE.search(response.text)
        if m:
            try:
                return orjson.loads(m.group(1).encode())
            except orjson.JSONDecodeError:
                return None

    pat = re.compile(
        rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>',
        re.I | re.S,
    )
    m = pat.search(response.text)
    if m:
        try:
            return orjson.loads(m.group(1).encode())
        except orjson.JSONDecodeError:
            return None
    return None


def json_ld(response: Response) -> list[Any]:
    """Return all ld+json blobs."""
    out: list[Any] = []
    for m in _JSON_LD_RE.finditer(response.text):
        try:
            out.append(orjson.loads(m.group(1).encode()))
        except orjson.JSONDecodeError:
            continue
    return out


def re_first(response: Response, pattern: str, group: int = 1) -> str:
    m = re.search(pattern, response.text, re.I | re.S)
    if not m:
        return ""
    try:
        return m.group(group)
    except IndexError:
        return m.group(0)
