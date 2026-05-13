"""HTTP download with conditional requests + ETag/Last-Modified cache.

Uses sqlite-stored metadata to avoid re-downloading unchanged files.
The Assemblée's CDN supports If-None-Match / If-Modified-Since.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class DownloadResult:
    source: str
    url: str
    file_path: Path
    bytes_downloaded: int
    etag: Optional[str]
    last_modified: Optional[str]
    cache_hit: bool      # True if 304 / locally identical, file_path still valid


def _cache_lookup(conn: sqlite3.Connection, source: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM source_cache WHERE source = ?", (source,)
    ).fetchone()
    return row


def _cache_upsert(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    etag: str | None,
    last_modified: str | None,
    content_length: int | None,
    file_path: Path,
) -> None:
    conn.execute(
        """
        INSERT INTO source_cache(source, url, etag, last_modified, content_length, fetched_at, file_path)
        VALUES(?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(source) DO UPDATE SET
            url = excluded.url,
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            content_length = excluded.content_length,
            fetched_at = excluded.fetched_at,
            file_path = excluded.file_path
        """,
        (source, url, etag, last_modified, content_length, str(file_path)),
    )


def download_source(
    conn: sqlite3.Connection,
    source: str,
    url: str | None = None,
    *,
    force: bool = False,
) -> DownloadResult:
    """Download a single source ZIP, with conditional GET against the cache."""
    url = url or settings.sources[source]
    out_path = settings.raw_dir / f"{source}.zip"
    cache = None if force else _cache_lookup(conn, source)

    headers = {"User-Agent": settings.user_agent}
    if cache:
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]

    started = time.monotonic()
    log.info("download_start", extra={"source": source, "url": url, "force": force})

    # The Assemblée's CDN regularly drops the connection mid-stream on the
    # largest dumps (Amendements.json.zip, hundreds of MB) — at random offsets.
    # It *does* honour Range requests (Accept-Ranges: bytes), so we stream to a
    # `.part` file and, on each retry, resume from where we left off with a
    # `Range: bytes=N-` header. Each attempt therefore makes progress and the
    # download converges. The conditional headers (If-None-Match / If-Modified-
    # Since) are only sent on the *first* attempt with an empty `.part`, never
    # on a resume (a 304 there would be wrong — we'd have a partial file).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_name(out_path.name + ".part")
    if force:
        part_path.unlink(missing_ok=True)
    etag = last_modified = None
    cl = 0
    have = part_path.stat().st_size if part_path.exists() else 0
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            req_headers = {"User-Agent": settings.user_agent}
            if have > 0:
                req_headers["Range"] = f"bytes={have}-"
            elif cache:
                if cache.get("etag"):
                    req_headers["If-None-Match"] = cache["etag"]
                if cache.get("last_modified"):
                    req_headers["If-Modified-Since"] = cache["last_modified"]
            with httpx.Client(timeout=settings.http_timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers=req_headers) as resp:
                    if resp.status_code == 304 and out_path.exists():
                        log.info(
                            "download_not_modified",
                            extra={"source": source,
                                   "elapsed_s": round(time.monotonic() - started, 2)},
                        )
                        return DownloadResult(
                            source=source, url=url, file_path=out_path,
                            bytes_downloaded=0,
                            etag=cache.get("etag") if cache else None,
                            last_modified=cache.get("last_modified") if cache else None,
                            cache_hit=True,
                        )
                    resp.raise_for_status()
                    etag = resp.headers.get("ETag")
                    last_modified = resp.headers.get("Last-Modified")
                    # If we asked for a Range but got 200, the server ignored it
                    # → restart from scratch.
                    resuming = (resp.status_code == 206 and have > 0)
                    if have > 0 and not resuming:
                        have = 0
                    # Total expected size: from Content-Range if present, else
                    # Content-Length (+ what we already have when resuming).
                    cr = resp.headers.get("Content-Range") or ""
                    expected = None
                    if "/" in cr and cr.rsplit("/", 1)[-1].isdigit():
                        expected = int(cr.rsplit("/", 1)[-1])
                    else:
                        clh = resp.headers.get("Content-Length")
                        if clh and clh.isdigit():
                            expected = int(clh) + (have if resuming else 0)
                    with open(part_path, "ab" if resuming else "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1 << 20):
                            f.write(chunk)
                            have += len(chunk)
                    if expected is not None and have < expected:
                        raise httpx.RemoteProtocolError(
                            f"incomplete body: {have}/{expected} bytes"
                        )
            part_path.replace(out_path)
            cl = expected if expected is not None else have
            break
        except httpx.HTTPStatusError as e:
            # Don't retry genuine client errors (404, etc.) — only 429/5xx.
            sc = e.response.status_code
            if not (sc == 429 or 500 <= sc < 600) or attempt == max_attempts:
                raise
            wait = min(30, 2 ** attempt)
            log.warning("download_retry", extra={"source": source, "attempt": attempt,
                                                 "status": sc, "have": have, "wait_s": wait})
            time.sleep(wait)
        except (httpx.TransportError, httpx.RemoteProtocolError) as e:
            have = part_path.stat().st_size if part_path.exists() else 0
            if attempt == max_attempts:
                raise
            wait = min(30, 2 ** attempt)
            log.warning("download_retry", extra={"source": source, "attempt": attempt,
                                                 "error": str(e), "have": have, "wait_s": wait})
            time.sleep(wait)
    else:  # pragma: no cover — loop always breaks or raises
        raise RuntimeError(f"download failed for {source} after {max_attempts} attempts")

    _cache_upsert(
        conn,
        source=source,
        url=url,
        etag=etag,
        last_modified=last_modified,
        content_length=cl,
        file_path=out_path,
    )
    log.info(
        "download_success",
        extra={
            "source": source,
            "bytes": cl,
            "etag": etag,
            "last_modified": last_modified,
            "elapsed_s": round(time.monotonic() - started, 2),
        },
    )
    return DownloadResult(
        source=source,
        url=url,
        file_path=out_path,
        bytes_downloaded=cl,
        etag=etag,
        last_modified=last_modified,
        cache_hit=False,
    )
