#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
methodo_sensitivity.py — Analyse de sensibilité aux paramètres des indicateurs
de 577deputes.fr (artefact reproductible accompagnant la note méthodologique).

Lecture seule sur une copie de la base SQLite (`site/data/anqp.db`).
Aucune dépendance hors bibliothèque standard.

Usage :
    python scripts/methodo_sensitivity.py [--db PATH] [--out DIR] [--quick]
                                          [--only A,B,C,...] [--sample N]

Sections :
    A  Détection d'amendements quasi-identiques (MinHash + LSH + Jaccard)
       A1 balayage du seuil Jaccard θ sur le jeu complet (LSH de production)
       A2 balayage de k (taille des shingles) et de θ sur un échantillon (Jaccard exact)
       A3 rappel théorique/empirique de LSH selon (bandes, lignes) et K
       A4 exposition à la troncature MAX_TEXT_LEN et au plancher MIN_SHINGLES
    B  Typologie des clusters (seuils 15 / 3)
    C  Discipline de vote (plancher d'exprimés ; valeurs de positionMajoritaire)
    D  Matrice de cohésion inter-groupes (effet « abstention partagée = accord »)
    F  Coalitions par scrutin (seuils votants / écart)
    G  Réponses ministérielles « types » (longueur de préfixe × seuil d'occurrences
       × retrait des formules d'appel)  ← priorité
    H  Absentéisme stratégique (grille de seuils)
    I  Amendements « fantômes » (plancher × définition de "défendu" ; vue continue)
    J  Délais ministériels (seuils du filtre)
    K  Couverture des statistiques de circonscription

Sortie : un dossier `--out` (défaut `methodo_sensitivity_out/`) contenant un
fichier .csv et un fichier .md par tableau, plus `SUMMARY.md` qui les agrège.
Les intermédiaires coûteux du clustering sont mis en cache dans
`<out>/_cache_*.pkl` pour accélérer les ré-exécutions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import pickle
import random
import re
import sqlite3
import struct
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Console Windows : forcer UTF-8 pour ne pas planter sur les accents / flèches.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --------------------------------------------------------------------------
# Constantes du clustering — gardées synchrones avec
# site/src/anqp/ingestion/amd_clusters.py (vérifier en cas de modification).
# --------------------------------------------------------------------------
PROD_NUM_HASHES   = 64
PROD_NUM_BANDS    = 16
PROD_ROWS_PER_BAND = PROD_NUM_HASHES // PROD_NUM_BANDS   # 4
PROD_SHINGLE_K    = 5
PROD_MIN_SHINGLES = 8
PROD_JACCARD      = 0.80
PROD_MAX_TEXT_LEN = 12000

_TAG_RE      = re.compile(r"<[^>]+>")
_WS_RE       = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")

# Constantes de typologie — synchrones avec
# site/src/anqp/web/cluster_typology.py
PROD_OBSTRUCTION_THRESHOLD = 15
PROD_AMPLIFICATION_MIN     = 3


# --------------------------------------------------------------------------
# Petits utilitaires
# --------------------------------------------------------------------------
_T0 = time.monotonic()


def log(msg: str) -> None:
    el = time.monotonic() - _T0
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts} | +{el:6.1f}s] {msg}", flush=True)


def progress(prefix: str, done: int, total: int, every: int) -> None:
    if done % every == 0 or done == total:
        pct = 100.0 * done / total if total else 100.0
        log(f"  {prefix}: {done:,}/{total:,} ({pct:4.1f} %)")


class TableWriter:
    """Écrit un même tableau en .csv et en .md, et l'empile dans SUMMARY.md."""

    def __init__(self, out_dir: Path, summary_lines: list[str]):
        self.out_dir = out_dir
        self.summary = summary_lines

    def write(self, name: str, title: str, header: list[str], rows: list[list],
              note: str = "") -> None:
        # CSV
        with (self.out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow(r)
        # Markdown
        def fmt(x):
            if isinstance(x, float):
                return f"{x:.4g}"
            return str(x)
        md = [f"### {title}", ""]
        if note:
            md.append(f"_{note}_")
            md.append("")
        md.append("| " + " | ".join(header) + " |")
        md.append("|" + "|".join("---" for _ in header) + "|")
        for r in rows:
            md.append("| " + " | ".join(fmt(x) for x in r) + " |")
        md.append("")
        (self.out_dir / f"{name}.md").write_text("\n".join(md), encoding="utf-8")
        # Summary
        self.summary.extend(md)
        log(f"  -> ecrit {name}.csv / {name}.md  ({len(rows)} lignes)")


# --------------------------------------------------------------------------
# Clustering — primitives (copie fidèle d'amd_clusters.py, paramétrées)
# --------------------------------------------------------------------------
def normalise(text: str | None, max_len: int = PROD_MAX_TEXT_LEN) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _NON_WORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_len]


def _hash64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


def shingles(text: str, k: int) -> set[int]:
    words = text.split()
    if len(words) < k:
        return set()
    out = set()
    for i in range(len(words) - k + 1):
        out.add(_hash64(" ".join(words[i:i + k])))
    return out


_SEEDS_CACHE: dict[int, list[int]] = {}


def _seeds(num_hashes: int) -> list[int]:
    if num_hashes not in _SEEDS_CACHE:
        _SEEDS_CACHE[num_hashes] = [
            int.from_bytes(hashlib.blake2b(struct.pack(">I", i), digest_size=8).digest(), "little")
            for i in range(num_hashes)
        ]
    return _SEEDS_CACHE[num_hashes]


