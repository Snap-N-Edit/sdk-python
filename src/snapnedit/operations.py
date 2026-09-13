"""The operation catalog: ids, credit costs and which operations need a mask.

A static mirror of `GET /operations` and the `x-credit-cost` extensions on
`GET /openapi.json`, so a caller can price and validate a call without a round
trip. `Snapnedit.list_operations()` is the live, authoritative version.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "CREDIT_COSTS",
    "MASK_OPERATIONS",
    "OPERATION_IDS",
    "OperationId",
    "credit_cost",
    "requires_mask",
]

OperationId = Literal[
    "remove-background",
    "upscale",
    "unblur",
    "colorize",
    "style-transfer",
    "retouch",
    "beautify",
    "magic-eraser",
    "generative-fill",
    "remove-watermark",
    "ai-denoise",
    "replace-sky",
    "relight",
    "replace-background",
    "strip-metadata",
    "auto-remove-watermark",
    "resize-image",
]
"""Every operation the api accepts. `run()` also accepts a plain `str`."""

CREDIT_COSTS: dict[str, int] = {
    "remove-background": 1,
    "upscale": 2,
    "unblur": 1,
    "colorize": 1,
    "style-transfer": 2,
    "retouch": 1,
    "beautify": 1,
    "magic-eraser": 2,
    "generative-fill": 3,
    "remove-watermark": 2,
    "ai-denoise": 1,
    "replace-sky": 2,
    "relight": 2,
    "replace-background": 2,
    "strip-metadata": 1,
    "auto-remove-watermark": 2,
    "resize-image": 0,
}
"""Credit cost per operation.

`0` is free. A cache hit is never billed, and a terminal failure is refunded.
"""

OPERATION_IDS: tuple[str, ...] = tuple(CREDIT_COSTS)
"""The 17 operation ids, in catalog order."""

MASK_OPERATIONS: frozenset[str] = frozenset(
    {"magic-eraser", "generative-fill", "remove-watermark"}
)
"""Operations that require `params["maskAssetId"]` (or `run(..., mask=...)`)."""


def credit_cost(operation: str) -> int:
    """Return the credit cost of `operation`, or `-1` when it is not a known id."""
    return CREDIT_COSTS.get(operation, -1)


def requires_mask(operation: str) -> bool:
    """Return whether `operation` needs a mask asset."""
    return operation in MASK_OPERATIONS
