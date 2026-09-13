"""Typed views of every shape the snapnedit api speaks.

Frozen dataclasses with `from_wire` parsers, plus the small input types a
caller constructs (`UrlInput`, `PresignedPutDestination`, `SavedDestination`).
Field names are snake_case; the wire is camelCase and the conversion happens
here and in `_endpoints`, never in caller code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
]

JobState = Literal["queued", "processing", "succeeded", "failed", "canceled"]
DeliveryStatus = Literal["pending", "delivered", "failed"]
StorageProvider = Literal["aws-s3", "cloudflare-r2", "backblaze-b2", "s3-compatible"]
DestinationExt = Literal["png", "jpg", "webp", "avif", "svg", "pdf", "gif"]

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
    """The bring-your-own-storage half of a job: input kind, destination, delivery."""

    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> JobEnvelope:
        """Parse the envelope off a job response.

        Deliberately lenient: an api that predates bring-your-own-storage sends
        none of these keys, and that can only mean an asset input with no
        destination.
        """
        raw_input = opt_mapping(data.get("input"))
        kind: Literal["asset", "url"] = (
            "url" if raw_input is not None and raw_input.get("kind") == "url" else "asset"
        )
        return cls(
            input=JobInput(kind=kind),
            destination=JobDestinationSummary.from_wire(data.get("destination")),
            delivery=JobDelivery.from_wire(data.get("delivery")),
        )


@dataclass(frozen=True)
class JobView:
    """`GET /jobs/{id}`: the status union flattened, plus the job envelope."""

    job_id: str
    status: JobStatus
    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None

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

    @property
    def cached(self) -> bool:
        """True when the api answered `200` — a free cache hit, already `succeeded`."""
        return self.http_status == 200

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
        )

    @classmethod
    def from_wire(cls, value: object, source: str, http_status: int) -> CreateJobResult:
        """Parse a `POST /jobs` body."""
        data = req_mapping(value, source)
        return cls(
            job_id=req_str(data, "jobId", source),
            status=JobStatus.from_wire(data.get("status"), source),
            http_status=http_status,
            **_envelope_kwargs(JobEnvelope.from_wire(data)),
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
