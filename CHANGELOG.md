# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
