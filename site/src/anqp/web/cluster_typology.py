"""Typologie des clusters d'amendements quasi-identiques.

Classifie chaque cluster en 4 catégories selon sa composition :

  - obstruction  : ≥ 15 amendements (manœuvre de blocage / saturation
                   du débat, peu importe la composition partisane)
  - convergence  : ≥ 2 groupes différents (consensus technique ou
                   alliance opportuniste — signale les positions partagées)
  - amplification : 1 seul groupe, ≥ 3 amendements (stratégie collective
                    d'un groupe pour pousser un point)
  - reutilisation : 2-3 amendements, peu importe le groupe (réécriture
                    triviale d'un modèle, faible signal politique)

L'ordre de priorité est : obstruction > convergence > amplification >
reutilisation. La taille (obstruction) prime toujours.

Calibration sur la 17e legislature (mai 2026) :
    obstruction   :     91 clusters
    convergence   :  4 200 clusters env.
    amplification :  2 800 clusters env.
    reutilisation :  2 300 clusters env.
"""
from __future__ import annotations

import sqlite3


OBSTRUCTION_THRESHOLD = 15
AMPLIFICATION_MIN_SIZE = 3


CLUSTER_TYPES = {
    "obstruction": {
        "key": "obstruction",
        "label": "Obstruction",
        "description": "Cluster volumineux (≥ 15 amendements quasi-identiques) — souvent une tactique de blocage du débat.",
        "color": "#dc2626",
        "order": 1,
    },
    "convergence": {
        "key": "convergence",
        "label": "Convergence inter-groupes",
        "description": "Au moins deux groupes parlementaires différents portent la même rédaction — consensus technique ou alliance opportuniste.",
        "color": "#0891b2",
        "order": 2,
    },
    "amplification": {
        "key": "amplification",
        "label": "Amplification intra-groupe",
        "description": "Un seul groupe parlementaire dépose le même amendement à plusieurs reprises — stratégie collective de visibilité.",
        "color": "#7c3aed",
        "order": 3,
    },
    "reutilisation": {
        "key": "reutilisation",
        "label": "Réutilisation simple",
        "description": "Doublon ou triplet trivial — souvent la réécriture d'un même modèle, sans signal politique fort.",
        "color": "#6b7280",
        "order": 4,
    },
}


def classify(size: int, n_groups: int) -> str:
    """Renvoie le code-type d'un cluster en fonction de sa taille et du
    nombre de groupes parlementaires distincts qui le portent."""
    if size >= OBSTRUCTION_THRESHOLD:
        return "obstruction"
    if n_groups >= 2:
        return "convergence"
    if n_groups <= 1 and size >= AMPLIFICATION_MIN_SIZE:
        return "amplification"
    return "reutilisation"


# CTE SQLite qui matérialise la typologie de chaque cluster.
# Utilisable telle quelle dans une CTE WITH avant n'importe quel SELECT.
# Renvoie : cluster_id, size, n_groups, n_authors, type_key
CLUSTER_TYPES_CTE = f"""
WITH cluster_meta AS (
    SELECT c.cluster_id                                  AS cluster_id,
           COUNT(*)                                      AS size,
           COUNT(DISTINCT a.groupe_uid)                  AS n_groups,
           COUNT(DISTINCT a.auteur_uid)                  AS n_authors
      FROM amendement_clusters c
      JOIN amendements a ON a.uid = c.amendement_uid
     GROUP BY c.cluster_id
),
cluster_types AS (
    SELECT cluster_id, size, n_groups, n_authors,
           CASE
               WHEN size >= {OBSTRUCTION_THRESHOLD}              THEN 'obstruction'
               WHEN n_groups >= 2                                THEN 'convergence'
               WHEN n_groups <= 1 AND size >= {AMPLIFICATION_MIN_SIZE} THEN 'amplification'
               ELSE 'reutilisation'
           END AS type_key
      FROM cluster_meta
)
"""


def stats_overview(conn: sqlite3.Connection) -> dict:
    """Renvoie l'agrégat global : compteurs par type, totaux."""
    rows = conn.execute(
        f"""
        {CLUSTER_TYPES_CTE}
        SELECT type_key, COUNT(*) AS n_clusters, SUM(size) AS n_amdts
          FROM cluster_types
         GROUP BY type_key
        """
    ).fetchall()
    by_type = {r["type_key"]: dict(r) for r in rows}
    total_clusters = sum(b["n_clusters"] for b in by_type.values())
    total_amdts_clusterises = sum(b["n_amdts"] for b in by_type.values())
    total_amdts_global = conn.execute(
        "SELECT COUNT(*) AS n FROM amendements"
    ).fetchone()["n"]

    # Ordre stable pour l'UI
    ordered = []
    for key in ("obstruction", "convergence", "amplification", "reutilisation"):
        spec = CLUSTER_TYPES[key]
        row = by_type.get(key, {"n_clusters": 0, "n_amdts": 0})
        ordered.append({
            **spec,
            "n_clusters": row.get("n_clusters", 0) or 0,
            "n_amdts": row.get("n_amdts", 0) or 0,
        })

    return {
        "total_clusters": total_clusters,
        "total_amdts_clusterises": total_amdts_clusterises,
        "total_amdts_global": total_amdts_global,
        "pct_clusterises": (
            100.0 * total_amdts_clusterises / total_amdts_global
            if total_amdts_global else 0.0
        ),
        "by_type": ordered,
    }


