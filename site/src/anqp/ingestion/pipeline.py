"""End-to-end ingestion orchestration.

For each source: download (with conditional GET), parse, upsert, and write
an ingestion_runs row. Failures of one source never abort the others.
"""
from __future__ import annotations

import sqlite3
import time
import traceback
from datetime import datetime
from typing import Iterable

from ..config import settings
from ..db import connect, init_schema
from ..logging_setup import get_logger
from .amendements import ingest_amendements
from .deputies import ingest_amo
from .dossiers import ingest_dossiers
from .download import download_source
from .questions import ingest_questions
from .scrutins import ingest_scrutins
from .seances import ingest_agenda, ingest_syseron

log = get_logger(__name__)


# ---------------------------------------------------------------------
# Ingestion-run logbook
# ---------------------------------------------------------------------
def _run_start(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingestion_runs(source, started_at, status) "
        "VALUES (?, datetime('now'), 'running')",
        (source,),
    )
    return cur.lastrowid


def _run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    rows_seen: int = 0,
    rows_inserted: int = 0,
    rows_updated: int = 0,
    rows_skipped: int = 0,
    rows_errors: int = 0,
    bytes_downloaded: int = 0,
    source_etag: str | None = None,
    source_last_modified: str | None = None,
    duration_seconds: float | None = None,
    error_message: str | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
           SET finished_at = datetime('now'),
               status = ?,
               rows_seen = ?,
               rows_inserted = ?,
               rows_updated = ?,
               rows_skipped = ?,
               rows_errors = ?,
               bytes_downloaded = ?,
               source_etag = ?,
               source_last_modified = ?,
               duration_seconds = ?,
               error_message = ?,
               notes = ?
         WHERE id = ?
        """,
        (
            status, rows_seen, rows_inserted, rows_updated, rows_skipped,
            rows_errors, bytes_downloaded, source_etag, source_last_modified,
            duration_seconds, error_message, notes, run_id,
        ),
    )


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------
def run_ingestion(
    sources: Iterable[str] | None = None,
    *,
    force: bool = False,
    skip_download: bool = False,
) -> dict[str, dict]:
    """Run ingestion for the given sources (default = all).

    Each source is wrapped in its own try/except so a single failure does
    not abort the whole run.
    """
    conn = connect()
    init_schema(conn)
    sources = list(sources or settings.sources.keys())
    results: dict[str, dict] = {}

    for src in sources:
        url = settings.sources.get(src)
        if url is None:
            log.warning("unknown_source", extra={"source": src})
            continue

        run_id = _run_start(conn, src)
        started = time.monotonic()
        rows_seen = inserted = updated = errors = 0
        bytes_downloaded = 0
        etag = last_modified = None
        status = "running"
        err_msg = None

        try:
            if skip_download:
                file_path = settings.raw_dir / f"{src}.zip"
                if not file_path.exists():
                    raise FileNotFoundError(
                        f"--skip-download requested but {file_path} is missing"
                    )
                cache_hit = True
            else:
                dl = download_source(conn, src, url, force=force)
                file_path = dl.file_path
                bytes_downloaded = dl.bytes_downloaded
                etag = dl.etag
                last_modified = dl.last_modified
                cache_hit = dl.cache_hit

            if src in ("AMO10", "AMO50"):
                stats = ingest_amo(conn, file_path)
                rows_seen = stats["deputies"]
                inserted = stats["deputies"]
                errors = stats["errors"]
                notes = (
                    f"organes={stats['organes']} deputies={stats['deputies']} "
                    f"mandates={stats['mandates']} cache_hit={cache_hit}"
                )
            elif src in ("QE", "QOSD", "QAG"):
                stats = ingest_questions(conn, file_path, expected_type=src)
                rows_seen = stats["seen"]
                inserted = stats["inserted"]
                updated = stats["updated"]
                errors = stats["errors"]
                notes = (
                    f"cache_hit={cache_hit} "
                    f"status_changes={stats.get('status_changes', 0)} "
                    f"answers_published={stats.get('answers_published', 0)}"
                )
            elif src == "DOSSIERS":
                stats = ingest_dossiers(conn, file_path)
                rows_seen = stats["dossiers"] + stats["documents"]
                inserted = stats["dossiers"]
                errors = stats["errors"]
                notes = (
                    f"dossiers={stats['dossiers']} documents={stats['documents']} "
                    f"actes={stats['actes']} cache_hit={cache_hit}"
                )
            elif src == "SCRUTINS":
                stats = ingest_scrutins(conn, file_path)
                rows_seen = stats["scrutins"]
                inserted = stats["scrutins"]
                errors = stats["errors"]
                notes = f"votes={stats['votes']} cache_hit={cache_hit}"
            elif src == "AMENDEMENTS":
                stats = ingest_amendements(conn, file_path)
                rows_seen = stats["seen"]
                inserted = stats["inserted"]
                errors = stats["errors"]
                notes = f"rows={stats['rows']} cache_hit={cache_hit}"
            elif src == "AGENDA":
                stats = ingest_agenda(conn, file_path)
                rows_seen = stats["seances"]
                inserted = stats["seances"]
                errors = stats["errors"]
                notes = f"cache_hit={cache_hit}"
            elif src == "SYSERON":
                stats = ingest_syseron(conn, file_path)
                rows_seen = stats["comptes_rendus"]
                inserted = stats["interventions"]
                errors = stats["errors"]
                notes = (
                    f"comptes_rendus={stats['comptes_rendus']} "
                    f"interventions={stats['interventions']} cache_hit={cache_hit}"
                )
            else:
                raise ValueError(f"Unknown source: {src}")

            status = "success" if errors == 0 else "partial"
        except Exception as e:
            status = "failure"
            err_msg = f"{type(e).__name__}: {e}"
            log.exception(
                "ingestion_source_failed",
                extra={"source": src, "error": err_msg},
            )
            notes = traceback.format_exc()[-1000:]

        duration = round(time.monotonic() - started, 2)
        _run_finish(
            conn,
            run_id,
            status=status,
            rows_seen=rows_seen,
            rows_inserted=inserted,
            rows_updated=updated,
            rows_errors=errors,
            bytes_downloaded=bytes_downloaded,
            source_etag=etag,
            source_last_modified=last_modified,
            duration_seconds=duration,
            error_message=err_msg,
            notes=notes,
        )
        results[src] = {
            "status": status,
            "rows_seen": rows_seen,
            "inserted": inserted,
            "updated": updated,
            "errors": errors,
            "bytes_downloaded": bytes_downloaded,
            "duration_seconds": duration,
            "error": err_msg,
        }
        log.info("source_ingested", extra={"source": src, **results[src]})

    # After all questions land, refill auteur_nom_complet for anything that
    # was missing the deputy row at upsert time (e.g. ex-deputies whose
    # acteur file isn't in AMO10 but might appear if we ever load AMO50).
    conn.execute(
        """
        UPDATE questions
           SET auteur_nom_complet = (
               SELECT nom_complet FROM deputies WHERE deputies.uid = questions.auteur_uid
           )
         WHERE auteur_nom_complet IS NULL
           AND auteur_uid IS NOT NULL
        """
    )

    _refresh_meta_counts(conn)

    conn.close()
    return results


def _refresh_meta_counts(conn: sqlite3.Connection) -> None:
    """Materialise heavy COUNT(*) results into `meta` so the homepage stays
    instant (votes table is 1M+ rows)."""
    for t in ("dossiers", "documents", "amendements", "scrutins", "votes",
              "deputies", "questions"):
        try:
            n = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"count_{t}", str(n)),
            )
        except sqlite3.OperationalError:
            # Table may not exist yet (older DB). Skip silently.
            continue
