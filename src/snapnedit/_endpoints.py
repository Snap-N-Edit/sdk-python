"""Pure request builders and response parsers — one pair per api endpoint.

No I/O happens here. Both clients call these, so a wire-format change (a
renamed field, a new query parameter) is a one-line edit that both the sync and
the async surface pick up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

from ._transport import Request
from ._wire import req_list, req_mapping, req_str
from .models import (
    UNSET,
    CreateJobResult,
    Destination,
    DestinationPresign,
    DestinationTestResult,
    EmbedToken,
    JobView,
    OperationMetadata,
    SignedUrl,
    StorageDestination,
    UnsetType,
    UrlInput,
    UsageGroupBy,
    UsageInstant,
    UsageReport,
    UsageSource,
    destination_to_wire,
)

__all__ = [
    "confirm_upload",
    "create_design",
    "create_destination",
    "create_embed_session",
    "create_embed_token",
    "create_job",
    "create_upload",
    "delete_destination",
    "download",
    "get_job",
    "get_usage",
    "list_destinations",
    "list_operations",
    "parse_confirm_upload",
    "parse_create_job",
    "parse_create_upload",
    "parse_destination",
    "parse_destination_list",
    "parse_destination_presign",
    "parse_destination_test",
    "parse_embed_token",
    "parse_job",
    "parse_operations",
    "parse_usage",
    "presign_destination_upload",
    "render_design",
    "test_destination",
    "update_destination",
]

_JSON = {"content-type": "application/json"}


def _camel(name: str) -> str:
    """Convert a snake_case argument name to the wire's camelCase."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _body(**fields: Any) -> dict[str, Any]:
    """Build a JSON body from snake_case keyword arguments, dropping every `UNSET`."""
    return {
        _camel(key): value for key, value in fields.items() if not isinstance(value, UnsetType)
    }


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def list_operations() -> Request:
    """`GET /operations` — the public operation catalog."""
    return Request("GET", "/operations")


def parse_operations(payload: Any, url: str) -> list[OperationMetadata]:
    """Parse the operation catalog."""
    entries = req_list(payload, url, "operations")
    return [OperationMetadata.from_wire(entry, url) for entry in entries]


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------


def create_upload(mime: str, size: int) -> Request:
    """`POST /uploads` — reserve an asset id and a presigned PUT."""
    return Request("POST", "/uploads", json_body={"mime": mime, "bytes": size}, headers=_JSON)


def parse_create_upload(payload: Any, url: str) -> tuple[str, SignedUrl]:
    """Parse `{ assetId, upload }`."""
    data = req_mapping(payload, url)
    return req_str(data, "assetId", url), SignedUrl.from_wire(data.get("upload"), url)


def put_upload(upload_url: str, data: bytes, mime: str) -> Request:
    """Send the bytes to the presigned PUT url.

    Deliberately unauthenticated: the url carries its own signature, and some
    presigned schemes (S3 SigV4) reject a second `Authorization` header.
    """
    return Request(
        "PUT",
        upload_url,
        content=data,
        headers={"content-type": mime},
        auth=False,
        resolve="url",
        idempotent=True,
    )


def confirm_upload(asset_id: str) -> Request:
    """`POST /uploads/{assetId}/confirm` — not optional: it fixes the content hash."""
    return Request("POST", f"/uploads/{asset_id}/confirm")


def parse_confirm_upload(payload: Any, url: str) -> tuple[str, str, int]:
    """Parse `{ assetId, contentHash, bytes }`."""
    data = req_mapping(payload, url)
    size = data.get("bytes")
    return (
        req_str(data, "assetId", url),
        req_str(data, "contentHash", url),
        size if isinstance(size, int) and not isinstance(size, bool) else 0,
    )


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


def create_job(
    operation: str,
    job_input: str | UrlInput,
    params: Mapping[str, Any] | None = None,
    destination: Destination | UnsetType | None = UNSET,
) -> Request:
    """`POST /jobs`.

    Exactly one of `inputAssetId` / `inputUrl` is emitted. `destination` is
    omitted entirely unless the caller said something — an explicit `None` opts
    out of the account default, which is not the same as saying nothing.
    """
    body: dict[str, Any] = {"operation": operation, "params": dict(params or {})}
    if isinstance(job_input, UrlInput):
        body["inputUrl"] = job_input.url
    else:
        body["inputAssetId"] = job_input
    if not isinstance(destination, UnsetType):
        body["destination"] = destination_to_wire(destination)
    return Request("POST", "/jobs", json_body=body, headers=_JSON)


