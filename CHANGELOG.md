# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Spider-supplied values are coerced instead of raising: a `str`
  `meta["depth"]` or `health_error_rate` used to abort the crawl. Depth and the
  health threshold now go through new `util.as_int` / `util.as_float` helpers,
  and `Retry-After` parsing is a digit check with a 30 s cap and an exponential
  fallback.

### Performance

- `CsvPipeline` writes through a plain `csv.writer` with a value list instead of
  building a dict per row (1.14 us -> 0.57 us per row); `JsonlPipeline` lets
  orjson append the newline.
- Card extraction is ~1.5x faster, scheduler admission ~1.35x, engine
  throughput ~1.2x and bounded fan-out ~1.25x (`benchmarks/`, Python 3.14.7,
  Apple Silicon). `Parser.extract_all` now plans per rule: one page-wide query
  when a rule matches about once per row and the rows cover the markup,
  per-row first-match search otherwise. Results are unchanged, nested rows
  included.
- Callback output is streamed without an extra async-generator hop, so a
  callback may return a single value, an iterable, or an async iterator.
- Callback names resolve through a per-crawl memo instead of `getattr` per
  request.
- Response bodies buffer as a chunk list and join once instead of growing and
  copying a `bytearray`.
- Scheduler normalizes allowed domains at construction and enqueues uncontended
  seeds without awaiting.
- `Request.fingerprint` skips `urldefrag` for URLs without a fragment, and
  `_detect_encoding` only scans for `<meta>` charset when a decodable header is
  absent.
- `Parser` no longer retains the HTML string it was built from (its length is
  kept for the extraction planner).

## [0.2.0] - 2026-07-22

### Added

- Bounded request frontiers with observable `queue_peak` and
  `queue_rejected` statistics.
- Shared per-spider token-bucket rate limiting through
  `requests_per_second` and `burst`.
- Structured `DownloadError` failures for timeouts, network errors,
  oversized responses, and custom backend failures.
- Retry, timeout, downloaded-byte, status-count, and queue-peak metrics.
- Incremental response limits and streaming media downloads with atomic files.
- Deterministic scheduler, parser, engine, and fan-out benchmarks.
- Ruff formatting/linting and GitHub Actions for Python 3.12 through 3.14.

### Changed

- `Spider.delay` now spaces logical request starts globally. Prefer the explicit
  `requests_per_second` setting for new spiders.
- The default maximum buffered response body is 10 MiB. Set
  `max_response_bytes = None` to opt out.
- Media byte budgets are enforced while bytes arrive instead of after complete
  bodies have already been buffered.
- TLS verification is enabled by default for the impersonation backend.

### Removed

- Evolution-specific retention modes and storage behavior from the core package.

## [0.1.0] - 2026-07-20

### Added

- Initial async crawl engine, scheduler, HTTP backends, parsers, pipelines,
  media support, storage helpers, and spider discovery.

[0.2.0]: https://github.com/ifoyoo/zerg/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ifoyoo/zerg/releases/tag/v0.1.0
