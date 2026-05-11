"""Detect near-identical amendments via MinHash + LSH-style bucketing.

Goal : group amendments whose dispositif is "essentially the same text"
across multiple authors, exposing concerted operations (same wording
copied between députés / groups).

Algorithm (stdlib only) :
  1. For each amendment, build the set of word 5-shingles from the
     normalised dispositif (HTML stripped, lowercased, accents folded).
  2. Compute K=64 MinHash signatures using `hash(salt + token)`.
  3. Bucket amendments by 16 bands of 4 signatures each (LSH). Pairs
     that collide in any band are candidates.
  4. Verify candidates with exact Jaccard on the shingles ; keep pairs
     ≥ THRESHOLD (default 0.80) and union-find them into clusters.

On ~108 k amendments, full run is ~5–8 minutes with stdlib hashing.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)

NUM_HASHES = 64
NUM_BANDS = 16
ROWS_PER_BAND = NUM_HASHES // NUM_BANDS    # 4
SHINGLE_K = 5
MIN_SHINGLES = 8                            # below = too short to cluster
JACCARD_THRESHOLD = 0.80
MAX_TEXT_LEN = 12000                        # truncate huge dispositifs


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")


def _normalise(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _NON_WORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:MAX_TEXT_LEN]


def _shingles(text: str, k: int = SHINGLE_K) -> set[int]:
    words = text.split()
    if len(words) < k:
        return set()
    out = set()
    for i in range(len(words) - k + 1):
        s = " ".join(words[i:i + k])
        out.add(_hash64(s))
    return out


def _hash64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


def _build_signature(shingles: set[int]) -> tuple[int, ...]:
    """Compute K=NUM_HASHES MinHash values over the shingle set."""
    if not shingles:
        return tuple([0] * NUM_HASHES)
    sig = [(1 << 64) - 1] * NUM_HASHES
    seeds = SEEDS  # precomputed list of K random salts (deterministic)
    for sh in shingles:
        for i, seed in enumerate(seeds):
            h = sh ^ seed
            if h < sig[i]:
                sig[i] = h
    return tuple(sig)


# Deterministic seeds.
SEEDS = [
    int.from_bytes(hashlib.blake2b(struct.pack(">I", i), digest_size=8).digest(), "little")
    for i in range(NUM_HASHES)
]


def _bands(sig: tuple[int, ...]) -> list[tuple[int, ...]]:
    return [
        sig[band * ROWS_PER_BAND:(band + 1) * ROWS_PER_BAND]
        for band in range(NUM_BANDS)
    ]


# ---------------------------------------------------------------------
# Union-find for clustering.
# ---------------------------------------------------------------------
class UF:
    def __init__(self) -> None:
        self.p: dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self.p:
            self.p[x] = x
            return x
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ---------------------------------------------------------------------
# Top-level computation
# ---------------------------------------------------------------------
def compute_clusters(conn: sqlite3.Connection, *, only_legislature: int | None = None) -> dict[str, int]:
    leg_filter = ""
    params: tuple = ()
    if only_legislature is not None:
        leg_filter = "WHERE legislature = ?"
        params = (only_legislature,)
    rows = conn.execute(
        f"SELECT uid, texte FROM amendements {leg_filter} ORDER BY uid",
        params,
    ).fetchall()

    log.info("amd_cluster_normalise_start", extra={"count": len(rows)})
    t0 = time.perf_counter()

    # 1. Normalise + shingle.
    shingles_by_idx: list[set[int]] = []
    uids: list[str] = []
    for r in rows:
        norm = _normalise(r["texte"])
        sh = _shingles(norm)
        if len(sh) < MIN_SHINGLES:
            continue
        uids.append(r["uid"])
        shingles_by_idx.append(sh)
    log.info("amd_cluster_signature_start", extra={"keep": len(uids)})

    # 2. Signatures.
    sigs: list[tuple[int, ...]] = []
    for sh in shingles_by_idx:
        sigs.append(_build_signature(sh))
        if len(sigs) % 5000 == 0:
            log.info("amd_cluster_signature_progress", extra={"done": len(sigs)})

    # 3. LSH bands → candidate buckets.
    log.info("amd_cluster_bucketing")
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, sig in enumerate(sigs):
        for b_i, band in enumerate(_bands(sig)):
            buckets[(b_i, band)].append(idx)

    # 4. Verify candidates with exact Jaccard, union-find pairs.
    log.info("amd_cluster_verify")
    uf = UF()
    seen_pairs: set = set()
    for bucket_members in buckets.values():
        if len(bucket_members) < 2:
            continue
        for i in range(len(bucket_members)):
            for j in range(i + 1, len(bucket_members)):
                a, b = bucket_members[i], bucket_members[j]
                key = (a, b) if a < b else (b, a)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                sa, sb = shingles_by_idx[a], shingles_by_idx[b]
                inter = len(sa & sb)
                if inter == 0:
                    continue
                union = len(sa) + len(sb) - inter
                jac = inter / union
                if jac >= JACCARD_THRESHOLD:
                    uf.union(a, b)

    # 5. Materialise clusters.
    log.info("amd_cluster_materialise")
    cluster_of: dict[int, int] = {}
    for idx in range(len(uids)):
        if idx in uf.p:
            root = uf.find(idx)
            cluster_of.setdefault(root, []).append(idx) if False else None  # noqa
    # Build root → members
    root_to_members: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(uids)):
        if idx in uf.p:
            root_to_members[uf.find(idx)].append(idx)

    rows_to_insert: list[tuple] = []
    cluster_id = 0
    for root, members in root_to_members.items():
        if len(members) < 2:
            continue
        cluster_id += 1
        for m in members:
            rows_to_insert.append((cluster_id, uids[m]))

    conn.execute("BEGIN")
    conn.execute("DELETE FROM amendement_clusters")
    if rows_to_insert:
        conn.executemany(
            "INSERT OR REPLACE INTO amendement_clusters(cluster_id, amendement_uid) VALUES (?, ?)",
            rows_to_insert,
        )
    conn.execute("COMMIT")

    elapsed = round(time.perf_counter() - t0, 1)
    log.info(
        "amd_cluster_done",
        extra={
            "amendements_considered": len(uids),
            "clusters": cluster_id,
            "amendements_clustered": len(rows_to_insert),
            "elapsed_s": elapsed,
        },
    )
    return {
        "clusters": cluster_id,
        "amendements_clustered": len(rows_to_insert),
        "elapsed_s": elapsed,
    }