def signature(sh: set[int], num_hashes: int) -> tuple[int, ...]:
    if not sh:
        return tuple([0] * num_hashes)
    sig = [(1 << 64) - 1] * num_hashes
    seeds = _seeds(num_hashes)
    for s in sh:
        for i, seed in enumerate(seeds):
            h = s ^ seed
            if h < sig[i]:
                sig[i] = h
    return tuple(sig)


def bands_of(sig: tuple[int, ...], num_bands: int, rows_per_band: int) -> list[tuple[int, ...]]:
    return [sig[b * rows_per_band:(b + 1) * rows_per_band] for b in range(num_bands)]


class UF:
    def __init__(self):
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


def jaccard(a: set[int], b: set[int]) -> float:
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def clusters_from_pairs(n_items: int, pairs: list[tuple[int, int]]) -> tuple[int, int]:
    """Renvoie (n_clusters, n_items_clusterisés) pour une liste de paires liées."""
    uf = UF()
    for a, b in pairs:
        uf.union(a, b)
    root_members: dict[int, int] = defaultdict(int)
    for idx in range(n_items):
        if idx in uf.p:
            root_members[uf.find(idx)] += 1
    n_clusters = sum(1 for c in root_members.values() if c >= 2)
    n_clustered = sum(c for c in root_members.values() if c >= 2)
    return n_clusters, n_clustered


