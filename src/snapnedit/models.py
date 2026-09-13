"""Typed views of every shape the snapnedit api speaks.

Frozen dataclasses with `from_wire` parsers, plus the small input types a
caller constructs (`UrlInput`, `PresignedPutDestination`, `SavedDestination`).
Field names are snake_case; the wire is camelCase and the conversion happens
here and in `_endpoints`, never in caller code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from ._wire import (
    malformed,
    opt_bool,
    opt_float,
    opt_int,
    opt_mapping,
    opt_str,
    req_bool,
    req_int,
    req_list,
    req_mapping,
    req_str,
)
from .errors import ErrorCode

__all__ = [
    "UNSET",
    "USAGE_UNATTRIBUTED",
    "CreateJobResult",
    "Destination",
    "DestinationPresign",
    "DestinationTestResult",
    "EmbedToken",
    "JobDelivery",
    "JobDestinationSummary",
    "JobInput",
    "JobStatus",
    "JobView",
    "OperationMetadata",
    "PresignedPutDestination",
    "RunResult",
    "SavedDestination",
    "SignedUrl",
    "StorageDestination",
    "StorageDestinationTest",
    "UnsetType",
    "UploadResult",
    "UrlInput",
    "UsageGroupBy",
    "UsageInstant",
    "UsageKeyRow",
    "UsageRange",
    "UsageReport",
    "UsageSeriesPoint",
    "UsageSource",
    "UsageTotals",
]

JobState = Literal["queued", "processing", "succeeded", "failed", "canceled"]
DeliveryStatus = Literal["pending", "delivered", "failed"]
StorageProvider = Literal["aws-s3", "cloudflare-r2", "backblaze-b2", "s3-compatible"]
DestinationExt = Literal["png", "jpg", "webp", "avif", "svg", "pdf", "gif"]
UsageGroupBy = Literal["day", "key", "origin", "operation", "source"]
"""The dimension `GET /usage` buckets its series along."""
UsageSource = Literal["api", "embed", "session", "anonymous"]
"""How a job was requested, as the api persists it. `session` and `anonymous` are never billed."""

UsageInstant = str | date | datetime
"""What `get_usage()` accepts for a range end: an ISO string, a `date` or a `datetime`.

A `date` is sent as `YYYY-MM-DD` (which the api reads as the whole UTC day); a
`datetime` is normalized to UTC and sent as an instant, a naive one being taken
as already UTC.
"""

USAGE_UNATTRIBUTED = "none"
"""The series key for a bucket with no value for the grouped dimension.

A website job has no api key, an `sk_` job has no origin — both land here
rather than being dropped from the series.
"""

_TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "canceled"})


class UnsetType:
    """Sentinel type for "the caller said nothing" — distinct from an explicit `None`."""

    _instance: UnsetType | None = None

    def __new__(cls) -> UnsetType:
        """Return the single shared instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        """Return `False` — an unset value is falsy."""
        return False

    def __repr__(self) -> str:
        """Render as `UNSET`."""
        return "UNSET"


UNSET = UnsetType()
"""Default for `destination=` and for patch fields: omit the key entirely.

`destination=None` is NOT the same thing — it explicitly opts a job out of the
account's default destination, while omitting it means "apply my default".
"""


# --------------------------------------------------------------------------
# inputs the caller constructs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlInput:
    """Bring your own storage, input half: an image the SERVER fetches.

    The bytes never pass through your process and no upload happens. https
    only; private, loopback and metadata addresses are refused, redirects are
    not followed, and the fetch is capped at 30s and the upload size ceiling.
    A fetch that fails is a terminal `input_fetch_failed` job (credits
    refunded). Requires an api key or embed token.
    """

    url: str


@dataclass(frozen=True)
class PresignedPutDestination:
    """Bring your own storage, output half: a PUT url you signed yourself.

    `headers` are the ones your signature covers — only `content-type`,
    `cache-control`, `content-disposition` and `x-amz-*` / `x-goog-*` /
    `x-ms-*` are accepted (16 at most).
    """

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    type: Literal["presigned-put"] = "presigned-put"

    def to_wire(self) -> dict[str, Any]:
        """Render the `destination` object for `POST /jobs`."""
        body: dict[str, Any] = {"type": "presigned-put", "url": self.url}
        if self.headers:
            body["headers"] = dict(self.headers)
        return body


