"""Narrowing helpers for untrusted JSON bodies.

Every response field is checked before it lands in a typed slot: no blind
casts, and a malformed body raises a `SnapneditError(code='internal')` naming
the field rather than an opaque `KeyError` / `TypeError` deep in a dataclass.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ErrorCode, SnapneditError

__all__ = [
    "malformed",
    "opt_bool",
    "opt_float",
    "opt_int",
    "opt_mapping",
    "opt_str",
    "req_bool",
    "req_int",
    "req_list",
    "req_mapping",
    "req_str",
]


def malformed(source: str, detail: str) -> SnapneditError:
    """Build the error raised for any response we cannot narrow."""
    return SnapneditError(ErrorCode.INTERNAL, f"{source}: {detail}", 0)


def req_mapping(value: object, source: str, field: str = "body") -> Mapping[str, Any]:
    """Require a JSON object."""
    if not isinstance(value, Mapping):
        raise malformed(source, f'expected an object for "{field}"')
    return {str(key): item for key, item in value.items()}


def opt_mapping(value: object) -> Mapping[str, Any] | None:
    """Return a JSON object, or `None` for anything else (including `null`)."""
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def req_str(data: Mapping[str, Any], key: str, source: str) -> str:
    """Require a string field."""
    value = data.get(key)
    if not isinstance(value, str):
        raise malformed(source, f'missing string field "{key}"')
    return value


def opt_str(data: Mapping[str, Any], key: str) -> str | None:
    """Return a string field, or `None` when absent or not a string."""
    value = data.get(key)
    return value if isinstance(value, str) else None


def req_int(data: Mapping[str, Any], key: str, source: str) -> int:
    """Require an integer field."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise malformed(source, f'missing integer field "{key}"')
    return value


def opt_int(data: Mapping[str, Any], key: str) -> int | None:
    """Return an integer field, or `None` when absent or not an integer."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def req_bool(data: Mapping[str, Any], key: str, source: str) -> bool:
    """Require a boolean field."""
    value = data.get(key)
    if not isinstance(value, bool):
        raise malformed(source, f'missing boolean field "{key}"')
    return value


def opt_bool(data: Mapping[str, Any], key: str) -> bool | None:
    """Return a boolean field, or `None` when absent or not a boolean."""
    value = data.get(key)
    return value if isinstance(value, bool) else None


def opt_float(data: Mapping[str, Any], key: str) -> float | None:
    """Return a numeric field as a float, or `None` when absent or not numeric."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def req_list(value: object, source: str, field: str) -> list[Any]:
    """Require a JSON array."""
    if not isinstance(value, list):
        raise malformed(source, f'missing array field "{field}"')
    return list(value)
