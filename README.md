<p align="center">
  <img src="assets/logo.png" alt="zerg" width="140"/>
</p>

<h1 align="center">zerg</h1>

<p align="center">
  <b>small, sharp, and hungry.</b><br/>
  异虫，倾巢而出。
</p>

## Install

```bash
uv sync
uv sync --extra impersonate
```

## Usage

```python
import asyncio
from zerg import Spider, crawl, jsonl

class MySpider(Spider):
    name = "mysite"
    start_urls = ["https://example.com"]
    concurrency = 8
    allowed_domains = ["example.com"]
    max_depth = 2

    async def parse(self, response):
        for href in response.links("a.item"):
            yield response.follow(href, callback=self.parse_detail)

    async def parse_detail(self, response):
        yield {"title": response.css("h1"), "url": response.url}

asyncio.run(crawl(MySpider, pipelines=[jsonl()]))
```

```bash
uv run python examples/hackernews.py
```

结果写入 `data/<name>/items.jsonl`。

## How it works

```text
Spider.start()
    └─ Request ──▶ Scheduler ──▶ Worker × concurrency
                                      │
                                   Fetch
                                      │
                                   Response
                                      │
                              parse / callback
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
                 Request                              dict
               (继续爬)                           (进 pipeline)
```

| 组件 | 文件 | 作用 |
|------|------|------|
| `Spider` | `spider.py` | 站点逻辑 |
| `Engine` / `crawl()` | `engine.py` | 调度运行 |
| `Scheduler` | `scheduler.py` | 队列、去重、域名/深度过滤 |
| `Fetch` | `http.py` | HTTP 下载（可选 TLS 伪装） |
| `Response` / `Parser` | `models.py` / `parser.py` | 解析页面 |
| `Pipeline` | `pipeline.py` | 写结果 |
| `Stats` / `Failure` | `models.py` | 统计与错误 |

`concurrency` 是唯一并发旋钮；连接池与之对齐。

## API

```python
from zerg import Spider, Request, Response, Failure, crawl, jsonl, media, Fetch
```

```python
response.css("h1")
response.links("a.item")
response.follow(href, callback=self.parse_detail)
response.extract_all(".card", {"title": "h2", "url": ("a", "href")})
```

callback 只 yield `Request` 或 `dict`。
