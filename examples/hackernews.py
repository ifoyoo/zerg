"""Hacker News front page example.

    uv run python examples/hackernews.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from zerg import Spider, crawl, jsonl, print_pipe

ROOT = Path(__file__).resolve().parent.parent


class HackerNewsSpider(Spider):
    name = "hackernews"
    start_urls = ["https://news.ycombinator.com"]
    concurrency = 3
    delay = 0.3
    allowed_domains = ["news.ycombinator.com"]
    max_depth = 0

    async def parse(self, response):
        items = response.extract_all(
            "tr.athing",
            {
                "rank": "span.rank",
                "title": "span.titleline > a",
                "link": ("span.titleline > a", "href"),
            },
        )
        scores = response.css_all("span.score")
        for i, item in enumerate(items):
            item["score"] = scores[i] if i < len(scores) else ""
            yield item


async def main():
    stats = await crawl(
        HackerNewsSpider,
        pipelines=[jsonl(), print_pipe(max_items=1)],
        data_dir=ROOT / "data" / "hackernews",
    )
    print(
        f"\n✅ Done: {stats['items']} items, {stats['requests']} requests "
        f"→ {stats['data_dir']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
