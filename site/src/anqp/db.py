"""Thin DB layer — backend-agnostic connection façade.

Today only SQLite is implemented (`SQLiteBackend`). The factory function
`connect()` reads `settings.backend` (default `"sqlite"`) so a Postgres
backend can be plugged in without touching call sites.

See ``POSTGRES.md`` at the repo root for the porting procedure.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from .config import settings
from .logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------------------------------------------------------------------
# Backend protocol — every concrete backend must expose `.connect(...)`
# returning a DB-API 2.0 compatible connection with a dict row factory.
# ---------------------------------------------------------------------
class Backend(Protocol):
    name: str

    def connect(self, *, read_only: bool = False) -> Any:
        ...

    def init_schema(self, conn: Any) -> None:
        ...


# ---------------------------------------------------------------------
# SQLite implementation (current default).
# ---------------------------------------------------------------------
def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


class SQLiteBackend:
    name = "sqlite"

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.db_path

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        p = self.db_path
        if read_only:
            uri = f"file:{p.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        conn.row_factory = _row_factory
        # FK constraints are declared but not enforced — see ADR-003.
        conn.execute("PRAGMA foreign_keys = OFF")
        if not read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA cache_size = -64000")  # 64 MB page cache
        return conn

    def init_schema(self, conn: sqlite3.Connection) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(sql)
        log.info("schema_initialised",
                 extra={"backend": self.name, "db_path": str(self.db_path)})


# ---------------------------------------------------------------------
# Backend factory + module-level helpers (kept stable for callers).
# ---------------------------------------------------------------------
def get_backend() -> Backend:
    """Return the configured backend instance.

    Reads `settings.backend` (default `"sqlite"`). To plug Postgres in
    later, register a `PostgresBackend` here and key it on `"postgres"`.
    """
    name = getattr(settings, "backend", "sqlite")
    if name == "sqlite":
        return SQLiteBackend()
    raise NotImplementedError(
        f"Backend '{name}' is not implemented. "
        "See POSTGRES.md for the SQLite → Postgres port procedure."
    )


def connect(db_path: Path | None = None, *, read_only: bool = False):
    """Open a connection through the configured backend.

    Kept signature-compatible with the original SQLite-only version so
    every caller (CLI, web, tests) continues to work unchanged.
    """
    if db_path is not None:
        backend: Backend = SQLiteBackend(db_path=db_path)
    else:
        backend = get_backend()
    return backend.connect(read_only=read_only)


def init_schema(conn) -> None:
    """Apply the schema idempotently using the active backend."""
    get_backend().init_schema(conn)


@contextmanager
def transaction(conn) -> Iterator[Any]:
    """Explicit transaction wrapper. Backend-neutral as long as the
    underlying driver supports BEGIN/COMMIT/ROLLBACK statements."""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_db():
    """FastAPI dependency: a fresh read-only connection per request."""
    conn = connect(read_only=True)
    try:
        yield conn
    finally:
        conn.close()
