"""Per-request legislature context.

The chosen legislature lives in a contextvar set by middleware, so any
query layer can read `current_legislature()` without threading the
parameter through every call site. Falls back to `settings.legislature`
when no override is set (CLI ingestion, tests, etc.).

Strict-isolation design: each legislature has its own SQLite file
(`anqp.db` for the current one, `anqp-{n}.db` for past ones — see
`config.db_path_for`). `available_legislatures()` just lists which of
those files exist on disk.
"""
from __future__ import annotations

import contextvars
import re

_current: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "legislature", default=None,
)

_DB_FILE_RE = re.compile(r"^anqp-(\d+)\.db$")


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


def available_legislatures(conn=None) -> list[int]:
    """Legislatures with a DB file present, most recent first.

    `conn` is accepted (and ignored) for backward compatibility with older
    call sites that passed a connection.
    """
    from ..config import settings, DEFAULT_LEGISLATURE
    legs: set[int] = set()
    data_dir = settings.data_dir
    main_db = data_dir / "anqp.db"
    if main_db.exists() and main_db.stat().st_size > 0:
        legs.add(DEFAULT_LEGISLATURE)
    try:
        for p in data_dir.glob("anqp-*.db"):
            m = _DB_FILE_RE.match(p.name)
            if m and p.stat().st_size > 0:
                legs.add(int(m.group(1)))
    except OSError:
        pass
    return sorted(legs, reverse=True)
