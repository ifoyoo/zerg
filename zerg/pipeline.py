"""Item pipelines."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import orjson

from zerg.media import MediaPipeline, media  # noqa: F401

_JSONL_BUF_ITEMS = 64
_JSONL_BUF_BYTES = 64 * 1024


@runtime_checkable
class ItemProcessor(Protocol):
    async def process_item(
        self, item: dict[str, Any], spider: Any
    ) -> dict[str, Any] | None: ...


class Pipeline:
    """Ordered item processors."""

    def __init__(self, *processors: Any):
        self._processors = list(processors)

    def add(self, processor: Any) -> Pipeline:
        self._processors.append(processor)
        return self

    async def open(self, spider: Any) -> None:
        for p in self._processors:
            fn = getattr(p, "open", None)
            if fn is None:
                continue
            result = fn(spider)
            if hasattr(result, "__await__"):
                await result

    async def process(self, item: dict[str, Any], spider: Any) -> dict[str, Any] | None:
        for p in self._processors:
            process_item = getattr(p, "process_item", None)
            if process_item is not None:
                result = process_item(item, spider)
                if hasattr(result, "__await__"):
                    result = await result
                if result is None:
                    return None
                item = result
            elif callable(p):
                result = p(item)
                if hasattr(result, "__await__"):
                    await result
        return item

    async def close(self, spider: Any) -> None:
        for p in self._processors:
            fn = getattr(p, "close", None)
            if fn is None:
                continue
            result = fn(spider)
            if hasattr(result, "__await__"):
                await result


def _default_data_path(spider: Any, filename: str) -> Path:
    base = getattr(spider, "data_dir", None) or Path("data") / getattr(
        spider, "name", "spider"
    )
    return Path(base) / filename


class JsonlPipeline:
    """Buffered JSONL writer."""

    def __init__(
        self,
        path: str | Path | None = None,
        mode: str = "w",
        *,
        buf_items: int = _JSONL_BUF_ITEMS,
        buf_bytes: int = _JSONL_BUF_BYTES,
    ):
        if mode not in {"w", "a"}:
            mode = "w"
        self._path = Path(path) if path else None
        self._mode = mode + "b"
        self._buf_items = max(1, buf_items)
        self._buf_bytes = max(1024, buf_bytes)
        self._file: Any = None
        self._buf: bytearray = bytearray()
        self._n: int = 0

    async def open(self, spider: Any) -> None:
        path = self._path or _default_data_path(spider, "items.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._file = open(path, self._mode)
        self._buf = bytearray()
        self._n = 0

    def _flush(self) -> None:
        if self._file is not None and self._buf:
            self._file.write(self._buf)
            self._buf.clear()
            self._n = 0

    async def process_item(self, item: dict[str, Any], spider: Any) -> dict[str, Any]:
        assert self._file is not None
        line = orjson.dumps(item) + b"\n"
        self._buf.extend(line)
        self._n += 1
        if self._n >= self._buf_items or len(self._buf) >= self._buf_bytes:
            self._flush()
        return item

    async def close(self, spider: Any) -> None:
        if self._file is not None:
            self._flush()
            self._file.close()
            self._file = None


class CsvPipeline:
    """CSV writer with fixed columns."""

    def __init__(
        self,
        columns: list[str],
        path: str | Path | None = None,
        mode: str = "w",
    ):
        self.columns = columns
        self._path = Path(path) if path else None
        self._mode = mode if mode in {"w", "a"} else "w"
        self._file: Any = None
        self._writer: Any = None

    async def open(self, spider: Any) -> None:
        path = self._path or _default_data_path(spider, "items.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        new_file = self._mode == "w" or not path.exists() or path.stat().st_size == 0
        self._file = open(path, self._mode, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.columns)
        if new_file or self._file.tell() == 0:
            self._writer.writeheader()

    async def process_item(self, item: dict[str, Any], spider: Any) -> dict[str, Any]:
        assert self._writer is not None
        self._writer.writerow({k: item.get(k, "") for k in self.columns})
        return item

    async def close(self, spider: Any) -> None:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None


class PrintPipeline:
    """Print items as JSON."""

    def __init__(self, max_items: int | None = None):
        self.max_items = max_items
        self._n = 0

    async def process_item(self, item: dict[str, Any], spider: Any) -> dict[str, Any]:
        if self.max_items is not None and self._n >= self.max_items:
            return item
        self._n += 1
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return item


def jsonl(
    path: str | Path | None = None,
    mode: str = "w",
    *,
    buf_items: int = _JSONL_BUF_ITEMS,
    buf_bytes: int = _JSONL_BUF_BYTES,
) -> JsonlPipeline:
    return JsonlPipeline(path, mode=mode, buf_items=buf_items, buf_bytes=buf_bytes)


def csv_pipe(
    columns: list[str], path: str | Path | None = None, mode: str = "w"
) -> CsvPipeline:
    return CsvPipeline(columns, path, mode=mode)


def print_pipe(max_items: int | None = None) -> PrintPipeline:
    return PrintPipeline(max_items=max_items)


class RequireKeys:
    """Drop or fill items missing required keys (shared schema helper)."""

    def __init__(
        self,
        *keys: str,
        drop: bool = True,
        defaults: dict[str, Any] | None = None,
    ):
        self.keys = keys
        self.drop = drop
        self.defaults = dict(defaults or {})
        self.dropped = 0
        self.filled = 0

    async def process_item(
        self, item: dict[str, Any], spider: Any
    ) -> dict[str, Any] | None:
        missing = [k for k in self.keys if k not in item or item.get(k) in (None, "")]
        if not missing:
            return item
        if self.drop and not self.defaults:
            self.dropped += 1
            return None
        out = dict(item)
        for k in missing:
            if k in self.defaults:
                out[k] = self.defaults[k]
                self.filled += 1
            elif self.drop:
                self.dropped += 1
                return None
        return out


def require_keys(
    *keys: str,
    drop: bool = True,
    defaults: dict[str, Any] | None = None,
) -> RequireKeys:
    """Pipeline: ensure item has keys (drop incomplete or fill defaults)."""
    return RequireKeys(*keys, drop=drop, defaults=defaults)
