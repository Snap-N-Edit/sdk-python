# Changelog

All notable changes to the snapnedit Python SDK. This project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-09-13

### Added

- `get_usage()` on both clients (`GET /usage`): jobs, credits, cache hits, deliveries and
  embedded-editor sessions over a date range, bucketed by `day`, `key`, `origin`,
  `operation` or `source`, with `key_id` / `origin` / `operation` / `source` filters.
  `start` and `end` (the wire's `from` and `to`) accept an ISO string, a `date` or a
  `datetime`.
- `UsageReport`, `UsageRange`, `UsageTotals`, `UsageSeriesPoint`, `UsageKeyRow`,
  `UsageGroupBy`, `UsageSource`, `UsageInstant` and `USAGE_UNATTRIBUTED`.
- `credit_cost`, `cached` and `delivery_only` on `JobView` / `CreateJobResult` (and
  `credit_cost` / `cached` on `RunResult`), read from the job responses.
- `usage.query` in the conformance suite (25 scenarios).

### Changed

- `CreateJobResult.cached` is now the api's own `cached` flag, falling back to "the api
  answered 200" for an older deployment. It was a property; it is now a field, with the
  same meaning.

## [0.1.0] — 2026-09-13

First release.

### Added

- `Snapnedit` (sync) and `AsyncSnapnedit` (async) clients over one shared request layer.
- `run()` — upload, create, poll and download in one call, with `bytes` / path / file
  object / `UrlInput` inputs and optional `mask`.
- `upload()`, `create_job()`, `get_job()`, `wait_for_job()`, `download_result()`,
  `list_operations()`.
- Bring your own storage: `UrlInput`, `PresignedPutDestination`, `SavedDestination`, and
  the `destination=None` opt-out distinct from omitting the argument.
- Saved storage destinations: `destinations.list/create/update/delete/test/presign_upload`.
- Embed credentials: `embed.create_session()` and `embed.create_token()`.
- Declarative designs: `designs.create()`, `designs.create_pages()`, `designs.render()`.
- `Webhooks.verify()` / `Webhooks.construct_event()` for `X-Snapnedit-Signature`.
- `SnapneditError` with the closed `ErrorCode` set, `SnapneditTimeoutError`,
  `SnapneditSignatureError`.
- Retries with exponential backoff and jitter on 429/5xx/network for idempotent calls.
- Typed throughout (`py.typed`), and a pytest suite covering every scenario in the
  monorepo's language-neutral SDK conformance contract.
