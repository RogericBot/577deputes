"""Per-request legislature context.

The chosen legislature lives in a contextvar set by middleware, so any
query layer can read `current_legislature()` without threading the
parameter through every call site. Falls back to `settings.legislature`
when no override is set (CLI ingestion, tests, etc.).
"""
from __future__ import annotations

import contextvars
import sqlite3

_current: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "legislature", default=None,
)


def current_legislature() -> int:
    """Return the legislature in effect for the current call.

    Per-request override (set by middleware) wins ; otherwise the static
    `settings.legislature`."""
    val = _current.get()
    if val is None:
        from ..config import settings
        return settings.legislature
    return val


def set_legislature(value: int | None) -> None:
    _current.set(value)


def available_legislatures(conn: sqlite3.Connection) -> list[int]:
    """Return legislatures that have a meaningful amount of data ingested.

    Threshold : > 100 questions OR > 100 amendements OR > 100 scrutins. Avoids
    listing legislatures referenced only marginally (e.g. via legacy dossiers).
    """
    rows = conn.execute(
        """
        SELECT leg FROM (
          SELECT legislature AS leg FROM questions GROUP BY legislature HAVING COUNT(*) > 100
          UNION
          SELECT legislature AS leg FROM amendements GROUP BY legislature HAVING COUNT(*) > 100
          UNION
          SELECT legislature AS leg FROM scrutins GROUP BY legislature HAVING COUNT(*) > 100
        )
        WHERE leg IS NOT NULL
        ORDER BY leg DESC
        """
    ).fetchall()
    return [int(r["leg"]) for r in rows if r["leg"] is not None]
