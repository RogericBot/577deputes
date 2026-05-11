"""Background daemon thread that re-runs the full ingestion every 24 h.

Runs as long as the FastAPI server is up. Idempotent : if the last
successful run is recent enough, it sleeps. Each cycle :
  1. `run_ingestion()` for every source (cache-aware, so 0 byte if nothing
     changed at the AN — see ETag/Last-Modified handling in download.py)
  2. Photo cache refresh (only downloads new portraits)

Exceptions are caught and logged ; the loop never aborts.

Disable with `ANQP_AUTO_REFRESH=0`. Override the period with
`ANQP_REFRESH_INTERVAL_S=3600` (e.g. for testing).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from ..config import settings
from ..db import connect
from ..ingestion.pipeline import run_ingestion
from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_INTERVAL_S = 24 * 3600
INITIAL_DELAY_S = 60     # let the server finish booting before checking

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _last_success_ts() -> float | None:
    """Return the UNIX timestamp of the most recent successful ingestion run."""
    try:
        conn = connect(read_only=True)
    except Exception as e:
        log.warning("refresh_loop_db_unavailable", extra={"error": str(e)})
        return None
    try:
        row = conn.execute(
            "SELECT MAX(finished_at) AS d FROM ingestion_runs WHERE status='success'"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["d"]:
        return None
    try:
        # SQLite stores naive UTC strings via datetime('now') — interpret as UTC.
        dt = datetime.fromisoformat(row["d"]).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _seconds_until_next_run(interval_s: int) -> int:
    last = _last_success_ts()
    if last is None:
        return 0
    elapsed = time.time() - last
    if elapsed >= interval_s:
        return 0
    return int(interval_s - elapsed)


def _refresh_all() -> None:
    """One full cycle : ingest every source + refresh photos."""
    started = time.time()
    log.info("refresh_loop_cycle_start")
    try:
        results = run_ingestion()
        log.info(
            "refresh_loop_ingestion_done",
            extra={"summary": {k: v.get("status") for k, v in results.items()}},
        )
    except Exception:
        log.exception("refresh_loop_ingestion_failed")
        return
    # Photos last : they only refresh missing portraits.
    try:
        from ..ingestion.photos import download_all_photos
        conn = connect()
        photo_res = download_all_photos(conn, workers=8)
        conn.close()
        log.info("refresh_loop_photos_done", extra={"photos": photo_res})
    except Exception:
        log.exception("refresh_loop_photos_failed")
    # Circo stats (INSEE pop + Min. Intérieur inscrits).
    try:
        from ..ingestion.circo_stats import ingest_circo_stats
        conn = connect()
        cs_res = ingest_circo_stats(conn)
        conn.close()
        log.info("refresh_loop_circo_stats_done", extra={"circo_stats": cs_res})
    except Exception:
        log.exception("refresh_loop_circo_stats_failed")
    log.info(
        "refresh_loop_cycle_done",
        extra={"elapsed_s": round(time.time() - started, 1)},
    )


def _loop(interval_s: int) -> None:
    log.info("refresh_loop_started", extra={"interval_s": interval_s})
    if _stop_event.wait(INITIAL_DELAY_S):
        return
    while not _stop_event.is_set():
        wait = _seconds_until_next_run(interval_s)
        if wait > 0:
            log.info(
                "refresh_loop_sleep",
                extra={"sleep_s": wait, "next_at_iso": datetime.utcfromtimestamp(time.time() + wait).isoformat() + "Z"},
            )
            if _stop_event.wait(wait):
                return
        _refresh_all()
        if _stop_event.wait(interval_s):
            return


def start_refresh_loop() -> None:
    """Launch the daemon thread (idempotent)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    interval_s = int(os.environ.get("ANQP_REFRESH_INTERVAL_S", DEFAULT_INTERVAL_S))
    _thread = threading.Thread(
        target=_loop, args=(interval_s,), name="anqp-refresh", daemon=True,
    )
    _thread.start()
    log.info("refresh_loop_thread_started", extra={"interval_s": interval_s})


def stop_refresh_loop() -> None:
    _stop_event.set()


def is_enabled() -> bool:
    return os.environ.get("ANQP_AUTO_REFRESH", "1") not in ("0", "false", "no", "off")


def next_run_in_seconds() -> int | None:
    """Seconds until the next planned refresh ; used by /a-propos to render
    the "next refresh in Xh" line."""
    if not is_enabled() or _thread is None or not _thread.is_alive():
        return None
    interval_s = int(os.environ.get("ANQP_REFRESH_INTERVAL_S", DEFAULT_INTERVAL_S))
    return _seconds_until_next_run(interval_s)
