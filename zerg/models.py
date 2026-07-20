"""Request, Response, Failure, Stats."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, NotRequired, TYPE_CHECKING, TypedDict
from urllib.parse import urldefrag, urlencode, urljoin, urlsplit, urlunsplit

if TYPE_CHECKING:
    from zerg.parser import Parser

REASON_DOWNLOAD = "download"
REASON_HTTP = "http"
REASON_PARSE = "parse"
REASON_CALLBACK = "missing_callback"
REASON_YIELD = "bad_yield"
REASON_ERRBACK = "errback"

_META_CHARSET_RE = re.compile(
    rb'<meta[^>]+charset=["\']?([a-zA-Z0-9_\-]+)',
    re.IGNORECASE,
)
_META_CONTENT_TYPE_RE = re.compile(
    rb'<meta[^>]+content=["\'][^"\']*charset=([a-zA-Z0-9_\-]+)',
    re.IGNORECASE,
)

Callback = str | Callable[..., Any]


def _detect_encoding(content: bytes, header_encoding: str | None) -> str:
    """Detect charset from header, HTML meta, or utf-8."""
    if header_encoding:
        enc = header_encoding.strip().lower()
        if enc and enc not in {"iso-8859-1", "latin-1", "latin1"}:
            return enc
    head = content[:4096]
    m = _META_CHARSET_RE.search(head) or _META_CONTENT_TYPE_RE.search(head)
    if m:
        return m.group(1).decode("ascii", errors="ignore").lower() or "utf-8"
    if header_encoding:
        return header_encoding
    return "utf-8"


@dataclass(slots=True)
class Request:
    """Outbound HTTP request."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    params: dict[str, Any] | None = None
    callback: Callback = "parse"
    errback: Callback = "errback"
    meta: dict[str, Any] = field(default_factory=dict)
    dont_filter: bool = False

    def __post_init__(self) -> None:
        if self.params:
            parts = urlsplit(self.url)
            extra = urlencode(self.params, doseq=True)
            query = f"{parts.query}&{extra}" if parts.query else extra
            self.url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
            )
            self.params = None
        self.meta.setdefault("depth", 0)

    def fingerprint(self) -> str:
        """Dedup key: METHOD:url[:body_sha1]."""
        url, _ = urldefrag(self.url)
        key = f"{self.method.upper()}:{url}"
        if self.body and self.method.upper() not in {"GET", "HEAD"}:
            digest = hashlib.sha1(self.body).hexdigest()[:16]
            key += f":{digest}"
        return key


@dataclass(slots=True)
class Response:
    """HTTP response with lazy text/parser."""

    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    request: Request
    encoding: str = "utf-8"
    _text: str | None = field(default=None, repr=False, compare=False)
    _parser: Parser | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_http(
        cls,
        request: Request,
        url: str,
        status: int,
        headers: dict[str, str],
        content: bytes,
        header_encoding: str | None = None,
    ) -> Response:
        encoding = _detect_encoding(content, header_encoding)
        return cls(
            url=url,
            status=status,
            headers=headers,
            content=content,
            request=request,
            encoding=encoding,
        )

    @property
    def text(self) -> str:
        if self._text is None:
            try:
                self._text = self.content.decode(self.encoding)
            except (LookupError, UnicodeDecodeError):
                self._text = self.content.decode("utf-8", errors="replace")
        return self._text

    @property
    def parser(self) -> Parser:
        if self._parser is None:
            from zerg.parser import Parser

            self._parser = Parser(self.text)
        return self._parser

    @property
    def meta(self) -> dict[str, Any]:
        return self.request.meta

    @property
    def depth(self) -> int:
        return int(self.request.meta.get("depth", 0))

    def urljoin(self, href: str | None) -> str:
        """Resolve href against response URL."""
        if not href:
            return ""
        return urljoin(self.url, href)

    def links(
        self,
        selector: str = "a",
        *,
        attr: str = "href",
        absolute: bool = True,
    ) -> list[str]:
        """Collect link attributes from matching nodes."""
        vals = self.parser.css_attrs(selector, attr)
        out: list[str] = []
        for v in vals:
            if not v:
                continue
            out.append(self.urljoin(v) if absolute else v)
        return out

    def follow(
        self,
        href: str | None,
        *,
        callback: Callback = "parse",
        errback: Callback = "errback",
        meta: dict[str, Any] | None = None,
        **request_kwargs: Any,
    ) -> Request:
        """Build a child Request; depth is parent+1 by default."""
        url = self.urljoin(href)
        if not url:
            raise ValueError("follow() requires a non-empty href")
        child_meta = dict(meta or {})
        child_meta.setdefault("depth", self.depth + 1)
        return Request(
            url,
            callback=callback,
            errback=errback,
            meta=child_meta,
            **request_kwargs,
        )

    def css(self, selector: str, default: str | None = None) -> str:
        return self.parser.css(selector, default)

    def css_all(self, selector: str) -> list[str]:
        return self.parser.css_all(selector)

    def css_attr(
        self, selector: str, attr: str, default: str | None = None
    ) -> str | None:
        return self.parser.css_attr(selector, attr, default)

    def css_first(self, selector: str):
        return self.parser.css_first(selector)

    def extract(self, rules: dict[str, str]) -> dict[str, str]:
        return self.parser.extract(rules)

    def extract_all(
        self, selector: str, rules: dict[str, str | tuple[str, str]]
    ) -> list[dict[str, Any]]:
        return self.parser.extract_all(selector, rules)

    def json(self) -> Any:
        import orjson

        return orjson.loads(self.content)

    def header(self, name: str, default: str | None = None) -> str | None:
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return v
        return default

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url!r}>"


@dataclass(slots=True)
class Failure:
    """Failed request passed to errback."""

    request: Request
    reason: str
    status: int | None = None
    response: Response | None = None
    exception: BaseException | None = None

    @property
    def url(self) -> str:
        return self.request.url

    @property
    def meta(self) -> dict[str, Any]:
        return self.request.meta

    def __str__(self) -> str:
        parts = [self.reason, self.url]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.exception is not None:
            parts.append(f"{type(self.exception).__name__}: {self.exception}")
        return " ".join(parts)

    def __repr__(self) -> str:
        return (
            f"<Failure {self.reason} {self.url!r}"
            f"{f' status={self.status}' if self.status is not None else ''}>"
        )


@dataclass(slots=True)
class Stats:
    """Crawl counters returned by crawl()."""

    spider: str = ""
    requests: int = 0
    items: int = 0
    errors: int = 0
    filtered: int = 0
    data_dir: str = ""
    duration_s: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str, n: int = 1) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + n

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ZergError(Exception):
    """Base library error."""


class CrawlError(ZergError):
    """Fatal crawl error."""


class Item(TypedDict, total=False):
    """Optional item typing helper."""

    title: str
    url: str
    images: list[str]
    files: list[str]
    files_count: int


class MediaItem(TypedDict):
    """Item shape for media()."""

    images: list[str]
    title: NotRequired[str]
