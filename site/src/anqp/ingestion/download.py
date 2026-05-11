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
    with httpx.Client(timeout=settings.http_timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)

    if resp.status_code == 304 and out_path.exists():
        log.info(
            "download_not_modified",
            extra={"source": source, "elapsed_s": round(time.monotonic() - started, 2)},
        )
        return DownloadResult(
            source=source,
            url=url,
            file_path=out_path,
            bytes_downloaded=0,
            etag=cache.get("etag") if cache else None,
            last_modified=cache.get("last_modified") if cache else None,
            cache_hit=True,
        )
    resp.raise_for_status()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)

    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")
    cl = int(resp.headers.get("Content-Length", len(resp.content)))

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