def parse_create_job(payload: Any, url: str, status: int) -> CreateJobResult:
    """Parse `POST /jobs` (200 on a cache hit, 202 otherwise)."""
    return CreateJobResult.from_wire(payload, url, status)


def get_job(job_id: str) -> Request:
    """`GET /jobs/{id}` — the poll. Another account's job is 404, never 403."""
    return Request("GET", f"/jobs/{job_id}")


def parse_job(payload: Any, url: str, job_id: str) -> JobView:
    """Parse `GET /jobs/{id}`."""
    return JobView.from_wire(job_id, payload, url)


def download(signed: SignedUrl | str) -> Request:
    """Fetch result bytes from a presigned url (a same-origin path, usually)."""
    return Request(
        "GET",
        signed if isinstance(signed, str) else signed.url,
        auth=False,
        resolve="url",
        idempotent=True,
    )


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------


def usage_instant(value: UsageInstant) -> str:
    """Render a range end for the query string.

    A `date` stays a bare `YYYY-MM-DD` — the api reads that as the whole UTC
    day, which is what a caller naming a day means. A `datetime` becomes an
    instant in UTC; a naive one is taken as already UTC rather than guessed at,
    so the same code reports the same window on every machine.
    """
    if isinstance(value, datetime):
        at = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def get_usage(
    *,
    start: UsageInstant | None = None,
    end: UsageInstant | None = None,
    group_by: UsageGroupBy | None = None,
    key_id: str | None = None,
    origin: str | None = None,
    operation: str | None = None,
    source: UsageSource | None = None,
) -> Request:
    """`GET /usage` — jobs, credits and embed sessions over a range.

    Every argument is optional; the api defaults to the last 30 days grouped by
    day. `start` and `end` are the wire's `from` and `to`.
    """
    query: list[tuple[str, str]] = [
        (name, value)
        for name, value in (
            ("from", None if start is None else usage_instant(start)),
            ("to", None if end is None else usage_instant(end)),
            ("groupBy", group_by),
            ("keyId", key_id),
            ("origin", origin),
            ("operation", operation),
            ("source", source),
        )
        if value is not None and value != ""
    ]
    suffix = f"?{urlencode(query)}" if query else ""
    return Request("GET", f"/usage{suffix}")


def parse_usage(payload: Any, url: str) -> UsageReport:
    """Parse the usage report."""
    return UsageReport.from_wire(payload, url)


# --------------------------------------------------------------------------
# saved storage destinations
# --------------------------------------------------------------------------


def list_destinations() -> Request:
    """`GET /destinations`."""
    return Request("GET", "/destinations")


def parse_destination_list(payload: Any, url: str) -> list[StorageDestination]:
    """Parse `{ destinations: [...] }`."""
    data = req_mapping(payload, url)
    return [
        StorageDestination.from_wire(entry, url)
        for entry in req_list(data.get("destinations"), url, "destinations")
    ]


def create_destination(body: Mapping[str, Any]) -> Request:
    """`POST /destinations` — saves a bucket plus credentials (encrypted at rest)."""
    return Request("POST", "/destinations", json_body=dict(body), headers=_JSON)


def update_destination(destination_id: str, body: Mapping[str, Any]) -> Request:
    """`PATCH /destinations/{id}`."""
    return Request(
        "PATCH", f"/destinations/{destination_id}", json_body=dict(body), headers=_JSON
    )


def parse_destination(payload: Any, url: str) -> StorageDestination:
    """Parse `{ destination: {...} }`."""
    data = req_mapping(payload, url)
    return StorageDestination.from_wire(data.get("destination"), url)


def delete_destination(destination_id: str) -> Request:
    """`DELETE /destinations/{id}` — 204, no body."""
    return Request("DELETE", f"/destinations/{destination_id}")


def test_destination(destination_id: str) -> Request:
    """`POST /destinations/{id}/test` — writes and deletes a probe object."""
    return Request(
        "POST", f"/destinations/{destination_id}/test", json_body={}, headers=_JSON
    )