@dataclass(frozen=True)
class SavedDestination:
    """One of your account's saved storage destinations, named by id.

    The server holds the credentials and signs the upload, so nothing about
    your bucket needs to be in this process.
    """

    id: str
    type: Literal["saved"] = "saved"

    def to_wire(self) -> dict[str, Any]:
        """Render the `destination` object for `POST /jobs`."""
        return {"type": "saved", "id": self.id}


Destination = PresignedPutDestination | SavedDestination | Mapping[str, Any]
"""What a job's `destination=` accepts — or a raw mapping in the wire shape."""


def destination_to_wire(destination: Destination | None) -> Any:
    """Normalize a `destination=` value (including an explicit `None`) to JSON."""
    if destination is None:
        return None
    if isinstance(destination, (PresignedPutDestination, SavedDestination)):
        return destination.to_wire()
    return dict(destination)


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedUrl:
    """A short-lived url the api minted, plus its ISO-8601 expiry.

    `url` may be a PATH (`/_local/…?exp&sig`) rather than an absolute url; the
    client resolves it against `base_url` for you.
    """

    url: str
    expires_at: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> SignedUrl:
        """Parse a `{ url, expiresAt }` object."""
        data = opt_mapping(value)
        if data is None:
            raise malformed(source, "expected a signed-url object")
        return cls(url=req_str(data, "url", source), expires_at=req_str(data, "expiresAt", source))


@dataclass(frozen=True)
class JobInput:
    """Which kind of input a job had. The input url itself is never echoed back."""

    kind: Literal["asset", "url"]


@dataclass(frozen=True)
class JobDestinationSummary:
    """A job's destination as the api reports it — never the url or the headers.

    `{ type: 'presigned-put' }` for a url you signed, or
    `{ type: 'saved', id, name }` for a saved destination (`name` is absent
    only if the destination has since been deleted).
    """

    type: Literal["presigned-put", "saved"]
    id: str | None = None
    name: str | None = None

    @classmethod
    def from_wire(cls, value: object) -> JobDestinationSummary | None:
        """Parse a destination summary. Anything unrecognized reads as "no destination"."""
        data = opt_mapping(value)
        if data is None:
            return None
        if data.get("type") == "presigned-put":
            return cls(type="presigned-put")
        if data.get("type") == "saved" and isinstance(data.get("id"), str):
            return cls(type="saved", id=str(data["id"]), name=opt_str(data, "name"))
        return None


@dataclass(frozen=True)
class JobDelivery:
    """How the PUT into your bucket went.

    A `succeeded` job with `status == "failed"` here is a real combination, not
    a contradiction: the job ran, only the delivery did not — the result is
    still downloadable.
    """

    status: DeliveryStatus
    attempts: int
    delivered_at: str | None = None
    status_code: int | None = None
    error: str | None = None
    key: str | None = None
    """Saved destinations only: the object key the server generated."""
    bucket: str | None = None
    local_copy_deleted: bool | None = None
    """True when `deleteAfterDelivery` removed our copy — `download` is then `None`."""

    @classmethod
    def from_wire(cls, value: object) -> JobDelivery | None:
        """Parse a delivery record. An unknown `status` degrades to `pending`."""
        data = opt_mapping(value)
        if data is None:
            return None
        raw = data.get("status")
        status: DeliveryStatus = raw if raw in ("pending", "delivered", "failed") else "pending"
        return cls(
            status=status,
            attempts=opt_int(data, "attempts") or 0,
            delivered_at=opt_str(data, "deliveredAt"),
            status_code=opt_int(data, "statusCode"),
            error=opt_str(data, "error"),
            key=opt_str(data, "key"),
            bucket=opt_str(data, "bucket"),
            local_copy_deleted=opt_bool(data, "localCopyDeleted"),
        )


