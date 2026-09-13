"""Outbound-webhook types and signature verification.

A snapnedit account can register endpoint urls that receive a signed HTTP POST
when a job finishes. Verify before you trust:

```python
from snapnedit import Webhooks

raw = request.get_data()                     # the RAW bytes, unparsed
sig = request.headers["X-Snapnedit-Signature"]
if not Webhooks.verify(raw, sig, endpoint_secret):
    return "bad signature", 400
event = Webhooks.construct_event(raw, sig, endpoint_secret)
```

The header is `t=<unix seconds>,v1=<hex>` — comma-separated `k=v` pairs,
unknown keys ignored, and `v1` may appear more than once during a secret
rotation (any match is accepted). The signed string is `f"{t}.{raw_body}"` and
the MAC is HMAC-SHA256 keyed with the endpoint's plaintext signing secret,
lower-case hex, compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from ._wire import opt_int, opt_str, req_mapping, req_str
from .errors import SnapneditSignatureError
from .models import JobDelivery, JobDestinationSummary, JobEnvelope, JobInput

__all__ = [
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "WebhookEvent",
    "WebhookJobData",
    "Webhooks",
    "construct_event",
    "verify",
]

WEBHOOK_SIGNATURE_HEADER = "X-Snapnedit-Signature"
WEBHOOK_EVENT_HEADER = "X-Snapnedit-Event"
WEBHOOK_DELIVERY_HEADER = "X-Snapnedit-Delivery"


@dataclass(frozen=True)
class WebhookJobData:
    """The `data` object of a job event.

    A `job.succeeded` event carries `output_asset_id` (and usually `download`);
    a `job.failed` event carries `error_code` and `message`. Both carry the
    bring-your-own-storage envelope. `status == "succeeded"` with
    `delivery.status == "failed"` is a real combination: the job ran, only the
    PUT into your bucket did not.
    """

    job_id: str
    operation: str
    status: str
    input: JobInput
    destination: JobDestinationSummary | None
    delivery: JobDelivery | None
    output_asset_id: str | None = None
    download: str | None = None
    error_code: str | None = None
    message: str | None = None

    @classmethod
    def from_wire(cls, value: object, source: str) -> WebhookJobData:
        """Parse the `data` object of a delivery body."""
        data = req_mapping(value, source, "data")
        envelope = JobEnvelope.from_wire(data)
        return cls(
            job_id=req_str(data, "jobId", source),
            operation=req_str(data, "operation", source),
            status=req_str(data, "status", source),
            input=envelope.input,
            destination=envelope.destination,
            delivery=envelope.delivery,
            output_asset_id=opt_str(data, "outputAssetId"),
            download=opt_str(data, "download"),
            error_code=opt_str(data, "errorCode"),
            message=opt_str(data, "message"),
        )


@dataclass(frozen=True)
class WebhookEvent:
    """One delivery body: `{ id, type, created, data }`.

    `type` is deliberately an open string (`job.succeeded`, `job.failed`, …) —
    treat an unrecognized one as a forward-compatible no-op.
    """

    id: str
    type: str
    created: int
    data: WebhookJobData
    raw: dict[str, Any]

    @classmethod
    def from_wire(cls, value: object, source: str) -> WebhookEvent:
        """Parse a delivery body."""
        body = req_mapping(value, source)
        return cls(
            id=req_str(body, "id", source),
            type=req_str(body, "type", source),
            created=opt_int(body, "created") or 0,
            data=WebhookJobData.from_wire(body.get("data"), source),
            raw=dict(body),
        )


def _as_bytes(payload: str | bytes | bytearray) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


def _parse_header(header: str) -> tuple[int, list[str]] | None:
    """Parse `t=<unix seconds>,v1=<hex>[,v1=<hex>…]`, or `None` when malformed."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(float(value))
            except ValueError:
                continue
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        return None
    return timestamp, signatures


def verify(
    payload: str | bytes | bytearray,
    signature: str,
    secret: str,
    *,
    tolerance_seconds: int | None = None,
    now: float | None = None,
) -> bool:
    """Verify an `X-Snapnedit-Signature` header against the RAW body and a secret.

    Args:
        payload: The exact bytes you received. Verify BEFORE `json.loads` — a
            re-serialized body will not match.
        signature: The `X-Snapnedit-Signature` header value.
        secret: The endpoint's plaintext signing secret (`whsec_…`).
        tolerance_seconds: Optional freshness window; reject a signed
            timestamp more than this many seconds from `now` (300 is the
            conventional choice). Off by default.
        now: Current unix time, for deterministic tests.

    Returns `True` only when a signature matches (and, if asked, the timestamp
    is fresh). Returns `False` — never raises — for a malformed header, a bad
    signature or a stale timestamp, so every falsy result can be treated the
    same way.
    """
    parsed = _parse_header(signature)
    if parsed is None:
        return False
    timestamp, candidates = parsed

    if tolerance_seconds is not None:
        current = time.time() if now is None else now
        if abs(current - timestamp) > tolerance_seconds:
            return False

    signed = str(timestamp).encode("ascii") + b"." + _as_bytes(payload)
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    # `compare_digest` for every candidate: a length mismatch may short-circuit,
    # a value mismatch must not.
    return any(hmac.compare_digest(expected, candidate.lower()) for candidate in candidates)


def construct_event(
    payload: str | bytes | bytearray,
    signature: str,
    secret: str,
    *,
    tolerance_seconds: int | None = None,
    now: float | None = None,
) -> WebhookEvent:
    """Verify a delivery and parse it into a :class:`WebhookEvent`.

    Raises:
        SnapneditSignatureError: the signature (or the freshness window) did
            not check out, or the verified body is not valid JSON.
    """
    if not verify(payload, signature, secret, tolerance_seconds=tolerance_seconds, now=now):
        raise SnapneditSignatureError()
    try:
        body = json.loads(_as_bytes(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SnapneditSignatureError(f"webhook payload is not valid JSON: {exc}") from exc
    return WebhookEvent.from_wire(body, "webhook payload")


class Webhooks:
    """Namespace for webhook helpers: `Webhooks.verify` and `Webhooks.construct_event`."""

    SIGNATURE_HEADER = WEBHOOK_SIGNATURE_HEADER
    EVENT_HEADER = WEBHOOK_EVENT_HEADER
    DELIVERY_HEADER = WEBHOOK_DELIVERY_HEADER

    verify = staticmethod(verify)
    construct_event = staticmethod(construct_event)
