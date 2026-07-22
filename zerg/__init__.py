"""zerg — a small, high-performance async crawling framework."""

from __future__ import annotations

from zerg.engine import CrawlObserver, Engine, crawl, crawl_many
from zerg.extract import (
    embedded_json,
    feed_items,
    json_ld,
    re_first,
    sitemap_urls,
    strip_jsonp,
    table_rows,
)
from zerg.http import Fetch, Fetcher, ImpersonateFetch, StreamingFetcher
from zerg.jsl import (
    clearance_from_step1_html,
    clearance_from_step2_html,
    process_jsl_html,
    solve_go_clearance,
)
from zerg.media import MediaPipeline, media
from zerg.models import (
    REASON_CALLBACK,
    REASON_DOWNLOAD,
    REASON_ERRBACK,
    REASON_HTTP,
    REASON_PARSE,
    REASON_YIELD,
    CrawlError,
    DownloadError,
    Failure,
    Item,
    MediaItem,
    Request,
    Response,
    Stats,
    ZergError,
)
from zerg.parser import Parser
from zerg.pipeline import Pipeline, csv_pipe, jsonl, print_pipe, require_keys
from zerg.registry import discover, get
from zerg.scheduler import Scheduler
from zerg.spider import Spider
from zerg.store import cap_items, gc, usage
from zerg.util import (
    absolute_url,
    absolute_urls,
    detect_waf,
    detect_waf_response,
    form_body,
    paginate,
    parse_link_header,
    rate_limit_headers,
)

core = (
    "Request",
    "Response",
    "Spider",
    "crawl",
    "jsonl",
    "media",
    "Fetch",
    "Failure",
)

__all__ = [
    *core,
    "crawl_many",
    "discover",
    "get",
    "Pipeline",
    "csv_pipe",
    "print_pipe",
    "require_keys",
    "MediaPipeline",
    "absolute_url",
    "absolute_urls",
    "paginate",
    "parse_link_header",
    "detect_waf",
    "detect_waf_response",
    "embedded_json",
    "feed_items",
    "json_ld",
    "strip_jsonp",
    "form_body",
    "rate_limit_headers",
    "re_first",
    "sitemap_urls",
    "table_rows",
    "Item",
    "MediaItem",
    "ZergError",
    "CrawlError",
    "DownloadError",
    "REASON_DOWNLOAD",
    "REASON_HTTP",
    "REASON_PARSE",
    "REASON_CALLBACK",
    "REASON_YIELD",
    "REASON_ERRBACK",
    "Engine",
    "CrawlObserver",
    "Fetcher",
    "StreamingFetcher",
    "Scheduler",
    "Stats",
    "Parser",
    "ImpersonateFetch",
    "gc",
    "usage",
    "cap_items",
    "solve_go_clearance",
    "clearance_from_step1_html",
    "clearance_from_step2_html",
    "process_jsl_html",
    "core",
]

__version__ = "0.2.0"