@dataclass(frozen=True)
class JobStatus:
    """A job's status, flattened across every state.

    Which fields are populated follows `state`: `processing` has `started_at`,
    `succeeded` has `output_asset_id` and (unless the local copy was deleted
    after delivery) `download`, `failed` has `error_code` and `message`.
    """

    state: JobState
    output_asset_id: str | None = None
    download: SignedUrl | None = None
    started_at: str | None = None
    error_code: ErrorCode | None = None
    message: str | None = None

    @property
    def terminal(self) -> bool:
        """True once the job can no longer change."""
        return self.state in _TERMINAL

    @property
    def succeeded(self) -> bool:
        """True for a job that finished successfully."""
        return self.state == "succeeded"

    @property
    def failed(self) -> bool:
        """True for a job that finished in `failed`."""
        return self.state == "failed"

    @classmethod
    def from_wire(cls, value: object, source: str) -> JobStatus:
        """Parse the status union (`POST /jobs`'s `status`, or a flattened `GET /jobs/{id}`)."""
        data = opt_mapping(value)
        if data is None:
            raise malformed(source, "expected a job status object")
        state = data.get("state")
        if state == "queued":
            return cls(state="queued")
        if state == "processing":
            return cls(state="processing", started_at=req_str(data, "startedAt", source))
        if state == "succeeded":
            raw_download = data.get("download")
            return cls(
                state="succeeded",
                output_asset_id=req_str(data, "outputAssetId", source),
                download=(
                    None if raw_download is None else SignedUrl.from_wire(raw_download, source)
                ),
            )
        if state == "failed":
            return cls(
                state="failed",
                error_code=ErrorCode.coerce(data.get("errorCode")),
                message=req_str(data, "message", source),
            )
        if state == "canceled":
            return cls(state="canceled")
        raise malformed(source, f"unknown job state: {state!r}")


@dataclass(frozen=True)
class JobEnvelope:
    """Everything a job response carries beside its status.

    The bring-your-own-storage half (input kind, destination, delivery) plus
    the billing facts `GET /usage` aggregates: what the request cost, whether
    it was served from the cache, and whether it exists only to deliver a
    cached result somewhere new.
    """

    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None
    credit_cost: int = 0
    """Credits debited for this job. 0 for a free op, a cache hit or an unmetered caller."""
    cached: bool = False
    """True when the result was served from the cache rather than by running a provider."""
    delivery_only: bool = False
    """True for a job that exists only to deliver an already-cached result to a new destination."""

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> JobEnvelope:
        """Parse the envelope off a job response.

        Deliberately lenient: an api that predates bring-your-own-storage sends
        none of these keys, and that can only mean an asset input with no
        destination, nothing billed and no cache hit.
        """
        raw_input = opt_mapping(data.get("input"))
        kind: Literal["asset", "url"] = (
            "url" if raw_input is not None and raw_input.get("kind") == "url" else "asset"
        )
        return cls(
            input=JobInput(kind=kind),
            destination=JobDestinationSummary.from_wire(data.get("destination")),
            delivery=JobDelivery.from_wire(data.get("delivery")),
            credit_cost=opt_int(data, "creditCost") or 0,
            cached=opt_bool(data, "cached") or False,
            delivery_only=opt_bool(data, "deliveryOnly") or False,
        )


@dataclass(frozen=True)
class JobView:
    """`GET /jobs/{id}`: the status union flattened, plus the job envelope."""

    job_id: str
    status: JobStatus
    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None
    credit_cost: int = 0
    """Credits debited for this job. 0 for a free op, a cache hit or an unmetered caller."""
    cached: bool = False
    """True when the result was served from the cache rather than by running a provider."""
    delivery_only: bool = False
    """True for a job that exists only to deliver an already-cached result to a new destination."""

    @property
    def state(self) -> JobState:
        """The job's state."""
        return self.status.state

    @property
    def download(self) -> SignedUrl | None:
        """The result's presigned url, or `None` (not succeeded, or the copy was deleted)."""
        return self.status.download

    @property
    def output_asset_id(self) -> str | None:
        """The result asset id, once the job has succeeded."""
        return self.status.output_asset_id

    @property
    def error_code(self) -> ErrorCode | None:
        """The failure code, for a `failed` job."""
        return self.status.error_code

    @property
    def message(self) -> str | None:
        """The human failure message, for a `failed` job."""
        return self.status.message

    @property
    def terminal(self) -> bool:
        """True once the job can no longer change."""
        return self.status.terminal

    @property
    def settled(self) -> bool:
        """True when the job is terminal AND any delivery has stopped being pending.

        The distinction matters for exactly one case: a cache hit that also has
        to be delivered comes back `succeeded` with `delivery.status ==
        "pending"`, because pushing the bytes to your bucket is still the
        worker's job.
        """
        if not self.status.terminal:
            return False
        return not (self.status.state == "succeeded" and self.delivery is not None
                    and self.delivery.status == "pending")

    @classmethod
    def from_wire(cls, job_id: str, value: object, source: str) -> JobView:
        """Parse a `GET /jobs/{id}` body."""
        data = req_mapping(value, source)
        return cls(
            job_id=job_id,
            status=JobStatus.from_wire(data, source),
            **_envelope_kwargs(JobEnvelope.from_wire(data)),
        )


