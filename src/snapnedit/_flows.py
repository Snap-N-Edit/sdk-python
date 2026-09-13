"""The decisions `run()` makes, factored out of both clients.

The sync and the async `run()` differ only in how they wait; every rule about
what to do next lives here, so the two can never drift apart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import ErrorCode, SnapneditError, SnapneditTimeoutError
from .models import UNSET, Destination, JobView, UnsetType

__all__ = ["Deadline", "job_failure", "should_download"]


@dataclass
class Deadline:
    """A polling budget: how long is left, and how long to sleep next."""

    timeout: float
    started: float

    @classmethod
    def start(cls, timeout: float) -> Deadline:
        """Begin a budget of `timeout` seconds from now."""
        return cls(timeout=timeout, started=time.monotonic())

    @property
    def remaining(self) -> float:
        """Seconds left before the budget is spent."""
        return self.timeout - (time.monotonic() - self.started)

    def sleep_for(self, poll_interval: float, job_id: str) -> float:
        """Return how long to sleep before the next poll, or raise when out of time."""
        remaining = self.remaining
        if remaining <= 0:
            raise SnapneditTimeoutError(
                f"polling job {job_id} exceeded timeout ({self.timeout:g}s) "
                "without reaching a settled state"
            )
        return min(poll_interval, remaining)


def job_failure(view: JobView) -> SnapneditError | None:
    """Return the error a terminal job should raise, or `None` when it succeeded."""
    if view.state == "failed":
        return SnapneditError(
            view.error_code or ErrorCode.INTERNAL,
            view.message or f"job {view.job_id} failed",
            200,
        )
    if view.state == "canceled":
        return SnapneditError(ErrorCode.INTERNAL, f"job {view.job_id} was canceled", 200)
    return None


def should_download(
    explicit: bool | None,
    destination: Destination | UnsetType | None,
    view: JobView,
) -> bool:
    """Whether `run()` should pull the result bytes back through this process.

    Defaults to yes — except when the caller explicitly named a destination AND
    the delivery succeeded, which is the one case where downloading would undo
    the point of bring-your-own-storage. A delivery that FAILED still
    downloads, so a caller is never left empty-handed. An account default
    applied server-side does not flip the default: someone who wrote plain
    `run(...)` still expects bytes.
    """
    if view.download is None:
        # `deleteAfterDelivery`: our copy is gone, the caller's bucket has the
        # only one. Reporting "not downloaded" beats raising at a caller who
        # asked for exactly this.
        return False
    if explicit is not None:
        return explicit
    asked_for_delivery = not isinstance(destination, UnsetType) and destination is not None
    delivered = view.delivery is not None and view.delivery.status == "delivered"
    return not (asked_for_delivery and delivered)


def wants_default_destination(destination: Destination | UnsetType | None) -> bool:
    """Return whether the caller said nothing, so the account default (if any) applies."""
    return destination is UNSET