# ==========================================================================
# SECTION A — amendements quasi-identiques
# ==========================================================================
def section_A(conn, tw: TableWriter, out_dir: Path, quick: bool, sample_n: int) -> None:
    log("SECTION A — détection d'amendements quasi-identiques")
    rows = conn.execute(
        "SELECT uid, groupe_uid, auteur_uid, texte FROM amendements ORDER BY uid"
    ).fetchall()
    log(f"  {len(rows):,} amendements lus")

    # ---- A4 : exposition à MAX_TEXT_LEN (avant troncature) -------------
    raw_lengths = []
    for r in rows:
        t = _TAG_RE.sub(" ", r["texte"] or "")
        t = unicodedata.normalize("NFKD", t)
        t = "".join(c for c in t if not unicodedata.combining(c))
        t = _NON_WORD_RE.sub(" ", t.lower())
        t = _WS_RE.sub(" ", t).strip()
        raw_lengths.append(len(t))
    a4 = []
    for thr in (3000, 6000, 12000, 24000, 48000, 100000):
        n_over = sum(1 for L in raw_lengths if L > thr)
        a4.append([thr, n_over, round(100.0 * n_over / len(raw_lengths), 3)])
    tw.write("A4_max_text_len", "A4 — Amendements dépassant le seuil de troncature (texte normalisé)",
             ["seuil_caracteres", "n_amendements_tronques", "pct"], a4,
             note=f"Production : MAX_TEXT_LEN = {PROD_MAX_TEXT_LEN}. "
                  "Au-delà, seul le préfixe est comparé.")

    # ---- Cache des shingles/signatures de production (k=5, K=64) -------
    cache_f = out_dir / "_cache_prod_shingles_sigs.pkl"
    if cache_f.exists():
        log("  cache shingles/signatures de production trouvé, chargement…")
        with cache_f.open("rb") as f:
            uids, gids, aids, sh_list, sig_list = pickle.load(f)
    else:
        log(f"  normalisation + shingles (k={PROD_SHINGLE_K})…")
        uids, gids, aids, sh_list = [], [], [], []
        for i, r in enumerate(rows):
            sh = shingles(normalise(r["texte"]), PROD_SHINGLE_K)
            if len(sh) < PROD_MIN_SHINGLES:
                continue
            uids.append(r["uid"]); gids.append(r["groupe_uid"]); aids.append(r["auteur_uid"])
            sh_list.append(sh)
            progress("shingles", i + 1, len(rows), 20000)
        log(f"  {len(uids):,} amendements retenus (≥ {PROD_MIN_SHINGLES} shingles)")
        log(f"  signatures MinHash (K={PROD_NUM_HASHES})…  (étape la plus longue)")
        sig_list = []
        for i, sh in enumerate(sh_list):
            sig_list.append(signature(sh, PROD_NUM_HASHES))
            progress("signatures", i + 1, len(sh_list), 5000)
        with cache_f.open("wb") as f:
            pickle.dump((uids, gids, aids, sh_list, sig_list), f)
        log("  cache écrit")

    n = len(uids)

    # ---- A4bis : effet du plancher MIN_SHINGLES -----------------------
    # (on a déjà coupé à 8 ; on recompte juste combien on couperait à d'autres seuils)
    sizes_sh = [len(s) for s in sh_list]
    # Pour les seuils < 8 on ne peut pas "récupérer" ce qu'on a déjà jeté ;
    # on indique donc seulement combien d'amendements supplémentaires seraient
    # exclus en montant le seuil au-dessus de 8.
    a4b = []
    for thr in (8, 12, 16, 24, 32, 50):
        n_excl = sum(1 for s in sizes_sh if s < thr)
        a4b.append([thr, n_excl, round(100.0 * n_excl / n, 3)])
    tw.write("A4b_min_shingles", "A4bis — Amendements exclus selon le plancher MIN_SHINGLES",
             ["plancher_shingles", "n_exclus_parmi_retenus", "pct"], a4b,
             note=f"Production : MIN_SHINGLES = {PROD_MIN_SHINGLES}. "
                  "Lecture : combien des amendements actuellement retenus seraient "
                  "perdus si l'on relevait le plancher.")

    # ---- A1 : LSH de production → toutes les paires candidates + Jaccard ----
    log("  bucketing LSH de production (16 bandes × 4 lignes)…")
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, sig in enumerate(sig_list):
        for b_i, band in enumerate(bands_of(sig, PROD_NUM_BANDS, PROD_ROWS_PER_BAND)):
            buckets[(b_i, band)].append(idx)
    log(f"  {len(buckets):,} buckets ; vérification Jaccard exacte des paires candidates…")
    cand_jac: list[float] = []          # Jaccard de chaque paire candidate distincte
    cand_pairs: list[tuple[int, int, float]] = []
    seen = set()
    nb_buckets = len(buckets)
    for bi, members in enumerate(buckets.values()):
        if bi % 200000 == 0:
            log(f"    bucket {bi:,}/{nb_buckets:,} ; paires vérifiées : {len(cand_pairs):,}")
        if len(members) < 2:
            continue
        # garde-fou : un bucket énorme (boilerplate) ⇒ on borne le nb de paires
        if len(members) > 4000:
            members = members[:4000]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                jv = jaccard(sh_list[a], sh_list[b])
                if jv > 0:
                    cand_jac.append(jv)
                    if jv >= 0.50:                # on ne garde le détail que pour le balayage
                        cand_pairs.append((a, b, jv))
    log(f"  {len(cand_jac):,} paires candidates avec Jaccard > 0 "
        f"(dont {len(cand_pairs):,} ≥ 0,50)")

    # Balayage du seuil θ
    a1 = []
    for theta in (0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.90, 0.95):
        kept = [(a, b) for (a, b, jv) in cand_pairs if jv >= theta]
        nc, ncl = clusters_from_pairs(n, kept)
        a1.append([theta, len(kept), nc, ncl, round(100.0 * ncl / n, 2)])
    tw.write("A1_jaccard_threshold_full",
             "A1 — Effet du seuil Jaccard θ (jeu complet, LSH de production)",
             ["theta", "n_paires_retenues", "n_clusters", "n_amendements_clusterises",
              "pct_amendements_clusterises"], a1,
             note=f"Production : θ = {PROD_JACCARD}. Jeu complet = {n:,} amendements "
                  f"retenus. ATTENTION : la couverture des paires candidates par le "
                  f"LSH 16×4 décroît pour θ < 0,80 (les comptes y sont des bornes "
                  f"basses) ; le tableau A2 donne la courbe θ exacte sur échantillon.")

    # Histogramme grossier des Jaccard candidats (pour la figure du PDF)
    hist_edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0001]
    hist = [0] * (len(hist_edges) - 1)
    for jv in cand_jac:
        for i in range(len(hist_edges) - 1):
            if hist_edges[i] <= jv < hist_edges[i + 1]:
                hist[i] += 1
                break
    tw.write("A1b_jaccard_histogram",
             "A1bis — Distribution des similarités Jaccard des paires candidates",
             ["intervalle_jaccard", "n_paires"],
             [[f"[{hist_edges[i]:.2f}, {hist_edges[i+1]:.2f})", hist[i]]
              for i in range(len(hist))])

    # ---- A2 : k et θ sur un échantillon, Jaccard EXACT toutes paires ----
    rng = random.Random(20240101)
    pool = list(range(len(rows)))
    rng.shuffle(pool)
    sample_idx = pool[: (3000 if quick else sample_n)]
    sample_texts = [rows[i]["texte"] for i in sample_idx]
    log(f"  A2 : échantillon de {len(sample_texts):,} amendements, Jaccard exact toutes paires "
        f"pour k ∈ {{3,5,7}}…")
    a2 = []
    for k in (3, 5, 7):
        sh_s = []
        for t in sample_texts:
            s = shingles(normalise(t), k)
            sh_s.append(s if len(s) >= PROD_MIN_SHINGLES else None)
        keep = [i for i, s in enumerate(sh_s) if s is not None]
        m = len(keep)
        # toutes les paires
        jacs: list[tuple[int, int, float]] = []
        for ii in range(m):
            si = sh_s[keep[ii]]
            for jj in range(ii + 1, m):
                jv = jaccard(si, sh_s[keep[jj]])
                if jv >= 0.50:
                    jacs.append((ii, jj, jv))
        for theta in (0.70, 0.75, 0.80, 0.85, 0.90):
            kept = [(a, b) for (a, b, jv) in jacs if jv >= theta]
            nc, ncl = clusters_from_pairs(m, kept)
            a2.append([k, m, theta, len(kept), nc, ncl,
                       round(100.0 * ncl / m, 2) if m else 0.0])
    tw.write("A2_k_and_theta_sample",
             "A2 — Effet de la taille des shingles k et du seuil θ (échantillon, Jaccard exact)",
             ["k_shingle", "n_amendements_echantillon", "theta", "n_paires_retenues",
              "n_clusters", "n_amendements_clusterises", "pct"], a2,
             note=f"Production : k = {PROD_SHINGLE_K}. Échantillon aléatoire (graine fixe), "
                  "toutes les paires comparées exactement — pas de biais LSH.")

    # ---- A3 : courbe LSH (théorique) selon (bandes, lignes) -----------
    a3 = []
    for (b, r) in [(16, 4), (8, 8), (32, 2), (20, 5), (10, 5), (25, 4), (4, 16), (64, 1)]:
        K = b * r
        row = [f"{b}×{r}", K]
        for s in (0.50, 0.70, 0.80, 0.90, 0.95):
            p = 1.0 - (1.0 - s ** r) ** b
            row.append(round(p, 4))
        a3.append(row)
    tw.write("A3_lsh_curve",
             "A3 — Probabilité qu'une paire de similarité s soit candidate, selon (bandes×lignes)",
             ["bandes_x_lignes", "K_total", "P(s=0.50)", "P(s=0.70)", "P(s=0.80)",
              "P(s=0.90)", "P(s=0.95)"], a3,
             note=f"Production : {PROD_NUM_BANDS} bandes × {PROD_ROWS_PER_BAND} lignes "
                  f"(K = {PROD_NUM_HASHES}). Formule : P = 1 − (1 − s^r)^b. "
                  "Le LSH n'affecte que le RAPPEL des paires candidates ; la "
                  "précision finale est garantie par la vérification Jaccard exacte.")

    # ---- B : typologie des clusters (sur les clusters de production θ=0.80) ----
    log("SECTION B — typologie des clusters")
    kept_prod = [(a, b) for (a, b, jv) in cand_pairs if jv >= PROD_JACCARD]
    uf = UF()
    for a, b in kept_prod:
        uf.union(a, b)
    root_members: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        if idx in uf.p:
            root_members[uf.find(idx)].append(idx)
    clusters = [members for members in root_members.values() if len(members) >= 2]
    log(f"  {len(clusters):,} clusters reconstruits ; "
        f"{sum(len(c) for c in clusters):,} amendements clusterisés "
        f"(production en base : 28 055)")
    cl_meta = []
    for members in clusters:
        size = len(members)
        ngroups = len({gids[m] for m in members})
        cl_meta.append((size, ngroups))

    def typ(size, ng, obs_thr, ampl_min):
        if size >= obs_thr:
            return "depot_masse"
        if ng >= 2:
            return "convergence"
        if ng <= 1 and size >= ampl_min:
            return "amplification"
        return "reutilisation"

    b_rows = []
    for obs_thr in (10, 15, 20, 30, 50):
        for ampl_min in (2, 3, 5):
            cnt = defaultdict(int); amd = defaultdict(int)
            for (size, ng) in cl_meta:
                tk = typ(size, ng, obs_thr, ampl_min)
                cnt[tk] += 1; amd[tk] += size
            b_rows.append([obs_thr, ampl_min,
                           cnt["depot_masse"], amd["depot_masse"],
                           cnt["convergence"], amd["convergence"],
                           cnt["amplification"], amd["amplification"],
                           cnt["reutilisation"], amd["reutilisation"]])
    tw.write("B_typology_thresholds",
             "B — Répartition des clusters selon les seuils de typologie",
             ["seuil_depot_masse", "seuil_amplification",
              "n_cl_depot_masse", "n_amdt_depot_masse",
              "n_cl_convergence", "n_amdt_convergence",
              "n_cl_amplification", "n_amdt_amplification",
              "n_cl_reutilisation", "n_amdt_reutilisation"], b_rows,
             note=f"Production : seuil « dépôt en masse » = {PROD_OBSTRUCTION_THRESHOLD}, "
                  f"seuil « amplification » = {PROD_AMPLIFICATION_MIN}. Ordre de priorité : "
                  "dépôt en masse > convergence > amplification > réutilisation.")
    # distribution des tailles de cluster
    size_hist = defaultdict(int)
    for (size, ng) in cl_meta:
        bucket = (2 if size == 2 else 3 if size == 3 else 4 if size in (4, 5)
                  else 6 if size <= 9 else 10 if size <= 14 else 15 if size <= 29
                  else 30 if size <= 99 else 100)
        size_hist[bucket] += 1
    tw.write("B2_cluster_size_dist", "B2 — Distribution de la taille des clusters",
             ["taille_min_de_la_classe", "n_clusters"],
             [[k, size_hist[k]] for k in sorted(size_hist)])