def _envelope_kwargs(envelope: JobEnvelope) -> dict[str, Any]:
    """Spread an envelope into the keyword arguments the job dataclasses take."""
    return {
        "input": envelope.input,
        "destination": envelope.destination,
        "delivery": envelope.delivery,
        "credit_cost": envelope.credit_cost,
        "cached": envelope.cached,
        "delivery_only": envelope.delivery_only,
    }


@dataclass(frozen=True)
class CreateJobResult:
    """`POST /jobs`: the new job id, its (possibly already terminal) status and envelope."""

    job_id: str
    status: JobStatus
    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None
    http_status: int = 202
    credit_cost: int = 0
    """Credits debited for this request. 0 for a free op, a cache hit or an unmetered caller."""
    cached: bool = False
    """True for a free cache hit — the api answered `200`, already `succeeded`.

    A cache hit still creates a NEW job row (so usage can count what was
    asked for as well as what ran), so `job_id` is a new id with the SAME
    `output_asset_id` as the job whose result it reuses.
    """
    delivery_only: bool = False
    """True for a job that exists only to deliver an already-cached result to a new destination."""

    @property
    def state(self) -> JobState:
        """The job's state at creation time."""
        return self.status.state

    def as_view(self) -> JobView:
        """Read this creation response as a :class:`JobView` (same fields, flattened)."""
        return JobView(
            job_id=self.job_id,
            status=self.status,
            input=self.input,
            destination=self.destination,
            delivery=self.delivery,
            credit_cost=self.credit_cost,
            cached=self.cached,
            delivery_only=self.delivery_only,
        )

    @classmethod
    def from_wire(cls, value: object, source: str, http_status: int) -> CreateJobResult:
        """Parse a `POST /jobs` body."""
        data = req_mapping(value, source)
        envelope = JobEnvelope.from_wire(data)
        kwargs = _envelope_kwargs(envelope)
        # A `200` IS the cache hit, whatever the body says: the status code is
        # the older, narrower signal and an api that predates `cached` sends
        # only that.
        kwargs["cached"] = envelope.cached or http_status == 200
        return cls(
            job_id=req_str(data, "jobId", source),
            status=JobStatus.from_wire(data.get("status"), source),
            http_status=http_status,
            **kwargs,
        )


@dataclass(frozen=True)
class UploadResult:
    """A confirmed upload: the asset id to feed a job, plus its content hash and size."""

    asset_id: str
    content_hash: str
    bytes: int


@dataclass(frozen=True)
class RunResult:
    """What `run()` returns: the finished job, and the bytes unless they were delivered.

    `downloaded` discriminates: an ordinary run carries `output` and `mime`; a
    run delivered straight into your bucket carries neither (see
    `delivery.bucket` / `delivery.key`).
    """

    job_id: str
    downloaded: bool
    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None
    download: SignedUrl | None = None
    output: bytes | None = None
    mime: str | None = None
    credit_cost: int = 0
    """Credits debited for this job. 0 for a free op, a cache hit or an unmetered caller."""
    cached: bool = False
    """True when the result was served from the cache rather than by running a provider."""


@dataclass(frozen=True)
class OperationMetadata:
    """One entry of `GET /operations`."""

    id: str
    label: str
    accept: list[str]
    max_input_dimension: int
    params_json_schema: Mapping[str, Any]
    requires_mask: bool
    description: str
    seo_title: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> OperationMetadata:
        """Parse one operation-catalog entry."""
        data = req_mapping(value, source)
        accept = req_list(data.get("accept"), source, "accept")
        return cls(
            id=req_str(data, "id", source),
            label=req_str(data, "label", source),
            accept=[item for item in accept if isinstance(item, str)],
            max_input_dimension=req_int(data, "maxInputDimension", source),
            params_json_schema=req_mapping(
                data.get("paramsJsonSchema"), source, "paramsJsonSchema"
            ),
            requires_mask=req_bool(data, "requiresMask", source),
            description=req_str(data, "description", source),
            seo_title=req_str(data, "seoTitle", source),
        )


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRange:
    """The window `GET /usage` actually reported on, as ISO-8601 instants.

    `start` and `end` are the wire's `from` and `to` — renamed only because
    `from` is a Python keyword. They are INSTANTS, not the `YYYY-MM-DD` the
    query accepts: slice the first ten characters for the day.
    """

    start: str
    end: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> UsageRange:
        """Parse `{ from, to }`."""
        data = req_mapping(value, source, "range")
        return cls(start=req_str(data, "from", source), end=req_str(data, "to", source))


