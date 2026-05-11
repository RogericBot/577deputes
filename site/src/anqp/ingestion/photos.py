"""Download deputy portraits to a local cache.

Photos are served from `/static/photos/{uid}.jpg` once cached. The
endpoint falls back to a placeholder SVG when the file is missing.

Cost : ~580 photos × ~10 KB = ~6 MB. Downloads in parallel with
threads, ETag-aware so re-runs are cheap.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from ..config import settings
from ..ingestion.deputies import _photo_url_remote
from ..logging_setup import get_logger

log = get_logger(__name__)


def _download_one(client: httpx.Client, uid: str, dst: Path) -> tuple[str, str]:
    if dst.exists() and dst.stat().st_size > 0:
        return uid, "cached"
    url = _photo_url_remote(uid, settings.legislature)
    try:
        resp = client.get(url, timeout=10.0)
        if resp.status_code == 200 and resp.content:
            dst.write_bytes(resp.content)
            return uid, "ok"
        return uid, f"http_{resp.status_code}"
    except Exception as e:
        return uid, f"error:{type(e).__name__}"


def download_all_photos(
    conn: sqlite3.Connection,
    *,
    only_active: bool = False,
    workers: int = 8,
) -> dict[str, int]:
    """Fetch every deputy photo. Returns counters."""
    sql = (
        "SELECT uid FROM deputies"
        + (" WHERE is_active = 1" if only_active else "")
    )
    uids = [r["uid"] for r in conn.execute(sql).fetchall()]
    settings.photos_dir.mkdir(parents=True, exist_ok=True)
    log.info("photos_download_start", extra={"count": len(uids)})

    counters = {"ok": 0, "cached": 0, "missing": 0, "error": 0}
    counters_lock = threading.Lock()

    with httpx.Client(
        timeout=10.0, follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for uid in uids:
                dst = settings.photos_dir / f"{uid}.jpg"
                futures.append(pool.submit(_download_one, client, uid, dst))
            done = 0
            for fut in as_completed(futures):
                uid, status = fut.result()
                done += 1
                with counters_lock:
                    if status == "ok":
                        counters["ok"] += 1
                    elif status == "cached":
                        counters["cached"] += 1
                    elif status.startswith("http_404"):
                        counters["missing"] += 1
                    else:
                        counters["error"] += 1
                if done % 100 == 0:
                    log.info("photos_progress", extra={"done": done, "total": len(uids)})

    log.info("photos_download_done", extra=counters)
    return counters