# ==========================================================================
# SECTION C — discipline
# ==========================================================================
def section_C(conn, tw: TableWriter) -> None:
    log("SECTION C — discipline de vote")
    # valeurs de positionMajoritaire
    pm = conn.execute(
        "SELECT COALESCE(position_majoritaire,'(NULL)') AS v, COUNT(*) AS n "
        "FROM scrutin_groupes GROUP BY v ORDER BY n DESC"
    ).fetchall()
    tot_sg = sum(r["n"] for r in pm)
    tw.write("C0_position_majoritaire_values",
             "C0 — Valeurs prises par positionMajoritaire (source AN)",
             ["valeur", "n_lignes_groupe_x_scrutin", "pct"],
             [[r["v"], r["n"], round(100.0 * r["n"] / tot_sg, 3)] for r in pm],
             note="Lecture : si la colonne ne contient jamais NULL ni autre chose que "
                  "{pour, contre, abstention}, il n'y a aucun cas d'égalité parfaite à "
                  "arbitrer côté indicateur — l'éventuel départage est fait en amont par "
                  "l'Assemblée et n'est pas observable ici.")

    # plancher d'exprimés
    rows = conn.execute(
        "SELECT acteur_uid, expressed, aligned FROM deputy_discipline_cache "
        "WHERE expressed > 0"
    ).fetchall()
    pcts_all = [(r["expressed"], 100.0 * r["aligned"] / r["expressed"]) for r in rows]
    c_rows = []
    for thr in (0, 10, 20, 30, 50, 75, 100, 150, 200):
        sub = [p for (e, p) in pcts_all if e >= thr]
        if not sub:
            c_rows.append([thr, 0, "", "", "", "", ""]); continue
        sub_sorted = sorted(sub)
        n = len(sub_sorted)
        mean = sum(sub_sorted) / n
        med = sub_sorted[n // 2]
        p10 = sub_sorted[int(0.10 * n)]
        p90 = sub_sorted[int(0.90 * n)]
        c_rows.append([thr, n, round(mean, 2), round(med, 2), round(p10, 2),
                       round(p90, 2), round(max(sub_sorted), 2)])
    tw.write("C1_discipline_floor",
             "C1 — Effet du plancher de votes exprimés sur le classement de discipline",
             ["plancher_exprimes", "n_deputes_eligibles", "discipline_moy",
              "mediane", "p10", "p90", "max"], c_rows,
             note="Production : plancher = 50 (classements) ou 100 (dissidents). "
                  "Lecture : la moyenne et les extrêmes bougent-ils beaucoup quand on "
                  "change le plancher ? (un classement robuste varie peu).")


# ==========================================================================
# SECTION D — matrice de cohésion : effet de "abstention partagée = accord"
# ==========================================================================
def section_D(conn, tw: TableWriter) -> None:
    log("SECTION D — matrice de cohésion inter-groupes")
    pairs = conn.execute(
        """
        SELECT sg1.groupe_uid AS g1, sg2.groupe_uid AS g2,
               COUNT(*) AS commun,
               SUM(CASE WHEN sg1.position_majoritaire = sg2.position_majoritaire THEN 1 ELSE 0 END) AS aligned,
               SUM(CASE WHEN sg1.position_majoritaire = sg2.position_majoritaire
                         AND sg1.position_majoritaire = 'abstention' THEN 1 ELSE 0 END) AS aligned_abst
          FROM scrutin_groupes sg1
          JOIN scrutin_groupes sg2
            ON sg1.scrutin_uid = sg2.scrutin_uid AND sg1.groupe_uid < sg2.groupe_uid
         WHERE sg1.position_majoritaire IN ('pour','contre','abstention')
           AND sg2.position_majoritaire IN ('pour','contre','abstention')
         GROUP BY sg1.groupe_uid, sg2.groupe_uid
        """
    ).fetchall()
    abrev = dict(conn.execute(
        "SELECT uid, COALESCE(libelle_abrege, uid) FROM organes WHERE code_type='GP'"
    ).fetchall())
    d_rows = []
    deltas = []
    for r in pairs:
        if r["commun"] < 20:
            continue
        score_incl = 100.0 * r["aligned"] / r["commun"]
        # score excluant les scrutins où les deux s'abstiennent
        denom_excl = r["commun"] - r["aligned_abst"]   # on retire les paires "abst+abst"
        num_excl = r["aligned"] - r["aligned_abst"]
        score_excl = (100.0 * num_excl / denom_excl) if denom_excl else None
        delta = (score_incl - score_excl) if score_excl is not None else None
        if delta is not None:
            deltas.append(abs(delta))
        d_rows.append([abrev.get(r["g1"], r["g1"]), abrev.get(r["g2"], r["g2"]),
                       r["commun"], round(score_incl, 1),
                       round(score_excl, 1) if score_excl is not None else "",
                       round(delta, 2) if delta is not None else ""])
    d_rows.sort(key=lambda x: (abs(x[5]) if isinstance(x[5], float) else 0), reverse=True)
    note_extra = ""
    if deltas:
        note_extra = (f" Écart absolu moyen entre les deux variantes : "
                      f"{sum(deltas)/len(deltas):.2f} pts ; max : {max(deltas):.2f} pts.")
    tw.write("D_matrix_abstention_effect",
             "D — Cohésion entre groupes : avec vs sans les scrutins « abstention partagée »",
             ["groupe_1", "groupe_2", "n_scrutins_communs", "cohesion_avec_abst_pct",
              "cohesion_sans_abst_pct", "delta_points"], d_rows,
             note="Production : « abstention identique » compte comme accord. Ce tableau "
                  "montre de combien la cohésion changerait si on excluait ces cas." + note_extra)


# ==========================================================================
# SECTION F — coalitions par scrutin (seuils votants / écart)
# ==========================================================================
def section_F(conn, tw: TableWriter) -> None:
    log("SECTION F — coalitions par scrutin")
    f_rows = []
    for minv in (200, 250, 300, 350, 400):
        for maxe in (10, 25, 50, 75, 100, 99999):
            n = conn.execute(
                "SELECT COUNT(*) FROM scrutins "
                "WHERE (nb_pour+nb_contre+nb_abstentions) >= ? "
                "AND ABS(nb_pour-nb_contre) <= ?", (minv, maxe)
            ).fetchone()[0]
            f_rows.append([minv, maxe if maxe < 99999 else "∞", n])
    tw.write("F_coalitions_topic_thresholds",
             "F — Nombre de scrutins « très suivis et clivants » selon les seuils",
             ["min_votants_exprimes", "max_ecart_pour_contre", "n_scrutins_eligibles"], f_rows,
             note="Production : min_votants = 300, max_écart = 50 (puis fallback sans le "
                  "second critère si trop peu de scrutins).")


# ==========================================================================
# SECTION G — réponses ministérielles "types"  (PRIORITÉ)
# ==========================================================================
def section_G(conn, tw: TableWriter, quick: bool) -> None:
    log("SECTION G — réponses ministérielles « types »  (priorité)")
    rows = conn.execute(
        "SELECT uid, ministere_interroge_court, auteur_nom_complet, texte_reponse "
        "FROM questions WHERE texte_reponse IS NOT NULL AND length(texte_reponse) > 200"
    ).fetchall()
    log(f"  {len(rows):,} réponses brutes (> 200 car. avec HTML) ; normalisation…")
    # Normalisation : strip HTML, espaces ; deux variantes : avec / sans retrait
    # des formules d'appel.
    SALUT = ("monsieur le", "madame la", "monsieur le depute", "madame la deputee",
             "monsieur, madame", "messieurs")

    def norm_variants(txt: str):
        t = _TAG_RE.sub(" ", txt or "")
        t = _WS_RE.sub(" ", t).strip()
        if len(t) < 100:
            return None, None
        # variante "avec retrait des formules d'appel" (= production)
        t2 = t
        low = t.lower()
        # retrait des accents pour le test de préfixe (le code de prod teste sur
        # 'lower' non désaccentué ; on reproduit ça à l'identique)
        if low.startswith(SALUT):
            tail = t.split(",", 1)[-1].strip()
            if len(tail) > 100:
                t2 = tail
        return t, t2   # (sans retrait, avec retrait)

    cleaned_raw, cleaned_strip = [], []
    metas = []
    for i, r in enumerate(rows):
        a, b = norm_variants(r["texte_reponse"])
        if a is None:
            continue
        cleaned_raw.append(a.lower())
        cleaned_strip.append(b.lower())
        metas.append((r["ministere_interroge_court"], r["auteur_nom_complet"]))
        progress("normalisation", i + 1, len(rows), 5000)
    total_elig = len(cleaned_raw)
    log(f"  {total_elig:,} réponses éligibles (> 100 car. nettoyés)")

    lengths = (100, 150, 200, 250, 300, 400, 500, 750, 1000) if not quick else (100, 250, 500, 1000)
    occ_thresholds = (2, 3, 5, 10)
    g_rows = []
    for variant_name, corpus in (("avec retrait formules", cleaned_strip),
                                 ("sans retrait formules", cleaned_raw)):
        for L in lengths:
            by_prefix: dict[str, list[int]] = defaultdict(list)
            for idx, txt in enumerate(corpus):
                by_prefix[txt[:L]].append(idx)
            for m in occ_thresholds:
                clusters = [v for v in by_prefix.values() if len(v) >= m]
                n_clusters = len(clusters)
                n_in = sum(len(v) for v in clusters)
                biggest = max((len(v) for v in clusters), default=0)
                g_rows.append([variant_name, L, m, n_clusters, n_in,
                               round(100.0 * n_in / total_elig, 2) if total_elig else 0.0,
                               biggest])
    tw.write("G_minister_templates_grid",
             "G — Taux de réponses « types » selon la longueur de préfixe L, le seuil "
             "d'occurrences m et le retrait des formules d'appel",
             ["variante", "longueur_prefixe_L", "seuil_occurrences_m", "n_clusters",
              "n_reponses_dans_clusters", "taux_template_pct", "taille_du_plus_gros_cluster"],
             g_rows,
             note=f"Production : L = 250, m = 3, variante « avec retrait des formules » "
                  f"→ c'est la cellule de référence (le 27,9 % affiché). Total de "
                  f"réponses éligibles : {total_elig:,}.")


# ==========================================================================
# SECTION H — absentéisme stratégique
# ==========================================================================
def section_H(conn, tw: TableWriter, quick: bool) -> None:
    log("SECTION H — absentéisme stratégique")
    # On précharge, par scrutin, (total_exprimes, ecart_rel) ; puis on classe en
    # Python pour balayer la grille sans relire 1M votes à chaque fois.
    scr = conn.execute(
        "SELECT uid, (nb_pour+nb_contre+nb_abstentions) AS tot, "
        "ABS(nb_pour-nb_contre) AS ecart FROM scrutins"
    ).fetchall()
    # votes : on charge (acteur, scrutin, present) une seule fois — 1M lignes.
    log("  chargement des ~1 M votes…")
    pres_by_scr: dict[str, list[tuple[str, int]]] = defaultdict(list)
    cnt = 0
    for acteur, scr_uid, position in conn.execute(
        "SELECT acteur_uid, scrutin_uid, position FROM votes"
    ):
        present = 1 if position in ("pour", "contre", "abstention") else 0
        pres_by_scr[scr_uid].append((acteur, present))
        cnt += 1
        if cnt % 200000 == 0:
            log(f"    votes lus : {cnt:,}")
    log(f"  {cnt:,} votes chargés")
    actifs = set(r[0] for r in conn.execute("SELECT uid FROM deputies WHERE is_active=1"))

    grid_minv = (150, 200, 250, 300) if not quick else (200,)
    grid_cli = (0.05, 0.10, 0.15)
    grid_con = (0.40, 0.50, 0.60)
    grid_minc = (20, 30, 50)
    h_rows = []
    for minv in grid_minv:
        for cli_t in grid_cli:
            for con_t in grid_con:
                # classer les scrutins
                cli_set, con_set = set(), set()
                for s in scr:
                    if s["tot"] is None or s["tot"] < minv:
                        continue
                    rel = s["ecart"] / s["tot"] if s["tot"] else 1.0
                    if rel < cli_t:
                        cli_set.add(s["uid"])
                    elif rel > con_t:
                        con_set.add(s["uid"])
                # agréger par député
                agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # pc, tc, pco, tco
                for su in cli_set:
                    for (a, p) in pres_by_scr.get(su, ()):
                        agg[a][1] += 1; agg[a][0] += p
                for su in con_set:
                    for (a, p) in pres_by_scr.get(su, ()):
                        agg[a][3] += 1; agg[a][2] += p
                gaps = []
                for a, (pc, tc, pco, tco) in agg.items():
                    if a not in actifs or tc == 0 or tco == 0:
                        continue
                    g = 100.0 * pco / tco - 100.0 * pc / tc
                    gaps.append(g)
                for minc in grid_minc:
                    # n eligibles = ceux avec tot_cli >= minc
                    n_elig = sum(1 for a, (pc, tc, pco, tco) in agg.items()
                                 if a in actifs and tc >= minc and tco > 0)
                    elig_gaps = sorted(
                        (100.0 * pco / tco - 100.0 * pc / tc)
                        for a, (pc, tc, pco, tco) in agg.items()
                        if a in actifs and tc >= minc and tco > 0
                    )
                    top_gap = elig_gaps[-1] if elig_gaps else None
                    med_gap = elig_gaps[len(elig_gaps) // 2] if elig_gaps else None
                    h_rows.append([minv, cli_t, con_t, minc, len(cli_set), len(con_set),
                                   n_elig,
                                   round(top_gap, 1) if top_gap is not None else "",
                                   round(med_gap, 1) if med_gap is not None else ""])
    tw.write("H_strategic_absence_grid",
             "H — Absentéisme stratégique : sensibilité aux quatre seuils",
             ["min_votants", "seuil_clivant", "seuil_consensuel", "min_clivants",
              "n_scrutins_clivants", "n_scrutins_consensuels", "n_deputes_eligibles",
              "gap_max_pts", "gap_median_pts"], h_rows,
             note="Production : min_votants = 200, clivant si écart relatif < 0,10, "
                  "consensuel si > 0,50, min_clivants = 30. Le « gap » = présence sur "
                  "consensuels − présence sur clivants (en points).")


# ==========================================================================
# SECTION I — amendements "fantômes"
# ==========================================================================
def section_I(conn, tw: TableWriter) -> None:
    log("SECTION I — amendements « fantômes »")
    # recensement des codes de sort
    sorts = conn.execute(
        "SELECT COALESCE(NULLIF(sort,''),'(vide/NULL)') AS s, COUNT(*) AS n "
        "FROM amendements GROUP BY s ORDER BY n DESC"
    ).fetchall()
    tot_a = sum(r["n"] for r in sorts)
    tw.write("I0_sort_codes", "I0 — Recensement des codes « sort » des amendements",
             ["sort", "n_amendements", "pct"],
             [[r["s"], r["n"], round(100.0 * r["n"] / tot_a, 2)] for r in sorts],
             note="« défendu » (prod) = {Adopté, Rejeté, Retiré, Discuté} ; "
                  "« fantôme » (prod) = {Non soutenu} ∪ {sort vide/NULL}. Les autres "
                  "(Irrecevable, En traitement, Tombé, Effacé) ne sont comptés ni "
                  "comme défendus ni comme fantômes.")
    # par auteur
    rows = conn.execute(
        """
        SELECT a.auteur_uid AS uid, COUNT(*) AS total,
               SUM(CASE WHEN a.sort IN ('Adopté','Rejeté','Retiré','Discuté') THEN 1 ELSE 0 END) AS defendus,
               SUM(CASE WHEN a.sort = 'Non soutenu' THEN 1 ELSE 0 END) AS non_soutenus,
               SUM(CASE WHEN a.sort IS NULL OR a.sort = '' THEN 1 ELSE 0 END) AS sans_sort
          FROM amendements a WHERE a.auteur_uid IS NOT NULL GROUP BY a.auteur_uid
        """
    ).fetchall()
    active = set(r[0] for r in conn.execute("SELECT uid FROM deputies WHERE is_active=1"))
    data = []
    for r in rows:
        fant = r["non_soutenus"] + r["sans_sort"]
        data.append((r["uid"], r["total"], r["defendus"], fant,
                     100.0 * fant / r["total"], r["uid"] in active))
    i_rows = []
    for thr in (5, 10, 20, 50, 100, 200):
        sub = [d for d in data if d[1] >= thr]
        sub_active = [d for d in sub if d[5]]
        if not sub:
            i_rows.append([thr, 0, 0, "", "", ""]); continue
        pcts = sorted(d[4] for d in sub)
        top = max(sub, key=lambda d: d[4])
        i_rows.append([thr, len(sub), len(sub_active), round(sum(pcts)/len(pcts), 2),
                       round(pcts[len(pcts)//2], 2), round(top[4], 1)])
    tw.write("I1_phantom_floor", "I1 — Amendements fantômes : effet du plancher de dépôts",
             ["plancher_amendements", "n_auteurs_eligibles", "dont_actifs",
              "pct_fantomes_moyen", "mediane", "pct_fantomes_max"], i_rows,
             note="Production : plancher = 50.")
    # vue continue : (total, pct_fantomes, actif) — pour la figure du PDF
    cont = sorted(((d[1], round(d[4], 2), int(d[5])) for d in data if d[1] >= 5),
                  key=lambda x: -x[0])
    tw.write("I2_phantom_scatter", "I2 — Vue continue (nuage de points) — sans plancher",
             ["total_amendements_deposes", "pct_fantomes", "actif"], cont,
             note="Chaque ligne = un auteur (≥ 5 amendements). À tracer en nuage de "
                  "points : abscisse = total déposé, ordonnée = % fantômes.")


# ==========================================================================
# SECTION J — délais ministériels
# ==========================================================================
def section_J(conn, tw: TableWriter) -> None:
    log("SECTION J — délais de réponse ministériels")
    base = conn.execute(
        "SELECT ministere_interroge_court AS m, COUNT(*) AS total, "
        "SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS rep, "
        "AVG(delai_reponse_jours) AS delai "
        "FROM questions WHERE ministere_interroge_court IS NOT NULL "
        "GROUP BY ministere_interroge_court"
    ).fetchall()
    j_rows = []
    for mt in (10, 20, 30, 50, 100):
        for mr in (1, 3, 5, 10):
            sub = [r for r in base if r["total"] >= mt and (r["rep"] or 0) >= mr]
            j_rows.append([mt, mr, len(sub)])
    tw.write("J_ministry_thresholds",
             "J — Nombre de ministères retenus dans le classement « les plus lents »",
             ["min_questions_total", "min_questions_repondues", "n_ministeres"], j_rows,
             note="Production : min_total = 30, min_répondues = 5.")


# ==========================================================================
# SECTION K — couverture circo
# ==========================================================================
def section_K(conn, tw: TableWriter) -> None:
    log("SECTION K — couverture des statistiques de circonscription")
    tot = conn.execute("SELECT COUNT(*) FROM circo_stats").fetchone()[0]
    pop = conn.execute("SELECT COUNT(*) FROM circo_stats WHERE population IS NOT NULL").fetchone()[0]
    ins = conn.execute("SELECT COUNT(*) FROM circo_stats WHERE inscrits IS NOT NULL").fetchone()[0]
    vot = conn.execute("SELECT COUNT(*) FROM circo_stats WHERE votants IS NOT NULL").fetchone()[0]
    nd = conn.execute("SELECT COUNT(*) FROM deputies WHERE is_active=1 AND circonscription IS NOT NULL").fetchone()[0]
    tw.write("K_circo_coverage", "K — Couverture des données de circonscription",
             ["indicateur", "valeur"],
             [["lignes circo_stats", tot], ["avec population (INSEE)", pop],
              ["avec inscrits (Min. Intérieur)", ins], ["avec votants", vot],
              ["circonscriptions de députés actifs", nd]],
             note="Les 11 circonscriptions des Français de l'étranger n'ont pas de "
                  "population INSEE rattachée.")


# ==========================================================================
# main
# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent
    ap.add_argument("--db", default=str(here.parent / "site" / "data" / "anqp.db"))
    ap.add_argument("--out", default=str(here.parent / "methodo_sensitivity_out"))
    ap.add_argument("--quick", action="store_true", help="échantillons réduits, grilles allégées")
    ap.add_argument("--sample", type=int, default=6000, help="taille de l'échantillon pour A2")
    ap.add_argument("--only", default="", help="sections à exécuter, ex. 'G,C,A'")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"DB  : {args.db}")
    log(f"OUT : {out_dir}")
    log(f"quick={args.quick}  sample={args.sample}")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    summary: list[str] = [
        "# Analyse de sensibilité — 577deputes.fr",
        "",
        f"_Généré le {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"base : `{Path(args.db).name}`. Voir `scripts/methodo_sensitivity.py`._",
        "",
    ]
    tw = TableWriter(out_dir, summary)

    sections = {
        "G": lambda: section_G(conn, tw, args.quick),     # priorité d'abord
        "C": lambda: section_C(conn, tw),
        "D": lambda: section_D(conn, tw),
        "F": lambda: section_F(conn, tw),
        "I": lambda: section_I(conn, tw),
        "J": lambda: section_J(conn, tw),
        "K": lambda: section_K(conn, tw),
        "H": lambda: section_H(conn, tw, args.quick),
        "A": lambda: section_A(conn, tw, out_dir, args.quick, args.sample),  # B inclus dans A
    }
    order = [s.strip().upper() for s in args.only.split(",") if s.strip()] or list(sections.keys())
    for key in order:
        fn = sections.get(key)
        if not fn:
            log(f"!! section inconnue : {key}")
            continue
        try:
            fn()
        except Exception as e:
            import traceback
            log(f"!! ÉCHEC section {key} : {e}")
            traceback.print_exc()
            summary.append(f"\n> ⚠️ Section {key} en échec : `{e}`\n")

    (out_dir / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    log(f"TERMINÉ. Résumé : {out_dir / 'SUMMARY.md'}")
    conn.close()


if __name__ == "__main__":
    main()
