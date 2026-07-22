<p align="center">
  <img src="assets/zerg-throne.jpg" alt="The Zerg awakens" width="900"/>
</p>

<h1 align="center">zerg</h1>

<p align="center">
  <b>small, sharp, and hungry.</b><br/>
  异虫，倾巢而出。
</p>

> 古老传说中，异虫沉眠于地底，不争鸣，不显形。<br/>
> 当巢群苏醒，万千异虫循同一意志奔赴四方，所过之处，信息尽归虫巢。

`zerg` 是一个轻量、高性能的 Python async 爬虫框架。

它提供并发调度、连接池、请求去重、域名与深度过滤、解析工具、失败处理和数据 pipeline，同时保持核心 API 简单直接。

## Install

要求 Python 3.12+。

```bash
uv sync
```

需要 browser TLS impersonation 时：

```bash
uv sync --extra impersonate
```

## Quick start

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
            yield response.follow(
                href,
                callback=self.parse_detail,
            )

    async def parse_detail(self, response):
        yield {
            "title": response.css("h1"),
            "url": response.url,
        }


asyncio.run(
    crawl(
        MySpider,
        pipelines=[jsonl()],
    )
)
```

结果默认写入：

```text
data/<spider.name>/items.jsonl
```

也可以运行内置示例：

```bash
uv run python examples/hackernews.py
```

## How it works

```text
Spider.start()
    │
    ▼
 Request ──▶ Scheduler ──▶ Worker × concurrency
                 │                   │
                 │                 Fetch
                 │                   │
                 │                Response
                 │                   │
                 │            callback / errback
                 │                   │
                 └──── Request ◀─────┤
                                     │
                                    dict
                                     │
                                     ▼
                                  Pipeline
```

| 组件 | 作用 |
|------|------|
| `Spider` | 定义站点入口、并发参数和解析逻辑 |
| `Engine` | 管理单个 spider 的完整运行周期 |
| `Scheduler` | FIFO 调度、请求去重、域名和深度过滤 |
| `Fetch` | 基于 httpx 的 async HTTP/2 下载器 |
| `Response` | 封装响应内容、解析器和 follow 操作 |
| `Pipeline` | 依次处理、过滤或持久化 item |
| `CrawlObserver` | 接收 metrics、tracing 和进度事件 |
| `crawl_many()` | 有界并发运行多个 spiders |

每个 spider 的 `concurrency` 同时决定 worker 数量和默认连接池大小，是单个 spider 的主要并发参数。

## Spider

一个 spider 通常只需要定义入口和 `parse()`：

```python
from zerg import Spider


class NewsSpider(Spider):
    name = "news"
    start_urls = ["https://example.com/news"]

    concurrency = 16
    delay = 0
    timeout = 30
    max_retries = 3

    allowed_domains = ["example.com"]
    max_depth = 2

    async def parse(self, response):
        yield {
            "title": response.css("h1"),
            "url": response.url,
        }
```

callback 可以 yield：

- `Request`：继续调度新请求
- `dict`：交给 item pipeline
- `None`：不产生结果

callback 支持 async generator、coroutine 和普通 iterable。

## Request and Response

创建请求：

```python
from zerg import Request

yield Request(
    "https://example.com/api",
    method="POST",
    headers={"accept": "application/json"},
    body=b'{"page":1}',
    callback=self.parse_api,
    errback=self.handle_error,
    meta={"page": 1},
)
```

从当前响应继续抓取：

```python
yield response.follow(
    "/detail/1",
    callback=self.parse_detail,
    meta={"source": "list"},
)
```

常用解析方法：

```python
response.css("h1")
response.css_all(".item")
response.css_attr("a.next", "href")
response.links("a.item")
response.json()

response.extract({
    "title": "h1",
    "author": ".author",
})

response.extract_all(
    ".card",
    {
        "title": "h2",
        "url": ("a", "href"),
    },
)
```

## Pipelines

写入 JSONL：

```python
from zerg import crawl, jsonl