def parse_destination_test(payload: Any, url: str) -> DestinationTestResult:
    """Parse the probe result (always HTTP 200)."""
    return DestinationTestResult.from_wire(payload, url)


def presign_destination_upload(destination_id: str, ext: str, content_type: str) -> Request:
    """`POST /destinations/{id}/presign` — one short-lived PUT into your own bucket."""
    return Request(
        "POST",
        f"/destinations/{destination_id}/presign",
        json_body={"ext": ext, "contentType": content_type},
        headers=_JSON,
    )


def parse_destination_presign(payload: Any, url: str) -> DestinationPresign:
    """Parse the signed PUT."""
    return DestinationPresign.from_wire(payload, url)


def destination_body(
    *,
    name: str | UnsetType = UNSET,
    provider: str | UnsetType = UNSET,
    bucket: str | UnsetType = UNSET,
    region: str | UnsetType | None = UNSET,
    endpoint: str | UnsetType | None = UNSET,
    account_id: str | UnsetType = UNSET,
    force_path_style: bool | UnsetType = UNSET,
    key_prefix: str | UnsetType = UNSET,
    access_key_id: str | UnsetType = UNSET,
    secret_access_key: str | UnsetType = UNSET,
    is_default: bool | UnsetType = UNSET,
    delete_after_delivery: bool | UnsetType = UNSET,
) -> dict[str, Any]:
    """Build a destination create/patch body, dropping every argument left `UNSET`."""
    return _body(
        name=name,
        provider=provider,
        bucket=bucket,
        region=region,
        endpoint=endpoint,
        account_id=account_id,
        force_path_style=force_path_style,
        key_prefix=key_prefix,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        is_default=is_default,
        delete_after_delivery=delete_after_delivery,
    )


# --------------------------------------------------------------------------
# embed
# --------------------------------------------------------------------------


def create_embed_session(publishable_key: str, host_origin: str) -> Request:
    """`POST /embed/sessions` — exchange a `pk_` key for a short-lived token.

    Unauthenticated by design: the publishable key IS the credential, and it is
    only ever accepted here (as a bearer it resolves to anonymous).
    """
    return Request(
        "POST",
        "/embed/sessions",
        json_body={"publishableKey": publishable_key, "hostOrigin": host_origin},
        headers=_JSON,
        auth=False,
    )


def create_embed_token(
    *,
    ttl_seconds: int | UnsetType = UNSET,
    end_user_id: str | UnsetType = UNSET,
    max_credits: int | UnsetType = UNSET,
    allowed_operations: Sequence[str] | UnsetType = UNSET,
    origin: str | UnsetType = UNSET,
) -> Request:
    """`POST /embed/tokens` — mint an embed token from your server, with an `sk_` key."""
    body = _body(
        ttl_seconds=ttl_seconds,
        end_user_id=end_user_id,
        max_credits=max_credits,
        allowed_operations=(
            allowed_operations
            if isinstance(allowed_operations, UnsetType)
            else list(allowed_operations)
        ),
        origin=origin,
    )
    return Request("POST", "/embed/tokens", json_body=body, headers=_JSON)


def parse_embed_token(payload: Any, url: str) -> EmbedToken:
    """Parse `{ token, expiresAt }`."""
    return EmbedToken.from_wire(payload, url)


# --------------------------------------------------------------------------
# designs
# --------------------------------------------------------------------------


def create_design(spec: Mapping[str, Any]) -> Request:
    """`POST /designs` — compile a declarative design spec into editor documents."""
    return Request("POST", "/designs", json_body=dict(spec), headers=_JSON)


def parse_design(payload: Any, url: str) -> dict[str, Any]:
    """Parse `{ document }`."""
    data = req_mapping(payload, url)
    return dict(req_mapping(data.get("document"), url, "document"))


def parse_design_pages(payload: Any, url: str) -> list[dict[str, Any]]:
    """Parse `{ documents: [...] }`."""
    data = req_mapping(payload, url)
    return [
        dict(req_mapping(entry, url, "documents[]"))
        for entry in req_list(data.get("documents"), url, "documents")
    ]


def render_design(spec: Mapping[str, Any]) -> Request:
    """`POST /designs/render` — server-side render to png/jpeg/pdf bytes."""
    return Request("POST", "/designs/render", json_body=dict(spec), headers=_JSON)
