"""URL / pagination helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?([^",;]+)"?', re.I)


def absolute_url(base: str, href: str | None) -> str:
    if not href:
        return ""
    return urljoin(base, href)


def absolute_urls(base: str, hrefs: list[str | None]) -> list[str]:
    out: list[str] = []
    for h in hrefs:
        u = absolute_url(base, h)
        if u:
            out.append(u)
    return out


def paginate(
    template: str | Callable[[int], str],
    start: int = 1,
    stop: int = 1,
) -> Iterator[str]:
    """Yield page URLs from ``{page}`` template or callable."""
    for page in range(start, stop + 1):
        if callable(template):
            yield template(page)
        else:
            yield template.format(page=page)


def replace_query(url: str, **params: Any) -> str:
    """Update or add query params."""
    parts = urlsplit(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    for k, v in params.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = [str(v)]
    flat: list[tuple[str, str]] = []
    for k, vals in q.items():
        for val in vals:
            flat.append((k, val))
    query = urlencode(flat)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse Link header into ``{rel: url}``."""
    if not value:
        return {}
    out: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        m = _LINK_RE.search(part)
        if m:
            out[m.group(2)] = m.group(1)
            continue
        if "<" not in part or ">" not in part:
            continue
        try:
            url_part, *params = part.split(";")
            url = url_part.strip().lstrip("<").rstrip(">")
            rel = ""
            for p in params:
                p = p.strip()
                if p.lower().startswith("rel="):
                    rel = p.split("=", 1)[1].strip().strip('"')
            if url and rel:
                out[rel] = url
        except (ValueError, IndexError):
            continue
    return out


def slug(text: str, max_len: int = 60) -> str:
    for ch in '/\\:*?"<>|\n\r\t':
        text = text.replace(ch, "_")
    text = text.strip(" .") or "item"
    return text[:max_len]