def top_textes_by_clusters(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Textes avec le plus de clusters (par type). Une ligne par texte."""
    # Étape 1 : pour chaque (texte, cluster) on garde son type. Étape 2 :
    # on agrège PAR TEXTE et on compte les clusters distincts de chaque type.
    rows = conn.execute(
        f"""
        {CLUSTER_TYPES_CTE},
        text_cluster_types AS (
            SELECT DISTINCT a.dossier_uid AS dossier_uid,
                            ct.cluster_id  AS cluster_id,
                            ct.type_key    AS type_key,
                            ct.size        AS size
              FROM cluster_types ct
              JOIN amendement_clusters c ON c.cluster_id = ct.cluster_id
              JOIN amendements a         ON a.uid = c.amendement_uid
             WHERE a.dossier_uid IS NOT NULL
        )
        SELECT tct.dossier_uid AS uid,
               d.titre, d.statut,
               COUNT(*)                                                AS n_clusters,
               SUM(tct.size)                                           AS n_amdts,
               SUM(CASE WHEN tct.type_key='obstruction'   THEN 1 ELSE 0 END) AS n_obstruction,
               SUM(CASE WHEN tct.type_key='convergence'   THEN 1 ELSE 0 END) AS n_convergence,
               SUM(CASE WHEN tct.type_key='amplification' THEN 1 ELSE 0 END) AS n_amplification,
               SUM(CASE WHEN tct.type_key='reutilisation' THEN 1 ELSE 0 END) AS n_reutilisation
          FROM text_cluster_types tct
          JOIN dossiers d ON d.uid = tct.dossier_uid
         GROUP BY tct.dossier_uid
         ORDER BY n_amdts DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def top_deputes_in_clusters(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Députés dont les amendements sont le plus souvent en cluster (signataires)."""
    rows = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               d.photo_url,
               COUNT(*) AS n_amdts_clusterises
          FROM deputies d
          JOIN amendements a ON a.auteur_uid = d.uid
          JOIN amendement_clusters c ON c.amendement_uid = a.uid
         WHERE d.is_active = 1
         GROUP BY d.uid
         ORDER BY n_amdts_clusterises DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def cluster_siblings(conn: sqlite3.Connection, amendement_uid: str) -> dict | None:
    """Renvoie le cluster auquel appartient un amendement + ses 'frères'.

    Retourne None si l'amendement n'est pas en cluster.
    """
    row = conn.execute(
        "SELECT cluster_id FROM amendement_clusters WHERE amendement_uid = ?",
        (amendement_uid,),
    ).fetchone()
    if not row:
        return None
    cid = row["cluster_id"]

    meta = conn.execute(
        f"""
        {CLUSTER_TYPES_CTE}
        SELECT * FROM cluster_types WHERE cluster_id = ?
        """,
        (cid,),
    ).fetchone()
    if not meta:
        return None

    siblings = conn.execute(
        """
        SELECT a.uid, a.numero, a.auteur_nom_complet,
               a.groupe_abrege, o.couleur AS groupe_couleur,
               a.article_designation, a.sort, a.date_depot
          FROM amendement_clusters c
          JOIN amendements a ON a.uid = c.amendement_uid
          LEFT JOIN organes o ON o.uid = a.groupe_uid
         WHERE c.cluster_id = ? AND a.uid <> ?
         ORDER BY a.date_depot ASC, a.numero ASC
         LIMIT 20
        """,
        (cid, amendement_uid),
    ).fetchall()

    type_spec = CLUSTER_TYPES[meta["type_key"]]
    return {
        "cluster_id": cid,
        "size": meta["size"],
        "n_groups": meta["n_groups"],
        "n_authors": meta["n_authors"],
        "type": type_spec,
        "siblings": [dict(s) for s in siblings],
    }


def texte_cluster_summary(conn: sqlite3.Connection, dossier_uid: str) -> dict | None:
    """Renvoie le compte de clusters par type pour un texte, ou None si
    aucun cluster n'est rattaché à ce texte."""
    rows = conn.execute(
        f"""
        {CLUSTER_TYPES_CTE}
        SELECT ct.type_key, COUNT(DISTINCT ct.cluster_id) AS n_clusters,
               SUM(ct.size) AS n_amdts
          FROM cluster_types ct
          JOIN amendement_clusters c ON c.cluster_id = ct.cluster_id
          JOIN amendements a         ON a.uid = c.amendement_uid
         WHERE a.dossier_uid = ?
         GROUP BY ct.type_key
        """,
        (dossier_uid,),
    ).fetchall()
    if not rows:
        return None
    by_type = {r["type_key"]: dict(r) for r in rows}
    breakdown = []
    for key in ("obstruction", "convergence", "amplification", "reutilisation"):
        if key in by_type:
            spec = CLUSTER_TYPES[key]
            breakdown.append({
                **spec,
                "n_clusters": by_type[key]["n_clusters"],
                "n_amdts": by_type[key]["n_amdts"],
            })
    return {
        "total_clusters": sum(b["n_clusters"] for b in breakdown),
        "total_amdts": sum(b["n_amdts"] for b in breakdown),
        "breakdown": breakdown,
    }