@dataclass(frozen=True)
class UsageTotals:
    """The whole range, undivided.

    `jobs` counts REQUESTS — a cache hit is a job too — and `credits` is what
    was actually debited, so a free operation, a cache hit and a website job
    all add 0. `sessions` counts embedded-editor sessions started in the range,
    `active_sessions` those last seen within it.
    """

    jobs: int
    credits: int
    cache_hits: int
    free: int
    failed: int
    delivered: int
    delivery_failed: int
    sessions: int
    active_sessions: int

    @classmethod
    def from_wire(cls, value: object, source: str) -> UsageTotals:
        """Parse the `totals` object."""
        data = req_mapping(value, source, "totals")
        return cls(
            **_usage_facts(data, source),
            sessions=req_int(data, "sessions", source),
            active_sessions=req_int(data, "activeSessions", source),
        )


@dataclass(frozen=True)
class UsageSeriesPoint:
    """One bucket of the series — a day, an api key, an origin, an operation or a source.

    `key` is the dimension value (:data:`USAGE_UNATTRIBUTED` when the row has
    none) and `label` is what to show a human: the api key's NAME for
    `group_by="key"`, otherwise the key itself. `sessions` is always 0 for a
    series grouped by operation or source, which sessions have no dimension for.
    """

    key: str
    label: str
    jobs: int
    credits: int
    cache_hits: int
    free: int
    failed: int
    delivered: int
    delivery_failed: int
    sessions: int

    @classmethod
    def from_wire(cls, value: object, source: str) -> UsageSeriesPoint:
        """Parse one `series` entry."""
        data = req_mapping(value, source, "series[]")
        return cls(
            key=req_str(data, "key", source),
            label=req_str(data, "label", source),
            **_usage_facts(data, source),
            sessions=req_int(data, "sessions", source),
        )


@dataclass(frozen=True)
class UsageKeyRow:
    """One of the account's live api keys, with today's spend against its cap."""

    id: str
    name: str
    kind: Literal["secret", "publishable"]
    daily_credit_limit: int | None
    """Publishable keys only: the per-UTC-day credit ceiling. `None` means uncapped."""
    used_today: int
    """Credits this key has spent so far TODAY (UTC), whatever range was requested."""

    @classmethod
    def from_wire(cls, value: object, source: str) -> UsageKeyRow:
        """Parse one `keys` entry."""
        data = req_mapping(value, source, "keys[]")
        kind: Literal["secret", "publishable"] = (
            "publishable" if data.get("kind") == "publishable" else "secret"
        )
        return cls(
            id=req_str(data, "id", source),
            name=req_str(data, "name", source),
            kind=kind,
            daily_credit_limit=opt_int(data, "dailyCreditLimit"),
            used_today=req_int(data, "usedToday", source),
        )


@dataclass(frozen=True)
class UsageReport:
    """`GET /usage`: what the account has spent, bucketed along one dimension.

    `keys` is empty for an embed token — the api omits the roster entirely for
    a credential that may not enumerate the account's other keys, and this
    client normalizes that absence to an empty list.
    """

    range: UsageRange
    group_by: UsageGroupBy
    totals: UsageTotals
    series: list[UsageSeriesPoint]
    keys: list[UsageKeyRow] = field(default_factory=list)

    def point(self, key: str) -> UsageSeriesPoint | None:
        """Return the series bucket named `key`, or `None` when the range has none."""
        return next((entry for entry in self.series if entry.key == key), None)

    @classmethod
    def from_wire(cls, value: object, source: str) -> UsageReport:
        """Parse a `GET /usage` body."""
        data = req_mapping(value, source)
        group_by = data.get("groupBy")
        if group_by not in ("day", "key", "origin", "operation", "source"):
            raise malformed(source, f"unknown usage groupBy: {group_by!r}")
        raw_keys = data.get("keys")
        return cls(
            range=UsageRange.from_wire(data.get("range"), source),
            group_by=group_by,
            totals=UsageTotals.from_wire(data.get("totals"), source),
            series=[
                UsageSeriesPoint.from_wire(entry, source)
                for entry in req_list(data.get("series"), source, "series")
            ],
            keys=[
                UsageKeyRow.from_wire(entry, source)
                for entry in (raw_keys if isinstance(raw_keys, list) else [])
            ],
        )


