# snapnedit — Python SDK

The official Python client for the [snapnedit](https://snapnedit.com) AI photo-editing API:
upload an image, run an operation, collect the result — or point the API at your own bucket
and never move the bytes through your process at all.

Sync and async clients, fully typed (`py.typed`, `mypy --strict` clean), one runtime
dependency (`httpx`), Python 3.10+.

```sh
pip install snapnedit
```

## Quick start

```python
from snapnedit import Snapnedit

with Snapnedit(api_key="sk_live_…") as snap:
    result = snap.run("remove-background", "cat.jpg")
    with open("cat-cutout.png", "wb") as out:
        out.write(result.output or b"")

    upscaled = snap.run("upscale", result.output, {"factor": "2"}, mime="image/png")
    print(upscaled.job_id, upscaled.mime, len(upscaled.output or b""))
```

`run()` uploads the input, creates the job, polls it to completion and downloads the result.
`input` can be `bytes`, a path (`str` / `Path`), any open binary file, or a
`UrlInput` the server fetches itself. `api_key` defaults to `$SNAPNEDIT_API_KEY`, and
`base_url` to `$SNAPNEDIT_BASE_URL` (falling back to `https://snapnedit.com/api`).

Prefer the pieces? `snap.upload(...)`, `snap.create_job(...)`, `snap.get_job(id)`,
`snap.wait_for_job(id)` and `snap.download_result(job)` are all public.

Every job — created, polled or run — also reports what it cost: `credit_cost` (0 for a free
operation, a cache hit or an unmetered caller), `cached` (served from the result cache
rather than by running a model) and, on `create_job()` / `get_job()`, `delivery_only` (a job
that exists only to deliver an already-cached result somewhere new). A cache hit records its
own job row, so its `job_id` is a **new** id sharing the earlier job's `output_asset_id`.

## Async

```python
import asyncio
from snapnedit import AsyncSnapnedit

async def main() -> None:
    async with AsyncSnapnedit(api_key="sk_live_…") as snap:
        jobs = [snap.run("remove-background", path) for path in ("a.jpg", "b.jpg")]
        for result in await asyncio.gather(*jobs):
            print(result.job_id, len(result.output or b""))

asyncio.run(main())
```

The async client is the same surface over the same request layer — every method, argument
and return type matches `Snapnedit`, awaited.

## Bring your own storage

Read the input from a URL you signed, write the result straight into your bucket. The bytes
never touch this process.

```python
from snapnedit import PresignedPutDestination, Snapnedit, UrlInput

snap = Snapnedit(api_key="sk_live_…")

result = snap.run(
    "remove-background",
    UrlInput("https://my-bucket.s3.amazonaws.com/in.png?X-Amz-Signature=…"),
    destination=PresignedPutDestination(
        url="https://my-bucket.s3.amazonaws.com/out.png?X-Amz-Signature=…",
        headers={"content-type": "image/png"},
    ),
)

assert result.downloaded is False           # delivered, not pulled back through you
assert result.delivery is not None and result.delivery.status == "delivered"
```

A URL input requires an API key (anonymous callers get `403 forbidden`), must be `https`,
and a fetch that fails is a terminal `input_fetch_failed` job with the credits refunded.
Pass `download=True` to get the bytes as well as the delivery.

### Saved destinations

Save a bucket once and name it by id — the server holds the credentials and signs the
upload, so nothing about your bucket has to be in this process.

```python
from snapnedit import SavedDestination, Snapnedit

snap = Snapnedit(api_key="sk_live_…")

dest = snap.destinations.create(
    name="exports",
    provider="cloudflare-r2",
    bucket="my-bucket",
    account_id="abc123…",                  # r2
    access_key_id="AKIA…",
    secret_access_key="…",                 # encrypted at rest, never echoed back
    key_prefix="snapnedit/",
    is_default=False,
)

print(snap.destinations.test(dest.id).ok)  # a real write-then-delete probe

result = snap.run("upscale", "photo.jpg", {"factor": "4"},
                  destination=SavedDestination(dest.id))
print(result.delivery.bucket, result.delivery.key)   # where it landed

signed = snap.destinations.presign_upload(dest.id, ext="png", content_type="image/png")
# PUT your own bytes to signed.url with signed.headers, verbatim

snap.destinations.delete(dest.id)
```

One destination per account can be the **default**, applied automatically to any job that
names none. `destination=None` opts a single job out of it; omitting the argument entirely
means "apply my default". Those are different requests, and the SDK keeps them different.

## Usage and credits

`snap.get_usage()` reports what the account has actually spent — jobs, credits, cache hits,
deliveries and embedded-editor sessions — bucketed along one dimension at a time.

```python
report = snap.get_usage(group_by="operation")          # last 30 days by default

print(report.range.start, report.range.end)            # the resolved window, ISO instants
print(report.totals.jobs, report.totals.credits)       # 41 68
print(report.totals.cache_hits, report.totals.free)    # requests that ran no model / cost nothing

for point in report.series:                            # busiest first (oldest first for "day")
    print(point.key, point.label, point.jobs, point.credits)

for key in report.keys:                                # today's spend against each key's cap
    print(key.name, key.kind, key.used_today, "/", key.daily_credit_limit or "∞")
```

```python
from datetime import date

september = snap.get_usage(
    start=date(2026, 9, 1),            # the wire's `from`; a `datetime` works too
    end=date(2026, 9, 30),             # the wire's `to`, inclusive
    group_by="day",                    # "day" | "key" | "origin" | "operation" | "source"
    source="embed",                    # plus keyId / origin / operation filters
)
print(september.point("2026-09-13"))   # one bucket by key, or None
```

`group_by="day"` is zero-filled across the whole range so a chart has a point per day;
every other grouping comes back busiest first. `jobs` counts REQUESTS — a cache hit is a
job too — while `credits` is what was actually debited, so a free operation, a cache hit
and a website job all contribute `0`. A range wider than 366 days is refused with
`invalid_input`.

An embed token may read its own numbers: the api scopes the report to that token's key and
omits the key roster, which the SDK normalizes to `report.keys == []`.

## Webhooks

```python
from snapnedit import Webhooks

@app.post("/hooks/snapnedit")
def hook(request):
    raw = request.get_data()                       # RAW bytes, before any JSON parse
    sig = request.headers["X-Snapnedit-Signature"]
    if not Webhooks.verify(raw, sig, ENDPOINT_SECRET, tolerance_seconds=300):
        return "bad signature", 400
    event = Webhooks.construct_event(raw, sig, ENDPOINT_SECRET)
    if event.type == "job.succeeded":
        print(event.data.job_id, event.data.output_asset_id, event.data.download)
    return "", 204
```

`verify()` returns `False` — it never raises — for a malformed header, a bad signature or a
stale timestamp, so every falsy result can be handled the same way. `construct_event()`
verifies and then parses, raising `SnapneditSignatureError` if verification fails.

## Errors

Every failure is a `SnapneditError` carrying a machine-readable `code`, the human `message`
and the HTTP `status`. Branch on the code; messages change.

```python
from snapnedit import ErrorCode, SnapneditError, SnapneditTimeoutError

try:
    result = snap.run("upscale", "photo.jpg", {"factor": "3"})
except SnapneditTimeoutError:
    ...                                       # polling ran out of time
except SnapneditError as err:
    if err.code is ErrorCode.PAYMENT_REQUIRED:
        ...                                   # out of credits (HTTP 402)
    elif err.code == ErrorCode.INVALID_INPUT:
        print(err.status, err.message)        # 400, "params.factor: …"
```

The full set: `invalid_input`, `unsupported_mime`, `too_large`, `not_found`,
`input_fetch_failed`, `provider_failed`, `provider_exhausted`, `rate_limited`,
`bot_check_failed`, `unauthorized`, `forbidden`, `payment_required`, `internal`.
A job that finishes in `failed` raises the same error from `run()`, with the job's
`error_code` and `status=200`.

Idempotent calls (`GET`, `PUT`, `DELETE`) are retried on `429`, `5xx` and network errors —
exponential backoff with jitter, honoring `Retry-After`. Tune with
`Snapnedit(..., max_retries=2)`; `POST /jobs` is never retried automatically.

## Operations

`snap.list_operations()` returns the live catalog (accepted mime types, JSON Schema for the
params, `requires_mask`). The static table, with credit costs — `0` is free, a cache hit is
never billed, and a terminal failure is refunded:

| Operation | Credits | Mask | Operation | Credits | Mask |
| --- | --- | --- | --- | --- | --- |
| `remove-background` | 1 | | `ai-denoise` | 1 | |
| `upscale` | 2 | | `replace-sky` | 2 | |
| `unblur` | 1 | | `relight` | 2 | |
| `colorize` | 1 | | `replace-background` | 2 | |
| `style-transfer` | 2 | | `strip-metadata` | 1 | |
| `retouch` | 1 | | `auto-remove-watermark` | 2 | |
| `beautify` | 1 | | `resize-image` | 0 | |
| `magic-eraser` | 2 | ✓ | `generative-fill` | 3 | ✓ |
| `remove-watermark` | 2 | ✓ | | | |

A mask-guided operation needs a second image:

```python
result = snap.run("magic-eraser", "photo.jpg", mask="mask.png")
```

Also available: `snapnedit.OPERATION_IDS`, `snapnedit.CREDIT_COSTS`,
`snapnedit.credit_cost(op)` and `snapnedit.requires_mask(op)`.

## Embedded editor

```python
token = snap.embed.create_token(ttl_seconds=600, max_credits=25,
                                allowed_operations=["remove-background"])
# hand token.token to your front end; it authenticates as the account, scoped
```

`snap.embed.create_session(publishable_key, host_origin)` is the browser-side exchange of a
`pk_…` key for the same short-lived token, allowed only from the key's registered origins.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check
.venv/bin/mypy --strict src
.venv/bin/pytest -m "not conformance"     # unit tests, fake transport
.venv/bin/pytest -m conformance           # against the monorepo's conformance server
```

The conformance suite implements every scenario in the monorepo's
`test/conformance/scenarios.json` against a real, locally spawned api + worker
(`node scripts/conformance-server.mjs`). Point it at a checkout with
`SNAPNEDIT_REPO_ROOT=/path/to/snapnedit`; without one, those tests skip.

---

Developed in the snapnedit monorepo, mirrored here; issues welcome.
API reference: <https://snapnedit.com/docs/api-reference>. MIT licensed.
