"""The shared request layer: what to send, how to read it back, when to retry.

Everything here is pure and transport-agnostic — the sync client and the async
client build the same :class:`Request` objects, hand them to httpx, and run the
answers through the same :func:`check_response` / parser functions. The only
thing the two twins do differently is `await`.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin

from .errors import ErrorCode, SnapneditError

__all__ = ["DEFAULT_BASE_URL", "Request", "RetryPolicy", "check_response", "resolve_url"]

DEFAULT_BASE_URL = "https://snapnedit.com/api"
"""The public api. The website proxies `/api/*` to it, so this is a real origin+prefix."""

_RETRY_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})


@dataclass(frozen=True)
class Request:
    """One HTTP call, fully described and not yet sent."""

    method: str
    path: str
    """An api path (`/jobs`), or an absolute url / presigned path when `resolve` says so."""
    json_body: Any | None = None
    content: bytes | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    auth: bool = True
    """Send `Authorization: Bearer <api key>`. A presigned url is self-authenticating: never."""
    resolve: Literal["api", "url"] = "api"
    """`api` concatenates onto `base_url`; `url` resolves a presigned path against its origin."""
    idempotent: bool | None = None
    """Override the method-based retry-safety default (`POST` is not retried)."""

    @property
    def retryable(self) -> bool:
        """Whether this call may be retried after a 429/5xx/network failure."""
        if self.idempotent is not None:
            return self.idempotent
        return self.method.upper() in _RETRY_METHODS


def resolve_url(base_url: str, request: Request) -> str:
    """Turn a :class:`Request`'s path into an absolute url.

    An api path is CONCATENATED onto `base_url` (so a base with a path prefix,
    like `https://snapnedit.com/api`, keeps it). A presigned url comes back from
    the api as a same-origin PATH (`/_local/…?exp&sig`) and is resolved against
    the base's ORIGIN instead — matching what the reference client does.
    """
    if request.path.startswith(("http://", "https://")):
        return request.path
    if request.resolve == "url":
        return urljoin(base_url if base_url.endswith("/") else base_url + "/", request.path)
    return base_url.rstrip("/") + request.path


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter, for idempotent calls only."""

    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_max: float = 8.0

    def should_retry_status(self, status: int) -> bool:
        """Retry a rate limit or any server-side failure."""
        return status == 429 or status >= 500

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before retry number `attempt` (1-based).

        Honors a `Retry-After` header when the server sent one, otherwise
        `base * 2**(attempt-1)` with full jitter, capped at `backoff_max`.
        """
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.backoff_max)
        window = min(self.backoff_base * (2 ** max(attempt - 1, 0)), self.backoff_max)
        return random.uniform(window / 2, window)


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Read a numeric `Retry-After` header, if there is one."""
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return float(value)
            except ValueError:
                return None
    return None


def check_response(status: int, content: bytes, url: str) -> None:
    """Raise a typed :class:`SnapneditError` for any non-2xx response.

    The api's uniform envelope is `{ "error": { "code", "message" } }`; a body
    that isn't one (a proxy's HTML 502, say) still produces an error carrying
    the status.
    """
    if 200 <= status < 300:
        return
    code = ErrorCode.INTERNAL
    message = f"request to {url} failed with status {status}"
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = ErrorCode.coerce(error.get("code"))
            raw_message = error.get("message")
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
    raise SnapneditError(code, message, status)


def parse_json(content: bytes, status: int, url: str) -> Any:
    """Decode a 2xx JSON body, or raise a `SnapneditError` naming the endpoint."""
    try:
        return json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SnapneditError(
            ErrorCode.INTERNAL, f"{url} returned a non-JSON {status} response: {exc}", status
        ) from exc
