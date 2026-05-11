"""Tiny helpers used by every parser.

The Assemblée's JSON has a couple of recurring quirks:
  * A repeated element is sometimes a list, sometimes a single dict
    (XML-to-JSON unfolding artefact). Always go through `as_list`.
  * Optional values can be: missing, None, or {"@xsi:nil": "true"}.
    `text_of` normalises these.
"""
from __future__ import annotations

from typing import Any, Iterable

NIL_MARKERS = ("@xsi:nil",)


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def text_of(value: Any) -> str | None:
    """Return a string for either a bare string or a `{ "#text": "..." }` form,
    or None for nil/empty."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, dict):
        if any(value.get(k) for k in NIL_MARKERS):
            return None
        if "#text" in value:
            return text_of(value["#text"])
    return None


def get(d: dict | None, *path: str, default=None) -> Any:
    """Safe nested-getter. Returns default if any link in the chain is None."""
    cur: Any = d
    for key in path:
        if cur is None:
            return default
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def first(items: Iterable[dict], **filters) -> dict | None:
    """First dict in `items` matching ALL key=value filters, or None."""
    for it in items:
        if all(it.get(k) == v for k, v in filters.items()):
            return it
    return None


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = text_of(value) if isinstance(value, dict) else str(value)
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None
