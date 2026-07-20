"""zerg — small, sharp, and hungry."""

from __future__ import annotations

from zerg.engine import Engine, crawl, crawl_many
from zerg.extract import (
    embedded_json,
    feed_items,
    json_ld,
    re_first,
    sitemap_urls,
    table_rows,
)
from zerg.http import Fetch, Fetcher, ImpersonateFetch
from zerg.media import MediaPipeline, media
from zerg.models import (
    REASON_CALLBACK,
    REASON_DOWNLOAD,
    REASON_ERRBACK,
    REASON_HTTP,
    REASON_PARSE,
    REASON_YIELD,
    CrawlError,
    Failure,
    Item,
    MediaItem,
    Request,
    Response,
    Stats,
    ZergError,
)
from zerg.parser import Parser
from zerg.pipeline import Pipeline, csv_pipe, jsonl, print_pipe
from zerg.registry import discover, get
from zerg.scheduler import Scheduler
from zerg.spider import Spider
from zerg.util import absolute_url, absolute_urls, paginate, parse_link_header

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
    "MediaPipeline",
    "absolute_url",
    "absolute_urls",
    "paginate",
    "parse_link_header",
    "embedded_json",
    "feed_items",
    "json_ld",
    "re_first",
    "sitemap_urls",
    "table_rows",
    "Item",
    "MediaItem",
    "ZergError",
    "CrawlError",
    "REASON_DOWNLOAD",
    "REASON_HTTP",
    "REASON_PARSE",
    "REASON_CALLBACK",
    "REASON_YIELD",
    "REASON_ERRBACK",
    "Engine",
    "Fetcher",
    "Scheduler",
    "Stats",
    "Parser",
    "ImpersonateFetch",
    "core",
]

__version__ = "0.1.0"
