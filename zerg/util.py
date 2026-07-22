"""URL / pagination / anti-bot detection helpers."""

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


def form_body(data: dict[str, Any]) -> bytes:
    """URL-encoded form body for POST requests."""
    return urlencode(data, doseq=True).encode()


def slug(text: str, max_len: int = 60) -> str:
    for ch in '/\\:*?"<>|\n\r\t':
        text = text.replace(ch, "_")
    text = text.strip(" .") or "item"
    return text[:max_len]


def detect_waf(
    text: str,
    *,
    status: int | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Best-effort WAF / challenge classification (detect only, no bypass).

    Returns ``{kind, challenged, detail}`` where kind is one of:
    ``ok``, ``jsl``, ``waf_probe``, ``captcha``, ``blocked``, ``empty``, ``unknown``.

    Large 200 HTML pages often contain the substrings ``document.cookie`` /
    ``location`` / ``captcha`` in app JS — those must **not** count as jsl.
    Real RUJIA jsl is short + challenge status or explicit clearance markers.
    """
    body = text or ""
    low = body.lower()
    n = len(body)
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    challenge_status = status in {401, 403, 407, 412, 418, 429, 503, 521}
    # interstitial shells are almost always small
    short = n < 8000

    if not body and status is not None and status >= 400:
        return {
            "kind": "empty",
            "challenged": True,
            "detail": f"status={status}",
        }

    # RUJIA / CDN jsl: short script walls (or explicit 521) with cookie assign
    cookie_assign = bool(
        re.search(r"document\.cookie\s*=", body, re.I)
        and re.search(r"location(?:\.href|\.replace)?", body, re.I)
    )
    go_payload = bool(re.search(r"[;\s]go\(\{", body) or ";go(" in low)
    jsl_marker = (
        "__jsl" in low
        or "jsl_clearance" in low
        or "jsl_clearance_s" in low
    )
    if (cookie_assign or go_payload or jsl_marker) and (
        short or challenge_status or status == 521
    ):
        return {"kind": "jsl", "challenged": True, "detail": "jsl_clearance"}

    if short and (
        "probe.js" in low
        or "x-waf-captcha" in low
        or "x-waf-captcha-referer" in str(hdrs)
        or ("buid" in low and "probe" in low)
    ):
        return {"kind": "waf_probe", "challenged": True, "detail": "probe_js"}

    captcha_hit = (
        "本站开启了验证码" in body
        or "验证码保护" in body
        or "cf-challenge" in low
        or "cdn-cgi/challenge" in low
        or (short and ("验证码" in body or re.search(r"\bcaptcha\b", low)))
    )
    if captcha_hit:
        return {"kind": "captcha", "challenged": True, "detail": "captcha"}

    if challenge_status:
        return {
            "kind": "blocked",
            "challenged": True,
            "detail": f"status={status}",
        }

    if status == 202 and n < 2000:
        return {
            "kind": "waf_probe",
            "challenged": True,
            "detail": "http_202_shell",
        }

    if status is not None and status < 400 and n > 500:
        return {"kind": "ok", "challenged": False, "detail": ""}

    return {
        "kind": "unknown",
        "challenged": False,
        "detail": f"status={status}",
    }


def detect_waf_response(response: Any) -> dict[str, Any]:
    """``detect_waf`` over a Response-like object."""
    text = getattr(response, "text", None) or ""
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None) or {}
    return detect_waf(text, status=status, headers=headers)


def rate_limit_headers(headers: dict[str, str] | None) -> dict[str, int | str | None]:
    """Parse common API rate-limit response headers (GitHub-style)."""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}

    def _int(key: str) -> int | None:
        raw = h.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    return {
        "limit": _int("x-ratelimit-limit"),
        "remaining": _int("x-ratelimit-remaining"),
        "reset": _int("x-ratelimit-reset"),
        "used": _int("x-ratelimit-used"),
        "resource": h.get("x-ratelimit-resource"),
        "retry_after": h.get("retry-after"),
    }