def _usage_facts(data: Mapping[str, Any], source: str) -> dict[str, int]:
    """Parse the seven counters every usage bucket carries."""
    return {
        "jobs": req_int(data, "jobs", source),
        "credits": req_int(data, "credits", source),
        "cache_hits": req_int(data, "cacheHits", source),
        "free": req_int(data, "free", source),
        "failed": req_int(data, "failed", source),
        "delivered": req_int(data, "delivered", source),
        "delivery_failed": req_int(data, "deliveryFailed", source),
    }


@dataclass(frozen=True)
class StorageDestinationTest:
    """The last connectivity probe recorded against a saved destination."""

    status: Literal["ok", "failed"]
    at: str
    error: str | None = None

    @classmethod
    def from_wire(cls, value: object, source: str) -> StorageDestinationTest | None:
        """Parse `lastTest` (`None` when the destination has never been tested)."""
        data = opt_mapping(value)
        if data is None:
            return None
        status: Literal["ok", "failed"] = "ok" if data.get("status") == "ok" else "failed"
        return cls(status=status, at=req_str(data, "at", source), error=opt_str(data, "error"))


@dataclass(frozen=True)
class StorageDestination:
    """A saved S3-compatible bucket. The secret access key is never returned by any endpoint."""

    id: str
    name: str
    provider: StorageProvider
    bucket: str
    region: str | None
    endpoint: str | None
    force_path_style: bool
    key_prefix: str
    access_key_id_last4: str
    is_default: bool
    delete_after_delivery: bool
    last_test: StorageDestinationTest | None
    created_at: str
    updated_at: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> StorageDestination:
        """Parse one destination view."""
        data = req_mapping(value, source)
        provider = data.get("provider")
        if provider not in ("aws-s3", "cloudflare-r2", "backblaze-b2", "s3-compatible"):
            raise malformed(source, f"unknown storage provider: {provider!r}")
        return cls(
            id=req_str(data, "id", source),
            name=req_str(data, "name", source),
            provider=provider,
            bucket=req_str(data, "bucket", source),
            region=opt_str(data, "region"),
            endpoint=opt_str(data, "endpoint"),
            force_path_style=req_bool(data, "forcePathStyle", source),
            key_prefix=req_str(data, "keyPrefix", source),
            access_key_id_last4=req_str(data, "accessKeyIdLast4", source),
            is_default=req_bool(data, "isDefault", source),
            delete_after_delivery=req_bool(data, "deleteAfterDelivery", source),
            last_test=StorageDestinationTest.from_wire(data.get("lastTest"), source),
            created_at=req_str(data, "createdAt", source),
            updated_at=req_str(data, "updatedAt", source),
        )


@dataclass(frozen=True)
class DestinationTestResult:
    """A real round trip against the bucket: a probe object written, then deleted."""

    ok: bool
    latency_ms: float
    error: str | None = None

    @classmethod
    def from_wire(cls, value: object, source: str) -> DestinationTestResult:
        """Parse `POST /destinations/{id}/test` (always HTTP 200, `ok` says what happened)."""
        data = req_mapping(value, source)
        ok = data.get("ok") is True
        return cls(
            ok=ok,
            latency_ms=opt_float(data, "latencyMs") or 0.0,
            error=None if ok else (opt_str(data, "error") or "destination test failed"),
        )


@dataclass(frozen=True)
class DestinationPresign:
    """A one-shot, server-signed PUT into your own bucket. Send `headers` verbatim."""

    url: str
    method: Literal["PUT"]
    headers: dict[str, str]
    key: str
    bucket: str
    expires_at: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> DestinationPresign:
        """Parse `POST /destinations/{id}/presign`."""
        data = req_mapping(value, source)
        raw_headers = opt_mapping(data.get("headers")) or {}
        return cls(
            url=req_str(data, "url", source),
            method="PUT",
            headers={k: v for k, v in raw_headers.items() if isinstance(v, str)},
            key=req_str(data, "key", source),
            bucket=req_str(data, "bucket", source),
            expires_at=req_str(data, "expiresAt", source),
        )


@dataclass(frozen=True)
class EmbedToken:
    """A short-lived signed token for one embedded-editor session. Billed like an api key."""

    token: str
    expires_at: str

    @classmethod
    def from_wire(cls, value: object, source: str) -> EmbedToken:
        """Parse `POST /embed/sessions` or `POST /embed/tokens`."""
        data = req_mapping(value, source)
        return cls(
            token=req_str(data, "token", source),
            expires_at=req_str(data, "expiresAt", source),
        )