stats = await crawl(
    MySpider,
    pipelines=[jsonl()],
)
```

组合多个 pipeline：

```python
from zerg import jsonl, require_keys

stats = await crawl(
    MySpider,
    pipelines=[
        require_keys("title", "url"),
        jsonl(mode="w"),
    ],
)
```

内置 pipeline：

| Pipeline | 作用 |
|----------|------|
| `jsonl()` | Buffered JSONL 写入 |
| `csv_pipe()` | 写入固定字段的 CSV |
| `print_pipe()` | 输出 item 到终端 |
| `require_keys()` | 校验、补全或过滤字段 |
| `media()` | 并发下载图片或文件 |
| `cap_items()` | 限制保留的 item 数量 |

自定义 pipeline：

```python
class NormalizeTitle:
    async def process_item(self, item, spider):
        item = dict(item)
        item["title"] = item.get("title", "").strip()
        return item
```

返回 `None` 可以丢弃当前 item。

## Error handling

HTTP 错误、下载失败、callback 异常等会转换为 `Failure`：

```python
from zerg import Failure


class MySpider(Spider):
    async def errback(self, failure: Failure):
        print(
            failure.reason,
            failure.status,
            failure.url,
            failure.exception,
        )
```

也可以为单个请求指定 errback：

```python
yield response.follow(
    href,
    callback=self.parse_detail,
    errback=self.handle_detail_error,
)
```

运行结束后会返回统计信息：

```python
stats = await crawl(MySpider)
print(stats)
```

主要字段包括：

```text
spider
requests
items
errors
filtered
challenges
duration_s
error_rate
healthy
by_reason
data_dir
```

## Multiple spiders

使用 `crawl_many()` 有界并发运行多个 spiders：

```python
from zerg import crawl_many, jsonl

results = await crawl_many(
    [NewsSpider, ApiSpider, FeedSpider],
    max_spiders=3,
    pipelines_factory=lambda: [jsonl()],
    data_dir="data",
)
```

`max_spiders` 控制同时运行的 spider 数量；每个 spider 内部仍使用自己的 `concurrency` 控制 request workers。

单个 spider 抛出异常不会终止其他 spiders，其异常会记录在对应结果中。

本地 spider package 也可以通过 runner 执行：

```bash
uv run python run.py --list
uv run python run.py my_spider
uv run python run.py --tag rss
uv run python run.py --all --max-spiders 3
```

## Observability

`CrawlObserver` 是可选的事件接口，可用于 metrics、tracing 或进度展示：

```python
class MetricsObserver:
    def on_start(self, spider):
        print(f"start: {spider.name}")

    def on_response(self, response):
        print(response.status, response.url)

    def on_failure(self, failure):
        print(failure.reason, failure.url)

    def on_item(self, item):
        pass

    def on_request(self, request):
        pass

    def on_finish(self, spider, stats):
        print(f"finish: {spider.name}", stats)


stats = await crawl(
    MySpider,
    observers=[MetricsObserver()],
)
```

Observer 异常会被隔离，不会中断 crawl。

## HTTP backends

默认下载器基于 `httpx`，支持：

- async connection pooling
- HTTP/2
- timeout
- redirect
- retry and backoff
- proxy
- per-request headers

需要 browser TLS fingerprint 时：

```python
class ProtectedSpider(Spider):
    use_impersonate = True
    impersonate = "chrome124"
```

该功能需要安装 `impersonate` extra。

## Design

`zerg` 的核心原则：

- async-first
- bounded concurrency
- 少量明确的并发参数
- spider、scheduler、fetcher、pipeline 相互独立
- 高吞吐热路径保持简单
- 站点逻辑留在 spider，不进入 framework core
- 通用能力经过多个场景验证后再进入公共 API

`zerg` 只负责爬虫框架本身。站点 spiders、运行数据、部署策略和业务 schema 属于使用框架的 application。
