"""Errors raised by the snapnedit client, and the api's closed error-code set."""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ERROR_CODES",
    "ErrorCode",
    "SnapneditError",
    "SnapneditSignatureError",
    "SnapneditTimeoutError",
]


class ErrorCode(str, Enum):
    """Every machine-readable code the api can send in `{ error: { code, message } }`.

    A `str` enum, so `err.code == "not_found"` and `err.code is ErrorCode.NOT_FOUND`
    are both true. Branch on the code — never on the human `message`, which changes.
    """

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_MIME = "unsupported_mime"
    TOO_LARGE = "too_large"
    NOT_FOUND = "not_found"
    INPUT_FETCH_FAILED = "input_fetch_failed"
    """A job whose input was an external `input_url` could not be fetched. Terminal; refunded."""
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    RATE_LIMITED = "rate_limited"
    BOT_CHECK_FAILED = "bot_check_failed"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    PAYMENT_REQUIRED = "payment_required"
    """Out of credits, or over a free-tier limit (HTTP 402)."""
    INTERNAL = "internal"

    @classmethod
    def coerce(cls, value: object) -> ErrorCode:
        """Narrow an arbitrary response field to an `ErrorCode`.

        An unrecognized value (including a non-string) degrades to
        :attr:`INTERNAL` rather than raising, so an error code this release
        predates still produces a usable typed error.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for code in cls:
                if code.value == value:
                    return code
        return cls.INTERNAL

    def __str__(self) -> str:
        """Return the wire value, so f-strings read `not_found`, not `ErrorCode.NOT_FOUND`."""
        return self.value


ERROR_CODES: tuple[ErrorCode, ...] = tuple(ErrorCode)
"""Every :class:`ErrorCode`, in the order the api documents them."""


class SnapneditError(Exception):
    """A snapnedit api failure, or a job that finished in `failed`.

    Carries the api's machine-readable :class:`ErrorCode`, the human message and
    the HTTP status that produced it (`0` when there was no response at all —
    a network error, or a job-level failure reported inside a 200).
    """

    code: ErrorCode
    message: str
    status: int

    def __init__(self, code: ErrorCode | str, message: str, status: int = 0) -> None:
        """Build an error from the api's `code` / `message` and the HTTP `status`."""
        self.code = ErrorCode.coerce(code)
        self.message = message
        self.status = status
        super().__init__(message)

    def __repr__(self) -> str:
        """Show code and status alongside the message."""
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"message={self.message!r}, status={self.status!r})"
        )


class SnapneditTimeoutError(SnapneditError):
    """Polling a job never reached a settled state within the caller's `timeout`."""

    def __init__(self, message: str) -> None:
        """Build a poll-timeout error (code `internal`, status `0`)."""
        super().__init__(ErrorCode.INTERNAL, message, 0)


class SnapneditSignatureError(SnapneditError):
    """A webhook payload failed signature verification (raised only by `construct_event`)."""

    def __init__(self, message: str = "webhook signature verification failed") -> None:
        """Build a webhook-signature error (code `unauthorized`, status `0`)."""
        super().__init__(ErrorCode.UNAUTHORIZED, message, 0)
