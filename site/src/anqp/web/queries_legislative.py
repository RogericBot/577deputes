"""SQL queries for the Phase 2 legislative layer (textes, amendements, scrutins).

Kept in its own module so `queries.py` (Phase 1) stays untouched and
small. All read-only ; no mutating SQL here.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..config import settings
from .legislature import current_legislature


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _clean_pageable(page, page_size, default=50, hard_max=None):
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = default
    cap = hard_max or settings.page_size_max
    page_size = max(1, min(cap, page_size))
    return page, page_size


from .queries import _sanitize_fts as _sanitize_fts  # noqa: F401  (shared)


# =====================================================================
# DOSSIERS / TEXTES
# =====================================================================
def list_dossiers(
    conn: sqlite3.Connection,
    *,
    q_text: str | None = None,
    statut: str | None = None,
    initiateur_type: str | None = None,
    only_active: bool = False,
    sort: str = "date_dernier_acte_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size

    where = [f"d.legislature = {int(current_legislature())}"]
    params: list[Any] = []
    if q_text:
        like = f"%{q_text}%"
        where.append("d.titre LIKE ?")
        params.append(like)
    if statut:
        where.append("d.statut = ?")
        params.append(statut)
    if initiateur_type:
        # On filtre sur le TYPE DE TEXTE (procedure_libelle) plutôt que
        # sur d.initiateur_type qui est mal peuplé dans la source
        # (tous les projets de loi y sont marqués "parlementaire").
        if initiateur_type == "gouvernement":
            where.append("LOWER(d.procedure_libelle) LIKE 'projet de loi%'")
        elif initiateur_type == "parlementaire":
            where.append(
                "(LOWER(d.procedure_libelle) LIKE 'proposition%' "
                " OR LOWER(d.procedure_libelle) LIKE 'rapport%' "
                " OR LOWER(d.procedure_libelle) LIKE 'mission%')"
            )
        else:
            # valeur héritée éventuelle : on tente quand même l'ancien champ
            where.append("d.initiateur_type = ?")
            params.append(initiateur_type)
    if only_active:
        where.append("d.statut IN ('en_cours')")

    sort_sql = {
        "date_dernier_acte_desc": "d.date_dernier_acte DESC NULLS LAST, d.date_depot DESC",
        "date_depot_desc": "d.date_depot DESC NULLS LAST",
        "date_depot_asc": "d.date_depot ASC NULLS LAST",
        "amendements_desc": "d.nb_amendements_total DESC",
        "titre_asc": "d.titre ASC",
    }.get(sort, "d.date_dernier_acte DESC NULLS LAST")

    where_sql = " WHERE " + " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM dossiers d {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT d.uid, d.titre, d.statut, d.initiateur, d.initiateur_type,
               d.procedure_libelle, d.date_depot, d.date_dernier_acte,
               d.nb_amendements_total, d.nb_amendements_adoptes, d.nb_scrutins,
               d.commission_fond_libelle, d.source_url
          FROM dossiers d
          {where_sql}
         ORDER BY {sort_sql}
         LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows,
    }


def get_dossier(conn: sqlite3.Connection, uid: str) -> dict | None:
    return conn.execute("SELECT * FROM dossiers WHERE uid = ?", (uid,)).fetchone()


def get_dossier_actes(conn: sqlite3.Connection, uid: str) -> list[dict]:
    return conn.execute(
        """
        SELECT a.uid, a.parent_uid, a.ordre, a.code_acte, a.libelle, a.libelle_court,
               a.date_acte, a.organe_uid, o.libelle AS organe_libelle,
               a.document_uid, doc.titre_court AS document_titre, doc.type_libelle,
               a.texte_adopte_uid
          FROM actes_legislatifs a
          LEFT JOIN organes o ON o.uid = a.organe_uid
          LEFT JOIN documents doc ON doc.uid = a.document_uid
         WHERE a.dossier_uid = ?
         ORDER BY a.ordre ASC
        """,
        (uid,),
    ).fetchall()


def get_dossier_documents(conn: sqlite3.Connection, uid: str) -> list[dict]:
    return conn.execute(
        """
        SELECT d.uid, d.type_code, d.type_libelle, d.titre_principal, d.titre_court,
               d.date_depot, d.numero, d.auteur_principal_uid,
               dep.nom_complet AS auteur_nom
          FROM documents d
          LEFT JOIN deputies dep ON dep.uid = d.auteur_principal_uid
         WHERE d.dossier_uid = ?
         ORDER BY d.date_depot ASC, d.numero ASC
        """,
        (uid,),
    ).fetchall()


def dossier_amendements_summary(conn: sqlite3.Connection, dossier_uid: str) -> dict:
    """All the per-text aggregations the synthesis page needs."""
    by_sort = conn.execute(
        """
        SELECT sort, COUNT(*) AS c
          FROM amendements
         WHERE dossier_uid = ?
         GROUP BY sort
         ORDER BY c DESC
        """,
        (dossier_uid,),
    ).fetchall()

    by_examen = conn.execute(
        """
        SELECT examen_type, COUNT(*) AS c
          FROM amendements
         WHERE dossier_uid = ?
         GROUP BY examen_type
        """,
        (dossier_uid,),
    ).fetchall()

    by_groupe = conn.execute(
        """
        SELECT a.groupe_uid AS uid,
               a.groupe_abrege AS abrege,
               o.libelle AS libelle,
               o.couleur AS couleur,
               COUNT(*) AS c,
               SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements a
          LEFT JOIN organes o ON o.uid = a.groupe_uid
         WHERE a.dossier_uid = ? AND a.groupe_uid IS NOT NULL
         GROUP BY a.groupe_uid
         ORDER BY c DESC
         LIMIT 12
        """,
        (dossier_uid,),
    ).fetchall()

    by_article = conn.execute(
        """
        SELECT article_designation, article_numero, COUNT(*) AS c,
               SUM(CASE WHEN sort='Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements
         WHERE dossier_uid = ? AND article_designation IS NOT NULL
         GROUP BY article_designation
         ORDER BY (article_numero IS NULL) ASC, article_numero ASC, c DESC
        """,
        (dossier_uid,),
    ).fetchall()

    top_authors = conn.execute(
        """
        SELECT auteur_uid, auteur_nom_complet, groupe_abrege, COUNT(*) AS c
          FROM amendements
         WHERE dossier_uid = ? AND auteur_uid IS NOT NULL
         GROUP BY auteur_uid
         ORDER BY c DESC LIMIT 10
        """,
        (dossier_uid,),
    ).fetchall()

    return {
        "by_sort": by_sort,
        "by_examen": by_examen,
        "by_groupe": by_groupe,
        "by_article": by_article,
        "top_authors": top_authors,
    }


def dossier_scrutins(conn: sqlite3.Connection, dossier_uid: str) -> list[dict]:
    """Scrutins explicitly linked to this dossier (best-effort link only)."""
    return conn.execute(
        """
        SELECT uid, numero, date_scrutin, titre, sort_libelle,
               nb_pour, nb_contre, nb_abstentions, nb_non_votants,
               source_url
          FROM scrutins
         WHERE dossier_uid = ?
         ORDER BY date_scrutin DESC
        """,
        (dossier_uid,),
    ).fetchall()


# =====================================================================
# AMENDEMENTS
# =====================================================================
ALLOWED_AMD_SORTS = {
    "date_depot_desc": "a.date_depot DESC NULLS LAST, a.numero DESC",
    "date_depot_asc": "a.date_depot ASC NULLS LAST, a.numero ASC",
    "numero_asc": "a.numero ASC",
    "article_asc": "(a.article_numero IS NULL), a.article_numero ASC, a.numero ASC",
    "auteur_asc": "a.auteur_nom_complet ASC NULLS LAST",
}


def search_amendements(
    conn: sqlite3.Connection,
    *,
    q_text: str | None = None,
    dossier_uid: str | None = None,
    auteur_uid: str | None = None,
    groupe_uid: str | None = None,
    article_designation: str | None = None,
    sort_filter: str | None = None,
    examen_type: str | None = None,
    sort: str = "article_asc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page, page_size = _clean_pageable(page, page_size, default=50, hard_max=settings.page_size_max)
    offset = (page - 1) * page_size

    where: list[str] = []
    params: list[Any] = []
    if dossier_uid:
        where.append("a.dossier_uid = ?"); params.append(dossier_uid)
    if auteur_uid:
        where.append("a.auteur_uid = ?"); params.append(auteur_uid)
    if groupe_uid:
        where.append("a.groupe_uid = ?"); params.append(groupe_uid)
    if article_designation:
        where.append("a.article_designation = ?"); params.append(article_designation)
    if sort_filter:
        where.append("a.sort = ?"); params.append(sort_filter)
    if examen_type:
        where.append("a.examen_type = ?"); params.append(examen_type)

    fts_join = ""
    if q_text:
        sanitised = _sanitize_fts(q_text)
        if sanitised:
            fts_join = "JOIN amendements_fts fts ON fts.uid = a.uid AND fts.amendements_fts MATCH ?"
            params.insert(0, sanitised)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sort_sql = ALLOWED_AMD_SORTS.get(sort, ALLOWED_AMD_SORTS["article_asc"])

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM amendements a {fts_join} {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT a.uid, a.numero, a.examen_type, a.dossier_uid, a.document_uid,
               a.auteur_uid, a.auteur_nom_complet, a.groupe_uid, a.groupe_abrege,
               a.article_designation, a.article_numero, a.sort, a.date_depot,
               a.cosignataires_count, a.parent_uid, a.source_url, a.pdf_url,
               (SELECT couleur FROM organes WHERE uid = a.groupe_uid) AS groupe_couleur
          FROM amendements a
          {fts_join}
          {where_sql}
         ORDER BY {sort_sql}
         LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows,
    }


def get_amendement(conn: sqlite3.Connection, uid: str) -> dict | None:
    return conn.execute(
        """
        SELECT a.*, d.nom_complet AS auteur_nom_complet_full,
               d.photo_url AS auteur_photo, d.departement AS auteur_departement,
               d.circonscription AS auteur_circo,
               o.libelle AS groupe_libelle, o.couleur AS groupe_couleur,
               doss.titre AS dossier_titre, doss.statut AS dossier_statut,
               doss.uid AS dossier_uid_full,
               doc.titre_court AS document_titre,
               parent.numero AS parent_numero
          FROM amendements a
          LEFT JOIN deputies d ON d.uid = a.auteur_uid
          LEFT JOIN organes o ON o.uid = a.groupe_uid
          LEFT JOIN dossiers doss ON doss.uid = a.dossier_uid
          LEFT JOIN documents doc ON doc.uid = a.document_uid
          LEFT JOIN amendements parent ON parent.uid = a.parent_uid
         WHERE a.uid = ?
        """,
        (uid,),
    ).fetchone()


def amendement_cosignataires(conn: sqlite3.Connection, uid: str) -> list[dict]:
    """Phase 2 keeps cosignataires only as a count for perf — to enumerate them
    we would need to either denormalise or read the raw_json. We keep it count-only
    in this iteration and document the limitation."""
    return []


def list_articles_for_dossier(conn: sqlite3.Connection, dossier_uid: str) -> list[dict]:
    return conn.execute(
        """
        SELECT article_designation, article_numero, COUNT(*) AS c,
               SUM(CASE WHEN sort='Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements
         WHERE dossier_uid = ? AND article_designation IS NOT NULL
         GROUP BY article_designation
         ORDER BY (article_numero IS NULL), article_numero ASC, article_designation ASC
        """,
        (dossier_uid,),
    ).fetchall()


# =====================================================================
# SCRUTINS
# =====================================================================
def list_scrutins(
    conn: sqlite3.Connection,
    *,
    q_text: str | None = None,
    sort_filter: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
    sort: str = "date_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size

    where = [f"s.legislature = {int(current_legislature())}"]
    params: list[Any] = []
    if q_text:
        like = f"%{q_text}%"
        where.append("(s.titre LIKE ? OR s.objet LIKE ?)")
        params.extend([like, like])
    if sort_filter:
        where.append("s.sort_code = ?"); params.append(sort_filter)
    if date_min:
        where.append("s.date_scrutin >= ?"); params.append(date_min)
    if date_max:
        where.append("s.date_scrutin <= ?"); params.append(date_max)

    sort_sql = {
        "date_desc": "s.date_scrutin DESC, s.numero DESC",
        "date_asc": "s.date_scrutin ASC, s.numero ASC",
        "numero_desc": "s.numero DESC",
    }.get(sort, "s.date_scrutin DESC")

    where_sql = " WHERE " + " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM scrutins s {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT s.uid, s.numero, s.date_scrutin, s.titre, s.objet,
               s.sort_code, s.sort_libelle, s.nb_pour, s.nb_contre,
               s.nb_abstentions, s.nb_non_votants, s.nombre_votants,
               s.dossier_uid, s.source_url, doss.titre AS dossier_titre
          FROM scrutins s
          LEFT JOIN dossiers doss ON doss.uid = s.dossier_uid
          {where_sql}
         ORDER BY {sort_sql}
         LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows,
    }


def get_scrutin(conn: sqlite3.Connection, uid: str) -> dict | None:
    return conn.execute(
        """
        SELECT s.*, doss.titre AS dossier_titre
          FROM scrutins s
          LEFT JOIN dossiers doss ON doss.uid = s.dossier_uid
         WHERE s.uid = ?
        """,
        (uid,),
    ).fetchone()


def scrutin_ventilation(conn: sqlite3.Connection, scrutin_uid: str) -> list[dict]:
    """Per-group breakdown of one scrutin (used to render the bar chart)."""
    return conn.execute(
        """
        SELECT v.groupe_uid AS uid,
               o.libelle AS libelle,
               o.libelle_abrege AS abrege,
               o.couleur AS couleur,
               SUM(CASE WHEN v.position = 'pour' THEN 1 ELSE 0 END) AS nb_pour,
               SUM(CASE WHEN v.position = 'contre' THEN 1 ELSE 0 END) AS nb_contre,
               SUM(CASE WHEN v.position = 'abstention' THEN 1 ELSE 0 END) AS nb_abstentions,
               SUM(CASE WHEN v.position = 'non_votant' THEN 1 ELSE 0 END) AS nb_non_votants,
               COUNT(*) AS total
          FROM votes v
          LEFT JOIN organes o ON o.uid = v.groupe_uid
         WHERE v.scrutin_uid = ?
         GROUP BY v.groupe_uid
         ORDER BY total DESC
        """,
        (scrutin_uid,),
    ).fetchall()


def scrutin_votants(
    conn: sqlite3.Connection, scrutin_uid: str, *, position: str | None = None,
    groupe_uid: str | None = None,
) -> list[dict]:
    where = ["v.scrutin_uid = ?"]
    params: list[Any] = [scrutin_uid]
    if position:
        where.append("v.position = ?"); params.append(position)
    if groupe_uid:
        where.append("v.groupe_uid = ?"); params.append(groupe_uid)
    return conn.execute(
        f"""
        SELECT v.acteur_uid, v.position, v.par_delegation, v.groupe_uid,
               o.libelle_abrege AS groupe_abrege, o.couleur AS groupe_couleur,
               d.nom_complet, d.departement, d.circonscription, d.photo_url
          FROM votes v
          LEFT JOIN deputies d ON d.uid = v.acteur_uid
          LEFT JOIN organes o ON o.uid = v.groupe_uid
         WHERE {" AND ".join(where)}
         ORDER BY o.libelle_abrege NULLS LAST, d.nom NULLS LAST
        """,
        params,
    ).fetchall()


# =====================================================================
# DEPUTY — phase 2 enrichment
# =====================================================================
def deputy_legislative_activity(conn: sqlite3.Connection, uid: str) -> dict[str, Any]:
    """All Phase 2 numbers for a single deputy : amendements + scrutins."""
    amd_totals = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN sort = 'Adopté' THEN 1 ELSE 0 END) AS adoptes,
               SUM(CASE WHEN sort = 'Rejeté' THEN 1 ELSE 0 END) AS rejetes,
               SUM(CASE WHEN sort = 'Retiré' THEN 1 ELSE 0 END) AS retires,
               SUM(CASE WHEN examen_type = 'commission' THEN 1 ELSE 0 END) AS commission,
               SUM(CASE WHEN examen_type = 'seance' THEN 1 ELSE 0 END) AS seance
          FROM amendements
         WHERE auteur_uid = ?
        """,
        (uid,),
    ).fetchone()

    amd_top_dossiers = conn.execute(
        """
        SELECT a.dossier_uid AS uid, doss.titre, COUNT(*) AS c,
               SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements a
          LEFT JOIN dossiers doss ON doss.uid = a.dossier_uid
         WHERE a.auteur_uid = ?
         GROUP BY a.dossier_uid
         ORDER BY c DESC LIMIT 10
        """,
        (uid,),
    ).fetchall()

    # Scrutins : participation + alignment.
    vote_totals = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN position = 'pour' THEN 1 ELSE 0 END) AS pour,
               SUM(CASE WHEN position = 'contre' THEN 1 ELSE 0 END) AS contre,
               SUM(CASE WHEN position = 'abstention' THEN 1 ELSE 0 END) AS abstention,
               SUM(CASE WHEN position = 'non_votant' THEN 1 ELSE 0 END) AS non_votant
          FROM votes
         WHERE acteur_uid = ?
        """,
        (uid,),
    ).fetchone()

    # Total number of L17 scrutins published (sample size for "absences").
    nb_scrutins_total = conn.execute(
        "SELECT COUNT(*) AS c FROM scrutins WHERE legislature = ?",
        (current_legislature(),)
    ).fetchone()["c"]

    # Group alignment from precomputed cache (fast).
    alignment = conn.execute(
        """
        SELECT COALESCE(expressed, 0) AS expressed,
               COALESCE(aligned, 0)   AS aligned
          FROM deputy_discipline_cache WHERE acteur_uid = ?
        """,
        (uid,),
    ).fetchone() or {"expressed": 0, "aligned": 0}

    return {
        "amd_totals": amd_totals,
        "amd_top_dossiers": amd_top_dossiers,
        "vote_totals": vote_totals,
        "nb_scrutins_total": nb_scrutins_total,
        "alignment": alignment,
    }


# =====================================================================
# STATS — 3 nouvelles pages
# =====================================================================
def stats_textes_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Stats globaux sur les dossiers de la législature courante."""
    leg = (current_legislature(),)
    by_status = conn.execute(
        "SELECT statut, COUNT(*) AS c FROM dossiers WHERE legislature = ? "
        "GROUP BY statut ORDER BY c DESC",
        leg,
    ).fetchall()
    # Type d'initiateur déduit du type de texte (procedure_libelle), car
    # le champ initiateur_type de la source est faux (tous les PJL marqués
    # "parlementaire").
    by_initiateur = conn.execute(
        "SELECT CASE "
        "         WHEN LOWER(procedure_libelle) LIKE 'projet de loi%' "
        "           OR LOWER(procedure_libelle) LIKE 'projet de ratification%' THEN 'gouvernement' "
        "         WHEN LOWER(procedure_libelle) LIKE 'proposition%' "
        "           OR LOWER(procedure_libelle) LIKE 'rapport%' "
        "           OR LOWER(procedure_libelle) LIKE 'mission%' THEN 'parlementaire' "
        "         ELSE 'autre' END AS initiateur_type, "
        "       COUNT(*) AS c, "
        "       SUM(CASE WHEN statut IN ('adopte','promulgue') THEN 1 ELSE 0 END) AS adoptes, "
        "       SUM(CASE WHEN statut = 'rejete' THEN 1 ELSE 0 END) AS rejetes "
        "  FROM dossiers WHERE legislature = ? "
        " GROUP BY initiateur_type ORDER BY c DESC",
        leg,
    ).fetchall()
    by_month = conn.execute(
        "SELECT substr(date_depot, 1, 7) AS month, COUNT(*) AS c "
        "  FROM dossiers WHERE legislature = ? AND date_depot IS NOT NULL "
        " GROUP BY month ORDER BY month",
        leg,
    ).fetchall()
    return {"by_status": by_status, "by_initiateur": by_initiateur, "by_month": by_month}


def stats_amendements_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Top auteurs, top groupes, distribution par sort.

    All numbers come from the precomputed `deputy_amd_cache` /
    `groupe_amd_cache` tables (refreshed at the end of amendements
    ingestion). Rendering stays under 100ms.
    """
    top_auteurs = conn.execute(
        """
        SELECT c.acteur_uid AS uid,
               d.nom_complet AS nom,
               d.groupe_abrege,
               o.couleur AS groupe_couleur,
               c.total, c.adoptes
          FROM deputy_amd_cache c
          LEFT JOIN deputies d ON d.uid = c.acteur_uid
          LEFT JOIN organes o ON o.uid = d.groupe_uid
         ORDER BY c.total DESC
         LIMIT 25
        """
    ).fetchall()

    by_groupe = conn.execute(
        """
        SELECT c.groupe_uid AS uid,
               o.libelle_abrege AS abrege, o.libelle, o.couleur,
               c.total, c.adoptes
          FROM groupe_amd_cache c
          LEFT JOIN organes o ON o.uid = c.groupe_uid
         ORDER BY c.total DESC
        """
    ).fetchall()

    by_sort = conn.execute(
        "SELECT sort, COUNT(*) AS c FROM amendements WHERE legislature = ? "
        "GROUP BY sort ORDER BY c DESC",
        (current_legislature(),),
    ).fetchall()

    return {"top_auteurs": top_auteurs, "by_groupe": by_groupe, "by_sort": by_sort}


def stats_scrutins_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Discipline par groupe + participation par député.

    Discipline = ratio of votes that match the group's majority position on
    that scrutin. Uses the precomputed `scrutin_groupes.position_majoritaire`
    so the calculation stays under 100ms.
    """
    discipline = conn.execute(
        """
        SELECT g.uid AS groupe_uid, g.libelle, g.libelle_abrege AS abrege,
               g.couleur,
               COALESCE(c.expressed, 0) AS expressed,
               COALESCE(c.aligned, 0)   AS aligned
          FROM organes g
          LEFT JOIN groupe_discipline_cache c ON c.groupe_uid = g.uid
         WHERE g.code_type = 'GP' AND g.legislature = ?
           AND c.expressed > 0
         ORDER BY c.expressed DESC
        """,
        (current_legislature(),),
    ).fetchall()

    nb_scrutins_total = conn.execute(
        "SELECT COUNT(*) AS c FROM scrutins WHERE legislature = ?",
        (current_legislature(),)
    ).fetchone()["c"]

    top_participants = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               d.photo_url,
               c.expressed, c.nb_pour AS pour,
               c.nb_contre AS contre, c.nb_abstention AS abstention
          FROM deputy_discipline_cache c
          JOIN deputies d ON d.uid = c.acteur_uid
         WHERE d.is_active = 1
         ORDER BY c.expressed DESC
         LIMIT 25
        """
    ).fetchall()

    return {
        "discipline": discipline,
        "nb_scrutins_total": nb_scrutins_total,
        "top_participants": top_participants,
    }


# =====================================================================
# Home — section Législation
# =====================================================================
# =====================================================================
# TOPS — 8 classements taillés pour le partage social.
# =====================================================================
def tops_overview(conn: sqlite3.Connection, limit: int = 10) -> dict[str, Any]:
    """Ready-to-share top-N rankings (no implied hierarchy between them)."""
    leg = (current_legislature(),)

    # Députés les plus actifs (cumul questions + amendts + votes exprimés).
    # ⚠️ Ce cumul est mécaniquement dominé par la présence en scrutin
    # (les votes exprimés sont >> aux questions/amendements). Les 3 sous-
    # classements ci-dessous (questions seules / amendements seuls /
    # présence) donnent une vision moins biaisée.
    deputes_actifs = conn.execute(
        f"""
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur, d.photo_url,
               COALESCE((SELECT COUNT(*) FROM questions q WHERE q.auteur_uid = d.uid), 0) AS n_q,
               COALESCE((SELECT total FROM deputy_amd_cache c WHERE c.acteur_uid = d.uid), 0) AS n_a,
               COALESCE((SELECT expressed FROM deputy_discipline_cache c WHERE c.acteur_uid = d.uid), 0) AS n_v
          FROM deputies d
         WHERE d.is_active = 1 AND d.legislature = ?
         ORDER BY (n_q + n_a + n_v) DESC
         LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    # 1b. Top députés par QUESTIONS posées (seules).
    deputes_top_questions = conn.execute(
        f"""
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               COUNT(q.uid) AS n_q
          FROM deputies d
          JOIN questions q ON q.auteur_uid = d.uid
         WHERE d.is_active = 1 AND d.legislature = ?
         GROUP BY d.uid
         ORDER BY n_q DESC LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    # 1c. Top députés par AMENDEMENTS déposés (seuls).
    deputes_top_amendements = conn.execute(
        f"""
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               c.total AS n_a
          FROM deputies d
          JOIN deputy_amd_cache c ON c.acteur_uid = d.uid
         WHERE d.is_active = 1 AND c.total > 0
         ORDER BY n_a DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    # 2. 10 députés les plus présents en scrutin public.
    deputes_presents = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur, d.photo_url,
               c.expressed AS n_votes
          FROM deputies d
          JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid
         WHERE d.is_active = 1
         ORDER BY c.expressed DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    # 10 questions sans réponse depuis le plus longtemps.
    questions_orphelines = conn.execute(
        f"""
        SELECT uid, type, numero, titre, auteur_nom_complet, auteur_groupe_abrege,
               ministere_interroge_court, date_question,
               julianday('now') - julianday(date_question) AS jours_attente
          FROM questions
         WHERE legislature = ? AND statut = 'sans_reponse'
           AND date_question IS NOT NULL
         ORDER BY date_question ASC LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    # 5. 10 ministères les plus lents à répondre.
    # NB : on ne filtre PAS sur delai_reponse_jours IS NOT NULL (sinon on
    # ne compte que les questions répondues → taux toujours = 100 %).
    # AVG() ignore les NULL → le délai moyen porte sur les répondues,
    # mais questions_total inclut les questions sans réponse.
    ministeres_lents = conn.execute(
        f"""
        SELECT ministere_interroge_court AS ministere,
               ROUND(AVG(delai_reponse_jours), 1) AS delai_moyen,
               COUNT(*) AS questions_total,
               SUM(CASE WHEN statut = 'avec_reponse' THEN 1 ELSE 0 END) AS questions_repondues,
               ROUND(100.0 * SUM(CASE WHEN statut = 'avec_reponse' THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) AS taux_reponse
          FROM questions
         WHERE legislature = ? AND ministere_interroge_court IS NOT NULL
         GROUP BY ministere_interroge_court
         HAVING questions_total >= 30
            AND SUM(CASE WHEN statut = 'avec_reponse' THEN 1 ELSE 0 END) >= 5
         ORDER BY delai_moyen DESC LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    # 6. 10 textes avec le plus d'amendements + nb de doublons détectés.
    textes_chauds = conn.execute(
        f"""
        SELECT d.uid, d.titre, d.statut, d.date_dernier_acte,
               d.nb_amendements_total, d.nb_amendements_adoptes,
               COALESCE((
                   SELECT COUNT(*)
                     FROM amendement_clusters c
                     JOIN amendements a ON a.uid = c.amendement_uid
                    WHERE a.dossier_uid = d.uid
               ), 0) AS nb_doublons
          FROM dossiers d
         WHERE d.legislature = ? AND d.nb_amendements_total > 0
         ORDER BY d.nb_amendements_total DESC LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    # 7a. Classement absolu : les députés les plus alignés sur leur groupe.
    discipline_top = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur, d.photo_url,
               c.expressed, c.aligned,
               (c.aligned * 100.0 / c.expressed) AS discipline_pct
          FROM deputies d
          JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid
         WHERE d.is_active = 1 AND c.expressed >= 50
           AND (d.groupe_abrege IS NULL OR UPPER(d.groupe_abrege) <> 'NI')
         ORDER BY discipline_pct DESC, c.expressed DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    # 7b. Le député le plus aligné de CHAQUE groupe (vue équilibrée :
    # une ligne par groupe parlementaire, pas dominée par un seul groupe).
    discipline_top_by_group = conn.execute(
        """
        WITH ranked AS (
            SELECT d.uid, d.nom_complet, d.groupe_uid, d.groupe_abrege,
                   d.groupe_couleur, d.groupe_libelle,
                   c.expressed, c.aligned,
                   (c.aligned * 100.0 / c.expressed) AS discipline_pct,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.groupe_uid
                       ORDER BY (c.aligned * 100.0 / c.expressed) DESC,
                                c.expressed DESC
                   ) AS rk
              FROM deputies d
              JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid
             WHERE d.is_active = 1 AND c.expressed >= 50
               AND d.groupe_uid IS NOT NULL
               AND (d.groupe_abrege IS NULL OR UPPER(d.groupe_abrege) <> 'NI')
        )
        SELECT * FROM ranked WHERE rk = 1
         ORDER BY discipline_pct DESC
        """
    ).fetchall()

    # 8. 10 derniers scrutins les plus serrés.
    scrutins_serres = conn.execute(
        f"""
        SELECT uid, numero, date_scrutin, titre, sort_libelle,
               nb_pour, nb_contre, nb_abstentions,
               ABS(nb_pour - nb_contre) AS ecart
          FROM scrutins
         WHERE legislature = ? AND nb_pour > 0 AND nb_contre > 0
         ORDER BY ecart ASC, date_scrutin DESC LIMIT ?
        """,
        (current_legislature(), limit),
    ).fetchall()

    return {
        "deputes_actifs": deputes_actifs,
        "deputes_top_questions": deputes_top_questions,
        "deputes_top_amendements": deputes_top_amendements,
        "deputes_presents": deputes_presents,
        "questions_orphelines": questions_orphelines,
        "ministeres_lents": ministeres_lents,
        "textes_chauds": textes_chauds,
        "discipline_top": discipline_top,
        "discipline_top_by_group": discipline_top_by_group,
        "scrutins_serres": scrutins_serres,
    }


_DISSIDENT_SORTS = {
    "dissidence_desc":  "discipline_pct ASC,  dissidences DESC",
    "discipline_desc":  "discipline_pct DESC, dissidences ASC",
    "votes_desc":       "expressed DESC, discipline_pct ASC",
    "votes_asc":        "expressed ASC,  discipline_pct ASC",
}


def dissidents_list(
    conn: sqlite3.Connection,
    *,
    min_votes: int = 100,
    groupe_uid: str | None = None,
    sort: str = "dissidence_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Députés classés par alignement avec leur groupe (≥ min_votes votes exprimés)."""
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size
    if sort not in _DISSIDENT_SORTS:
        sort = "dissidence_desc"
    order_sql = _DISSIDENT_SORTS[sort]
    # Les députés Non inscrits (NI) n'ont pas de "groupe" au sens
    # parlementaire → la notion de discipline de groupe ne s'applique pas.
    # On les exclut systématiquement de ce classement.
    where = [
        "d.is_active = 1",
        "c.expressed >= ?",
        "(d.groupe_abrege IS NULL OR UPPER(d.groupe_abrege) <> 'NI')",
        "(d.groupe_libelle IS NULL OR LOWER(d.groupe_libelle) NOT LIKE '%non inscrit%')",
    ]
    params: list[Any] = [min_votes]
    if groupe_uid:
        where.append("d.groupe_uid = ?")
        params.append(groupe_uid)
    where_sql = " WHERE " + " AND ".join(where)

    total = conn.execute(
        f"""
        SELECT COUNT(*) AS c
          FROM deputies d
          JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid
          {where_sql}
        """,
        params,
    ).fetchone()["c"]
    rows = conn.execute(
        f"""
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               d.groupe_libelle, d.photo_url, d.departement, d.circonscription,
               c.expressed, c.aligned,
               (c.aligned * 100.0 / c.expressed) AS discipline_pct,
               (c.expressed - c.aligned) AS dissidences
          FROM deputies d
          JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid
          {where_sql}
         ORDER BY {order_sql}
         LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    ).fetchall()
    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows, "min_votes": min_votes, "groupe_uid": groupe_uid,
        "sort": sort,
    }


def unified_search(
    conn: sqlite3.Connection, q_text: str, *, per_kind: int = 8
) -> dict[str, Any]:
    """Cross-table search hitting questions/amendts/scrutins/dossiers/députés."""
    if not q_text or len(q_text.strip()) < 2:
        return {"q": q_text, "questions": [], "amendements": [],
                "scrutins": [], "dossiers": [], "deputies": []}
    fts = _sanitize_fts(q_text)
    like = f"%{q_text.strip()}%"

    questions: list[dict] = []
    if fts:
        questions = conn.execute(
            """
            SELECT q.uid, q.type, q.numero, q.titre, q.auteur_nom_complet,
                   q.auteur_groupe_abrege, q.statut, q.date_question,
                   q.ministere_interroge_court
              FROM questions q
              JOIN questions_fts fts ON fts.uid = q.uid AND fts.questions_fts MATCH ?
             ORDER BY q.date_question DESC NULLS LAST LIMIT ?
            """,
            (fts, per_kind),
        ).fetchall()

    amendements: list[dict] = []
    if fts:
        amendements = conn.execute(
            """
            SELECT a.uid, a.numero, a.examen_type, a.dossier_uid, a.article_designation,
                   a.auteur_nom_complet, a.groupe_abrege, a.sort, a.date_depot
              FROM amendements a
              JOIN amendements_fts fts ON fts.uid = a.uid AND fts.amendements_fts MATCH ?
             ORDER BY a.date_depot DESC NULLS LAST LIMIT ?
            """,
            (fts, per_kind),
        ).fetchall()

    scrutins = conn.execute(
        """
        SELECT uid, numero, date_scrutin, titre, sort_libelle,
               nb_pour, nb_contre, nb_abstentions, dossier_uid
          FROM scrutins
         WHERE titre LIKE ? OR objet LIKE ?
         ORDER BY date_scrutin DESC LIMIT ?
        """,
        (like, like, per_kind),
    ).fetchall()

    dossiers = conn.execute(
        """
        SELECT uid, titre, statut, initiateur, initiateur_type, date_dernier_acte,
               nb_amendements_total
          FROM dossiers
         WHERE titre LIKE ?
         ORDER BY date_dernier_acte DESC NULLS LAST LIMIT ?
        """,
        (like, per_kind),
    ).fetchall()

    deputies = conn.execute(
        """
        SELECT uid, nom_complet, groupe_abrege, groupe_couleur, departement,
               circonscription, photo_url, is_active
          FROM deputies
         WHERE nom_complet LIKE ?
         ORDER BY is_active DESC, nom ASC LIMIT ?
        """,
        (like, per_kind),
    ).fetchall()

    return {
        "q": q_text, "questions": questions, "amendements": amendements,
        "scrutins": scrutins, "dossiers": dossiers, "deputies": deputies,
    }


def list_amendement_clusters_by_dossier(
    conn: sqlite3.Connection, *, page: int = 1, page_size: int = 12,
    type_filter: str | None = None,
) -> dict[str, Any]:
    """Return clusters grouped by dossier (text), with the dominant
    political group of each cluster. The text is the headline ; the
    cluster is a sub-row beneath it. If `type_filter` is provided (one
    of 'obstruction', 'convergence', 'amplification', 'reutilisation'),
    only clusters of that type are kept.
    """
    from .cluster_typology import classify, CLUSTER_TYPES
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size

    # Pour chaque (dossier, cluster), on calcule :
    # - la repartition complete des groupes (qui porte combien)
    # - le groupe dominant (celui avec le plus d'amendements)
    breakdown_rows = conn.execute(
        """
        SELECT a.dossier_uid,
               c.cluster_id,
               a.groupe_uid,
               a.groupe_abrege,
               o.libelle  AS groupe_libelle,
               o.couleur  AS groupe_couleur,
               COUNT(*)   AS n
          FROM amendement_clusters c
          JOIN amendements a ON a.uid = c.amendement_uid
          LEFT JOIN organes o ON o.uid = a.groupe_uid
         GROUP BY a.dossier_uid, c.cluster_id, a.groupe_uid
        """
    ).fetchall()

    # Construire pour chaque cluster : breakdown trie par n DESC + groupe dominant
    breakdown: dict[tuple[str, int], list[dict]] = {}
    for r in breakdown_rows:
        key = (r["dossier_uid"], r["cluster_id"])
        breakdown.setdefault(key, []).append(dict(r))
    for k in breakdown:
        breakdown[k].sort(key=lambda x: x["n"], reverse=True)

    # Le "dominant" reste accessible : c'est le premier de la liste triee
    dom: dict[tuple[str, int], dict] = {}
    for key, members in breakdown.items():
        if members:
            d = dict(members[0])
            d["dominant_count"] = d["n"]
            d["n_groupes_distincts"] = len(members)
            dom[key] = d

    # Aggregate per dossier : how many clusters, total clustered amendments.
    per_dossier = conn.execute(
        """
        SELECT a.dossier_uid AS uid,
               COUNT(DISTINCT c.cluster_id) AS n_clusters,
               COUNT(*) AS n_amdts_clusterises
          FROM amendement_clusters c
          JOIN amendements a ON a.uid = c.amendement_uid
         WHERE a.dossier_uid IS NOT NULL
         GROUP BY a.dossier_uid
         ORDER BY n_amdts_clusterises DESC
        """
    ).fetchall()
    total = len(per_dossier)
    page_dossiers = per_dossier[offset:offset + page_size]

    rows: list[dict] = []
    for d in page_dossiers:
        dossier_uid = d["uid"]
        meta = conn.execute(
            "SELECT uid, titre, statut, date_dernier_acte, nb_amendements_total "
            "FROM dossiers WHERE uid = ?",
            (dossier_uid,),
        ).fetchone()
        # Clusters for this dossier, biggest first.
        clusters = conn.execute(
            """
            SELECT c.cluster_id, COUNT(*) AS size
              FROM amendement_clusters c
              JOIN amendements a ON a.uid = c.amendement_uid
             WHERE a.dossier_uid = ?
             GROUP BY c.cluster_id
             ORDER BY size DESC
             LIMIT 25
            """,
            (dossier_uid,),
        ).fetchall()
        cluster_rows = []
        for cl in clusters:
            domrow = dom.get((dossier_uid, cl["cluster_id"]))
            bdown = breakdown.get((dossier_uid, cl["cluster_id"]), [])
            # Classification typologique : taille + nb groupes distincts
            # (les amendements sans groupe comptent comme "?", on les
            # ignore dans le compte de groupes parlementaires distincts)
            n_groups = sum(1 for b in bdown if b.get("groupe_uid"))
            type_key = classify(cl["size"], n_groups)
            if type_filter and type_key != type_filter:
                continue
            sample = conn.execute(
                """
                SELECT a.uid, a.numero, a.article_designation,
                       a.auteur_nom_complet, a.groupe_abrege, a.sort,
                       substr(a.texte, 1, 220) AS preview
                  FROM amendement_clusters c
                  JOIN amendements a ON a.uid = c.amendement_uid
                 WHERE c.cluster_id = ? AND a.dossier_uid = ?
                 ORDER BY a.date_depot ASC, a.numero ASC
                 LIMIT 6
                """,
                (cl["cluster_id"], dossier_uid),
            ).fetchall()
            cluster_rows.append({
                "cluster_id": cl["cluster_id"],
                "size": cl["size"],
                "dominant": domrow,
                "breakdown": bdown,
                "type": CLUSTER_TYPES[type_key],
                "n_groups_distincts": n_groups,
                "sample": sample,
            })
        # Si filtré et qu'aucun cluster ne reste sur ce texte, on skip
        if type_filter and not cluster_rows:
            continue
        rows.append({
            "dossier": meta,
            "n_clusters": d["n_clusters"],
            "n_amdts_clusterises": d["n_amdts_clusterises"],
            "clusters": cluster_rows,
        })

    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows,
    }


# =====================================================================
# COALITIONS — analyses des blocs et alliances de vote
# =====================================================================

# Mapping groupe officiel -> bloc parlementaire de fait, PAR LÉGISLATURE.
# Les blocs ne sont PAS des groupes parlementaires officiels : ce sont des
# regroupements politiques de fait (alliances électorales, soutiens publics
# au gouvernement, position institutionnelle). Documenté dans /a-propos#coalitions.
# La 16e (2022-2024) et la 17e (depuis 2024) ont des configurations différentes.
_BLOC_DEFS: dict[int, dict] = {
    17: {
        "order": ["Nouveau Front Populaire", "Bloc central", "Charnière",
                  "Rassemblement National", "Non inscrits", "Non classé"],
        "intro": ("selon les alliances électorales 2024 (Nouveau Front Populaire) "
                  "et les soutiens parlementaires publics du gouvernement"),
        "by_abrege": {
            "LFI-NFP": "Nouveau Front Populaire", "LFI": "Nouveau Front Populaire",
            "SOC": "Nouveau Front Populaire", "EcoS": "Nouveau Front Populaire",
            "ECOS": "Nouveau Front Populaire", "EELV": "Nouveau Front Populaire",
            "GDR": "Nouveau Front Populaire", "GDR-NUPES": "Nouveau Front Populaire",
            "EPR": "Bloc central", "RE": "Bloc central", "REN": "Bloc central",
            "DEM": "Bloc central", "Dem": "Bloc central", "MoDem": "Bloc central",
            "HOR": "Bloc central",
            "DR": "Charnière", "LR": "Charnière", "LIOT": "Charnière",
            "RN": "Rassemblement National", "UDR": "Rassemblement National",
            "NI": "Non inscrits",
        },
        "colors": {
            "Nouveau Front Populaire": "#dc2626", "Bloc central": "#4f46e5",
            "Charnière": "#d97706", "Rassemblement National": "#1e3a8a",
            "Non inscrits": "#6b7280", "Non classé": "#94a3b8",
        },
        "subtitles": {
            "Nouveau Front Populaire": "Alliance électorale 2024 — gauche unie",
            "Bloc central": "Soutiens du gouvernement",
            "Charnière": "Centristes-droite indépendants — votent au cas par cas",
            "Rassemblement National": "RN et apparentés",
            "Non inscrits": "Députés sans groupe",
            "Non classé": "Mapping non disponible",
        },
    },
    16: {
        "order": ["NUPES", "Ensemble (majorité présidentielle)", "Les Républicains",
                  "Rassemblement National", "LIOT", "Non inscrits", "Non classé"],
        "intro": ("selon les alliances électorales 2022 (NUPES à gauche, coalition "
                  "Ensemble pour la majorité présidentielle relative) et les groupes "
                  "constitués sur la législature"),
        "by_abrege": {
            "LFI - NUPES": "NUPES", "LFI-NUPES": "NUPES", "LFI": "NUPES",
            "Ecolo - NUPES": "NUPES", "Écolo - NUPES": "NUPES", "Ecolo-NUPES": "NUPES",
            "ECOLO": "NUPES", "EcoS": "NUPES",
            "GDR - NUPES": "NUPES", "GDR-NUPES": "NUPES", "GDR": "NUPES",
            "SOC": "NUPES",
            "RE": "Ensemble (majorité présidentielle)", "REN": "Ensemble (majorité présidentielle)",
            "Renaissance": "Ensemble (majorité présidentielle)",
            "Dem": "Ensemble (majorité présidentielle)", "DEM": "Ensemble (majorité présidentielle)",
            "MoDem": "Ensemble (majorité présidentielle)",
            "HOR": "Ensemble (majorité présidentielle)", "Horizons": "Ensemble (majorité présidentielle)",
            "LR": "Les Républicains", "DR": "Les Républicains",
            "RN": "Rassemblement National",
            "LIOT": "LIOT",
            "NI": "Non inscrits",
        },
        "colors": {
            "NUPES": "#dc2626", "Ensemble (majorité présidentielle)": "#f59e0b",
            "Les Républicains": "#2563eb", "Rassemblement National": "#1e3a8a",
            "LIOT": "#0891b2", "Non inscrits": "#6b7280", "Non classé": "#94a3b8",
        },
        "subtitles": {
            "NUPES": "Alliance électorale 2022 — gauche unie (LFI, Écolo, GDR, SOC)",
            "Ensemble (majorité présidentielle)": "Coalition Ensemble — majorité relative",
            "Les Républicains": "Opposition de droite",
            "Rassemblement National": "Extrême droite",
            "LIOT": "Indépendants, Outre-mer et territoires — votent au cas par cas",
            "Non inscrits": "Députés sans groupe",
            "Non classé": "Mapping non disponible",
        },
    },
}


def _bloc_defs(leg: int | None = None) -> dict:
    """Définition des blocs pour une législature (défaut : la 17e)."""
    n = int(leg if leg is not None else current_legislature())
    return _BLOC_DEFS.get(n) or _BLOC_DEFS[17]


# Compat : ancienne table plate (17e) — encore référencée ailleurs ?
BLOC_BY_ABREGE = _BLOC_DEFS[17]["by_abrege"]


def _bloc_for(abrege: str | None, leg: int | None = None) -> str:
    """Retourne le bloc parlementaire d'un groupe a partir de son abrege."""
    if not abrege:
        return "Non classé"
    return _bloc_defs(leg)["by_abrege"].get(abrege, "Non classé")


def _coalitions_groups(conn: sqlite3.Connection) -> list[dict]:
    """Liste des groupes politiques actifs sur la legislature courante."""
    leg = int(current_legislature())
    rows = conn.execute(
        """
        SELECT o.uid, o.libelle_abrege AS abrege, o.libelle, o.couleur,
               COUNT(DISTINCT d.uid) AS effectif,
               (SELECT COUNT(*) FROM scrutin_groupes sg WHERE sg.groupe_uid = o.uid) AS n_scrutins,
               IFNULL(c.expressed, 0)  AS expressed,
               IFNULL(c.aligned, 0)    AS aligned
          FROM organes o
          LEFT JOIN deputies d ON d.groupe_uid = o.uid AND d.is_active = 1
          LEFT JOIN groupe_discipline_cache c ON c.groupe_uid = o.uid
         WHERE o.code_type = 'GP'
           AND o.legislature = ?
        GROUP BY o.uid
        HAVING n_scrutins > 0
        ORDER BY effectif DESC
        """,
        (leg,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["discipline_pct"] = (
            round(100.0 * d["aligned"] / d["expressed"], 1)
            if d["expressed"] else None
        )
        d["bloc"] = _bloc_for(d.get("abrege"))
        out.append(d)
    return out


def coalitions_overview(conn: sqlite3.Connection) -> dict:
    """Tab A : vue d'ensemble des blocs et groupes politiques."""
    groups = _coalitions_groups(conn)
    n_scrutins_total = conn.execute(
        "SELECT COUNT(*) AS c FROM scrutins"
    ).fetchone()["c"]

    # Grouper par bloc — définition propre à la législature en cours.
    defs = _bloc_defs()
    bloc_order = list(defs["order"])
    bloc_colors = defs["colors"]
    bloc_subtitles = defs["subtitles"]
    by_bloc: dict[str, list] = {b: [] for b in bloc_order}
    for g in groups:
        bloc = g.get("bloc") or "Non classé"
        by_bloc.setdefault(bloc, []).append(g)
    blocs = []
    for name in bloc_order:
        members = by_bloc.get(name, [])
        if not members:
            continue
        total = sum(m.get("effectif", 0) or 0 for m in members)
        # Discipline moyenne ponderee par effectif des votes exprimes
        total_expressed = sum(m.get("expressed", 0) or 0 for m in members)
        total_aligned = sum(m.get("aligned", 0) or 0 for m in members)
        discipline_pct = (
            round(100.0 * total_aligned / total_expressed, 1)
            if total_expressed else None
        )
        # Effectif max parmi les groupes du bloc (pour normaliser les barres)
        max_eff = max((m.get("effectif", 0) or 0) for m in members)
        blocs.append({
            "name": name,
            "members": members,
            "total": total,
            "discipline_pct": discipline_pct,
            "color": bloc_colors.get(name, "#64748b"),
            "subtitle": bloc_subtitles.get(name, ""),
            "max_eff": max_eff,
        })

    return {
        "groups": groups,
        "blocs": blocs,
        "n_groupes": len(groups),
        "n_scrutins_total": n_scrutins_total,
        "bloc_intro": defs["intro"],
        "legislature": int(current_legislature()),
    }


def coalitions_matrix(conn: sqlite3.Connection) -> dict:
    """Tab B : matrice de cohesion N x N entre groupes politiques.

    Pour chaque paire (G1, G2) : on prend les scrutins ou les deux ont
    une position majoritaire ('pour', 'contre', 'abstention') et on
    compte le ratio de positions identiques.
    """
    groups = _coalitions_groups(conn)
    pairs = conn.execute(
        """
        SELECT
            sg1.groupe_uid AS g1,
            sg2.groupe_uid AS g2,
            COUNT(*) AS commun,
            SUM(CASE WHEN sg1.position_majoritaire = sg2.position_majoritaire
                     THEN 1 ELSE 0 END) AS aligned
          FROM scrutin_groupes sg1
          JOIN scrutin_groupes sg2
            ON sg1.scrutin_uid = sg2.scrutin_uid
           AND sg1.groupe_uid < sg2.groupe_uid
         WHERE sg1.position_majoritaire IN ('pour','contre','abstention')
           AND sg2.position_majoritaire IN ('pour','contre','abstention')
         GROUP BY sg1.groupe_uid, sg2.groupe_uid
        """
    ).fetchall()

    matrix = {}
    for p in pairs:
        commun = p["commun"]
        aligned = p["aligned"]
        score = round(100.0 * aligned / commun, 1) if commun else None
        matrix[(p["g1"], p["g2"])] = {"commun": commun, "aligned": aligned, "score": score}
        matrix[(p["g2"], p["g1"])] = {"commun": commun, "aligned": aligned, "score": score}

    for g in groups:
        matrix[(g["uid"], g["uid"])] = {"commun": None, "aligned": None, "score": 100.0}

    return {
        "groups": groups,
        "matrix": matrix,
    }


def coalitions_by_topic(conn: sqlite3.Connection, top_n: int = 10) -> dict:
    """Tab C : alliances par sujet, basees sur les SCRUTINS reels.

    Selection rigoureuse et reproductible des Top N scrutins les plus
    interessants, par croisement de deux criteres mesurables :
      (a) scrutin tres suivi : >= 300 votants exprimes (~50% de l'AN)
          — filtre les scrutins anecdotiques en sous-effectif.
      (b) scrutin clivant : ecart |pour - contre| <= 50 voix
          — selectionne les scrutins ou les blocs ont reellement compte.

    Pour chaque scrutin retenu, on liste la position majoritaire de
    chaque groupe (depuis scrutin_groupes). Si le scrutin a un dossier
    rattache (dossier_uid non null), on affiche le titre du dossier et
    le lien vers la fiche texte.
    """
    groups = _coalitions_groups(conn)

    scrutins = conn.execute(
        """
        SELECT s.uid, s.numero, s.titre, s.date_scrutin, s.sort_code,
               s.nb_pour, s.nb_contre, s.nb_abstentions,
               (s.nb_pour + s.nb_contre + s.nb_abstentions) AS total_exprimes,
               ABS(s.nb_pour - s.nb_contre) AS ecart_abs,
               s.dossier_uid,
               d.titre AS dossier_titre, d.statut AS dossier_statut
          FROM scrutins s
          LEFT JOIN dossiers d ON d.uid = s.dossier_uid
         WHERE (s.nb_pour + s.nb_contre + s.nb_abstentions) >= 300
           AND ABS(s.nb_pour - s.nb_contre) <= 50
         ORDER BY ABS(s.nb_pour - s.nb_contre) ASC,
                  s.date_scrutin DESC
         LIMIT ?
        """,
        (top_n,),
    ).fetchall()

    # Fallback : si tres peu de scrutins clivants, on relache le critere
    # ecart et on prend les plus suivis tout court.
    if len(scrutins) < top_n // 2:
        scrutins = conn.execute(
            """
            SELECT s.uid, s.numero, s.titre, s.date_scrutin, s.sort_code,
                   s.nb_pour, s.nb_contre, s.nb_abstentions,
                   (s.nb_pour + s.nb_contre + s.nb_abstentions) AS total_exprimes,
                   ABS(s.nb_pour - s.nb_contre) AS ecart_abs,
                   s.dossier_uid,
                   d.titre AS dossier_titre, d.statut AS dossier_statut
              FROM scrutins s
              LEFT JOIN dossiers d ON d.uid = s.dossier_uid
             WHERE (s.nb_pour + s.nb_contre + s.nb_abstentions) >= 300
             ORDER BY ABS(s.nb_pour - s.nb_contre) ASC
             LIMIT ?
            """,
            (top_n,),
        ).fetchall()

    bloc_order = [
        "Nouveau Front Populaire",
        "Bloc central",
        "Charnière",
        "Rassemblement National",
        "Non inscrits",
        "Non classé",
    ]

    out = []
    for s in scrutins:
        sg_rows = conn.execute(
            """
            SELECT groupe_uid, position_majoritaire,
                   nb_pour, nb_contre, nb_abstentions, nb_membres
              FROM scrutin_groupes
             WHERE scrutin_uid = ?
            """,
            (s["uid"],),
        ).fetchall()
        by_group = {r["groupe_uid"]: dict(r) for r in sg_rows}

        # Construire les positions de chaque groupe et les ranger par bloc
        positions_by_bloc: dict[str, list] = {b: [] for b in bloc_order}
        for g in groups:
            sg = by_group.get(g["uid"])
            bloc_name = _bloc_for(g.get("abrege"))
            if not sg:
                pos = {
                    "uid": g["uid"], "abrege": g["abrege"],
                    "couleur": g["couleur"], "dominant": None,
                }
            else:
                pos = {
                    "uid": g["uid"], "abrege": g["abrege"],
                    "libelle": g["libelle"], "couleur": g["couleur"],
                    "dominant": sg.get("position_majoritaire"),
                    "pour": sg.get("nb_pour", 0),
                    "contre": sg.get("nb_contre", 0),
                    "abstention": sg.get("nb_abstentions", 0),
                    "total": (sg.get("nb_pour", 0) + sg.get("nb_contre", 0)
                              + sg.get("nb_abstentions", 0)),
                }
            positions_by_bloc.setdefault(bloc_name, []).append(pos)

        # Synthèse par bloc : position dominante du bloc (= la plus
        # frequente parmi ses groupes) pour resume rapide.
        positions_grouped = []
        for bloc_name in bloc_order:
            members = positions_by_bloc.get(bloc_name, [])
            if not members:
                continue
            counts = {"pour": 0, "contre": 0, "abstention": 0, "none": 0}
            for m in members:
                key = m["dominant"] if m["dominant"] in counts else "none"
                counts[key] += 1
            ranked = ("pour", "contre", "abstention")
            bloc_dominant = max(ranked, key=lambda k: counts[k]) if max(counts[k] for k in ranked) > 0 else None
            positions_grouped.append({
                "bloc": bloc_name,
                "members": members,
                "bloc_dominant": bloc_dominant,
            })

        out.append({
            "uid": s["uid"], "numero": s["numero"], "titre": s["titre"],
            "date_scrutin": s["date_scrutin"], "sort_code": s["sort_code"],
            "nb_pour": s["nb_pour"], "nb_contre": s["nb_contre"],
            "nb_abstentions": s["nb_abstentions"],
            "total_exprimes": s["total_exprimes"], "ecart_abs": s["ecart_abs"],
            "dossier_uid": s["dossier_uid"],
            "dossier_titre": s["dossier_titre"],
            "dossier_statut": s["dossier_statut"],
            "positions_grouped": positions_grouped,
        })

    return {
        "groups": groups,
        "scrutins": out,
    }


def _find_anchor(groups: list, *patterns: str) -> str | None:
    """Trouve le UID du premier groupe matchant l'un des patterns."""
    patterns_lower = [p.lower() for p in patterns]
    for g in groups:
        ab = (g.get("abrege") or "").lower()
        lib = (g.get("libelle") or "").lower()
        for p in patterns_lower:
            if p in ab or p in lib:
                return g["uid"]
    return None


# =====================================================================
# ANALYSES — outils de detection (templates ministeriels, absenteisme,
# amendements fantomes)
# =====================================================================
# Cache RAM module-level + persistance disque. Les donnees ne changent
# qu'apres le refresh nocturne (3h UTC) ; on met un TTL de 23h pour
# rafraichir une fois par jour, juste apres le refresh.
import time as _time_inner
import pickle as _pickle_inner
from pathlib import Path as _Path_inner
_ANALYSES_CACHE: dict = {}
_ANALYSES_TTL = 23 * 3600  # 23 heures
_ANALYSES_CACHE_FILE = _Path_inner(settings.data_dir) / "analyses_cache.pkl"


def _load_persistent_cache() -> None:
    """Charge le cache disque au demarrage du module."""
    global _ANALYSES_CACHE
    try:
        if _ANALYSES_CACHE_FILE.exists():
            with open(_ANALYSES_CACHE_FILE, "rb") as f:
                _ANALYSES_CACHE = _pickle_inner.load(f)
    except Exception:
        _ANALYSES_CACHE = {}


def _save_persistent_cache() -> None:
    """Persiste le cache courant sur disque (best-effort, silent fail)."""
    try:
        _ANALYSES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ANALYSES_CACHE_FILE.with_suffix(".pkl.tmp")
        with open(tmp, "wb") as f:
            _pickle_inner.dump(_ANALYSES_CACHE, f)
        tmp.replace(_ANALYSES_CACHE_FILE)
    except Exception:
        pass


_load_persistent_cache()


def _cached_analyses(cache_key: tuple, ttl: int = _ANALYSES_TTL):
    """Retourne la valeur cachee si fraiche, sinon None."""
    entry = _ANALYSES_CACHE.get(cache_key)
    if entry and (_time_inner.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _store_analyses_cache(cache_key: tuple, value):
    _ANALYSES_CACHE[cache_key] = (_time_inner.time(), value)
    _save_persistent_cache()
    return value


def warmup_analyses_cache(conn: sqlite3.Connection) -> None:
    """A appeler au demarrage de l'app (en background thread).
    Pre-calcule les analyses pour eviter la latence de la 1ere visite.
    """
    try:
        analyses_homepage_highlights(conn)
    except Exception:
        pass


def analyses_homepage_highlights_if_cached(conn: sqlite3.Connection) -> dict | None:
    """Version non-bloquante pour la home : retourne None si pas en cache.
    Empeche la home d'attendre 10s pour le 1er hit apres un restart.
    """
    cache_key = ("home_highlights",)
    return _cached_analyses(cache_key)


def analyses_minister_templates(conn: sqlite3.Connection, top_n: int = 15) -> dict:
    """Outil 1 : reponses ministerielles types (langue de bois quantifiee).

    Methode : on prend les premiers 250 caracteres significatifs de
    chaque reponse (apres nettoyage HTML), on hashe, et on compte les
    repetitions. Une "reponse type" = un prefixe qui apparait 3+ fois
    sur des questions DIFFERENTES.

    Cache RAM 1h (calcul lourd : ~17k reponses normalisees en Python).
    """
    cache_key = ("templates", top_n)
    cached = _cached_analyses(cache_key)
    if cached is not None:
        return cached

    import re as _re_inner
    rows = conn.execute(
        """
        SELECT uid, type, numero, titre, ministere_interroge_court,
               auteur_nom_complet, texte_reponse, date_reponse
          FROM questions
         WHERE texte_reponse IS NOT NULL AND length(texte_reponse) > 200
        """
    ).fetchall()

    by_prefix: dict[str, list[dict]] = {}
    for r in rows:
        txt = r["texte_reponse"] or ""
        txt = _re_inner.sub(r"<[^>]+>", " ", txt)
        txt = _re_inner.sub(r"\s+", " ", txt).strip()
        if len(txt) < 100:
            continue
        # Retirer formules de politesse initiales pour eviter faux positifs
        lower = txt.lower()
        if lower.startswith(("monsieur le", "madame la", "monsieur le depute",
                             "madame la deputee", "monsieur, madame", "messieurs")):
            tail = txt.split(",", 1)[-1].strip()
            if len(tail) > 100:
                txt = tail
        prefix = txt[:250].lower()
        by_prefix.setdefault(prefix, []).append(dict(r))

    clusters = []
    for prefix, members in by_prefix.items():
        if len(members) < 3:
            continue
        ministeres = {m["ministere_interroge_court"] for m in members
                      if m["ministere_interroge_court"]}
        auteurs = {m["auteur_nom_complet"] for m in members
                   if m["auteur_nom_complet"]}
        clusters.append({
            "n_reponses": len(members),
            "n_ministeres": len(ministeres),
            "n_auteurs": len(auteurs),
            "ministeres": sorted(ministeres),
            "preview": (members[0]["texte_reponse"] or "")[:400],
            "sample": members[:6],
        })
    clusters.sort(key=lambda c: (c["n_reponses"], c["n_ministeres"]), reverse=True)

    total_responses = len(rows)
    total_template = sum(c["n_reponses"] for c in clusters)
    template_rate = (
        round(100.0 * total_template / total_responses, 1)
        if total_responses else 0
    )

    result = {
        "clusters": clusters[:top_n],
        "n_clusters_total": len(clusters),
        "total_responses": total_responses,
        "total_template": total_template,
        "template_rate": template_rate,
    }
    return _store_analyses_cache(cache_key, result)


def analyses_strategic_absence(conn: sqlite3.Connection,
                                top_n: int = 25,
                                min_clivants: int = 30) -> dict:
    """Outil 2 : absenteisme strategique.

    Compare le taux de presence d'un depute sur scrutins clivants
    (ecart <= 10%) vs consensuels (ecart > 50%). Si presence sur
    consensuels >> presence sur clivants : fuite strategique.

    Cache RAM 1h (jointure lourde sur ~1M votes).
    """
    cache_key = ("absence", top_n, min_clivants)
    cached = _cached_analyses(cache_key)
    if cached is not None:
        return cached

    rows = conn.execute(
        """
        WITH scrutin_types AS (
            SELECT uid,
                   CASE
                     WHEN (nb_pour + nb_contre + nb_abstentions) < 200 THEN NULL
                     WHEN ABS(nb_pour - nb_contre) * 1.0
                          / NULLIF((nb_pour + nb_contre + nb_abstentions), 0) < 0.10
                       THEN 'clivant'
                     WHEN ABS(nb_pour - nb_contre) * 1.0
                          / NULLIF((nb_pour + nb_contre + nb_abstentions), 0) > 0.50
                       THEN 'consensuel'
                     ELSE NULL
                   END AS sc_type
              FROM scrutins
        ),
        votes_classed AS (
            SELECT v.acteur_uid,
                   st.sc_type,
                   CASE WHEN v.position IN ('pour','contre','abstention')
                        THEN 1 ELSE 0 END AS present
              FROM votes v
              JOIN scrutin_types st ON st.uid = v.scrutin_uid
             WHERE st.sc_type IS NOT NULL
        ),
        agg AS (
            SELECT acteur_uid,
                   SUM(CASE WHEN sc_type='clivant'    AND present=1 THEN 1 ELSE 0 END) AS pres_cli,
                   SUM(CASE WHEN sc_type='clivant'                  THEN 1 ELSE 0 END) AS tot_cli,
                   SUM(CASE WHEN sc_type='consensuel' AND present=1 THEN 1 ELSE 0 END) AS pres_con,
                   SUM(CASE WHEN sc_type='consensuel'               THEN 1 ELSE 0 END) AS tot_con
              FROM votes_classed
             GROUP BY acteur_uid
        )
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               d.groupe_libelle, d.departement, d.circonscription,
               a.pres_cli, a.tot_cli, a.pres_con, a.tot_con
          FROM agg a
          JOIN deputies d ON d.uid = a.acteur_uid
         WHERE d.is_active = 1 AND a.tot_cli >= ?
        """,
        (min_clivants,),
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["pct_pres_cli"] = round(100.0 * d["pres_cli"] / d["tot_cli"], 1) if d["tot_cli"] else None
        d["pct_pres_con"] = round(100.0 * d["pres_con"] / d["tot_con"], 1) if d["tot_con"] else None
        d["gap"] = (d["pct_pres_con"] or 0) - (d["pct_pres_cli"] or 0)
        if d["pct_pres_cli"] and d["pct_pres_con"]:
            d["ratio_fuite"] = round(d["pct_pres_con"] / d["pct_pres_cli"], 2)
        else:
            d["ratio_fuite"] = None
        out.append(d)
    out.sort(key=lambda r: (r["gap"] or 0), reverse=True)

    result = {
        "rows": out[:top_n],
        "n_total_eligibles": len(out),
        "min_clivants": min_clivants,
    }
    return _store_analyses_cache(cache_key, result)


def analyses_phantom_amendments(conn: sqlite3.Connection,
                                 top_n: int = 25,
                                 min_amdts: int = 50) -> dict:
    """Outil 3 : amendements fantomes (deposes mais jamais defendus).

    Pour chaque depute :
      defendus = sort in (Adopte, Rejete, Retire, Discute)
      fantomes = sort in (Non soutenu, Sans sort) — depose mais
                  jamais examine en seance ou commission.

    Bas ratio = amendements deposes "pour la com" sans intention de
    les defendre. NUANCE : "Non soutenu" peut aussi venir de contraintes
    de temps en seance, c'est un indicateur, pas une accusation.

    Cache RAM 1h (GROUP BY sur ~108k amendements + N SELECTs deputes).
    """
    cache_key = ("phantom", top_n, min_amdts)
    cached = _cached_analyses(cache_key)
    if cached is not None:
        return cached

    rows = conn.execute(
        """
        SELECT a.auteur_uid AS uid,
               COUNT(*) AS total,
               SUM(CASE WHEN a.sort IN ('Adopté','Rejeté','Retiré','Discuté')
                          THEN 1 ELSE 0 END) AS defendus,
               SUM(CASE WHEN a.sort = 'Non soutenu' THEN 1 ELSE 0 END) AS non_soutenus,
               SUM(CASE WHEN a.sort = 'Irrecevable' THEN 1 ELSE 0 END) AS irrecevables,
               SUM(CASE WHEN a.sort = 'Tombé' THEN 1 ELSE 0 END) AS tombes,
               SUM(CASE WHEN a.sort IS NULL OR a.sort = '' THEN 1 ELSE 0 END) AS sans_sort,
               SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements a
         WHERE a.auteur_uid IS NOT NULL
         GROUP BY a.auteur_uid
        HAVING total >= ?
        """,
        (min_amdts,),
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        deputy = conn.execute(
            "SELECT nom_complet, groupe_abrege, groupe_libelle, groupe_couleur, "
            "       departement, circonscription, is_active "
            "  FROM deputies WHERE uid = ?",
            (d["uid"],),
        ).fetchone()
        if not deputy:
            continue
        d.update(dict(deputy))
        d["fantomes"] = d["non_soutenus"] + d["sans_sort"]
        d["pct_defense"] = round(100.0 * d["defendus"] / d["total"], 1) if d["total"] else None
        d["pct_fantomes"] = round(100.0 * d["fantomes"] / d["total"], 1) if d["total"] else None
        d["pct_adoptes"] = round(100.0 * d["adoptes"] / d["total"], 1) if d["total"] else None
        out.append(d)
    out.sort(key=lambda r: (r["pct_fantomes"] or 0), reverse=True)

    result = {
        "rows": out[:top_n],
        "n_total_eligibles": len(out),
        "min_amdts": min_amdts,
    }
    return _store_analyses_cache(cache_key, result)


def analyses_homepage_highlights(conn: sqlite3.Connection) -> dict:
    """Pour la home : 3 trouvailles synthetiques tirees des analyses.
    Cache 1h (depend lui-meme de 3 fonctions cachees).
    """
    cache_key = ("home_highlights",)
    cached = _cached_analyses(cache_key)
    if cached is not None:
        return cached

    # Utiliser le meme top_n que ce qu'affiche /analyses pour mutualiser
    # le cache entre la home et la page detaillee
    tpl = analyses_minister_templates(conn, top_n=15)
    abs_data = analyses_strategic_absence(conn, top_n=30, min_clivants=30)
    phantom = analyses_phantom_amendments(conn, top_n=30, min_amdts=50)
    result = {
        "templates": {
            "rate": tpl["template_rate"],
            "total_responses": tpl["total_responses"],
            "biggest_cluster": tpl["clusters"][0] if tpl["clusters"] else None,
        },
        "strategic_absence": abs_data["rows"][0] if abs_data["rows"] else None,
        "phantom_amendments": phantom["rows"][0] if phantom["rows"] else None,
    }
    return _store_analyses_cache(cache_key, result)


def coalitions_ternary(conn: sqlite3.Connection,
                       anchor_top_uid: str | None = None,
                       anchor_bl_uid: str | None = None,
                       anchor_br_uid: str | None = None) -> dict:
    """Tab D : diagramme ternaire des groupes par cohesion 3-poles.

    Chaque groupe est positionne dans un triangle equilateral selon
    sa cohesion normalisee avec 3 ancres (un par sommet). Les ancres
    par defaut representent les 3 blocs principaux : LFI (NFP),
    EPR (Bloc central), RN (Rassemblement National).

    Mathematique : pour un groupe G avec cohesions (c_NFP, c_EPR, c_RN)
    avec les 3 ancres, on normalise (a, b, c) = (c_NFP, c_EPR, c_RN)
    / (c_NFP + c_EPR + c_RN). Coordonnees cartesiennes :
      x = a * x_top + b * x_bl + c * x_br
      y = a * y_top + b * y_bl + c * y_br
    Les ancres sont placees aux sommets exacts (1/0/0, 0/1/0, 0/0/1).
    """
    # SVG geometry : triangle equilateral
    V_TOP = (300, 60)
    V_BL = (60, 475)
    V_BR = (540, 475)

    mat = coalitions_matrix(conn)
    groups = mat["groups"]
    matrix = mat["matrix"]
    valid_uids = {g["uid"] for g in groups}

    # Defauts : LFI / EPR / RN
    default_top = (
        _find_anchor(groups, "LFI-NFP", "LFI", "insoumis")
        or (groups[0]["uid"] if groups else None)
    )
    default_bl = (
        _find_anchor(groups, "EPR", "Ensemble pour", "Renaissance")
        or (groups[0]["uid"] if groups else None)
    )
    default_br = (
        _find_anchor(groups, "RN", "Rassemblement", "national")
        or (groups[0]["uid"] if groups else None)
    )

    if anchor_top_uid not in valid_uids:
        anchor_top_uid = default_top
    if anchor_bl_uid not in valid_uids:
        anchor_bl_uid = default_bl
    if anchor_br_uid not in valid_uids:
        anchor_br_uid = default_br

    # Si l'utilisateur choisit des doublons, on retombe sur les defauts
    chosen = {anchor_top_uid, anchor_bl_uid, anchor_br_uid}
    if len(chosen) < 3:
        anchor_top_uid = default_top
        anchor_bl_uid = default_bl
        anchor_br_uid = default_br

    def _label_for(uid):
        return next((g["abrege"] for g in groups if g["uid"] == uid), "?")

    nodes = []
    for g in groups:
        # Ancres elles-memes : positionnees exactement au sommet correspondant
        if g["uid"] == anchor_top_uid:
            a, b, c = 1.0, 0.0, 0.0
        elif g["uid"] == anchor_bl_uid:
            a, b, c = 0.0, 1.0, 0.0
        elif g["uid"] == anchor_br_uid:
            a, b, c = 0.0, 0.0, 1.0
        else:
            c_top = matrix.get((g["uid"], anchor_top_uid), {}).get("score") or 0
            c_bl = matrix.get((g["uid"], anchor_bl_uid), {}).get("score") or 0
            c_br = matrix.get((g["uid"], anchor_br_uid), {}).get("score") or 0
            total = c_top + c_bl + c_br
            if total > 0:
                a, b, c = c_top / total, c_bl / total, c_br / total
            else:
                a, b, c = 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0

        x = a * V_TOP[0] + b * V_BL[0] + c * V_BR[0]
        y = a * V_TOP[1] + b * V_BL[1] + c * V_BR[1]

        nodes.append({
            "uid": g["uid"],
            "abrege": g["abrege"],
            "libelle": g["libelle"],
            "couleur": g["couleur"],
            "effectif": g["effectif"],
            "x": x,
            "y": y,
            "w_top": round(a * 100, 1),
            "w_bl": round(b * 100, 1),
            "w_br": round(c * 100, 1),
        })

    return {
        "nodes": nodes,
        "groups": groups,
        "anchor_top_uid": anchor_top_uid,
        "anchor_bl_uid": anchor_bl_uid,
        "anchor_br_uid": anchor_br_uid,
        "anchor_top_label": _label_for(anchor_top_uid),
        "anchor_bl_label": _label_for(anchor_bl_uid),
        "anchor_br_label": _label_for(anchor_br_uid),
        "v_top": V_TOP,
        "v_bl": V_BL,
        "v_br": V_BR,
    }


def coalitions_network(conn: sqlite3.Connection,
                       anchor_x_uid: str | None = None,
                       anchor_y_uid: str | None = None) -> dict:
    """Tab D legacy (2D scatter) — conserve pour compat. Voir coalitions_ternary."""
    mat = coalitions_matrix(conn)
    groups = mat["groups"]
    matrix = mat["matrix"]

    # Defauts si l'utilisateur n'a rien choisi : LFI vs EPR
    default_x = (
        _find_anchor(groups, "LFI-NFP", "LFI", "insoumis")
        or (groups[-1]["uid"] if groups else None)
    )
    default_y = (
        _find_anchor(groups, "EPR", "Ensemble pour", "Renaissance")
        or (groups[0]["uid"] if groups else None)
    )

    # Validation des UIDs choisis
    valid_uids = {g["uid"] for g in groups}
    if anchor_x_uid not in valid_uids:
        anchor_x_uid = default_x
    if anchor_y_uid not in valid_uids or anchor_y_uid == anchor_x_uid:
        anchor_y_uid = default_y

    # Recuperer les libelles des ancres pour l'UI
    anchor_x_label = next((g["abrege"] for g in groups if g["uid"] == anchor_x_uid), "?")
    anchor_y_label = next((g["abrege"] for g in groups if g["uid"] == anchor_y_uid), "?")

    nodes = []
    for g in groups:
        x_score = matrix.get((g["uid"], anchor_x_uid), {}).get("score")
        y_score = matrix.get((g["uid"], anchor_y_uid), {}).get("score")
        nodes.append({
            "uid": g["uid"],
            "abrege": g["abrege"],
            "libelle": g["libelle"],
            "couleur": g["couleur"],
            "effectif": g["effectif"],
            "x": x_score if x_score is not None else 50.0,
            "y": y_score if y_score is not None else 50.0,
        })

    links = []
    for i, g1 in enumerate(groups):
        for g2 in groups[i + 1:]:
            score = matrix.get((g1["uid"], g2["uid"]), {}).get("score")
            if score is not None and score >= 60:
                links.append({
                    "source": g1["uid"],
                    "target": g2["uid"],
                    "score": score,
                })

    return {
        "nodes": nodes,
        "links": links,
        "groups": groups,
        "anchor_x_uid": anchor_x_uid,
        "anchor_y_uid": anchor_y_uid,
        "anchor_x_label": anchor_x_label,
        "anchor_y_label": anchor_y_label,
    }


def get_amendement_cluster(conn: sqlite3.Connection, uid: str) -> dict | None:
    """Return cluster (and members) the given amendment belongs to, if any."""
    from .cluster_typology import classify, CLUSTER_TYPES
    row = conn.execute(
        "SELECT cluster_id FROM amendement_clusters WHERE amendement_uid = ?",
        (uid,),
    ).fetchone()
    if not row:
        return None
    cid = row["cluster_id"]
    members = conn.execute(
        """
        SELECT a.uid, a.numero, a.dossier_uid, a.article_designation,
               a.auteur_nom_complet, a.auteur_uid, a.groupe_uid, a.groupe_abrege,
               o.couleur AS groupe_couleur, a.sort,
               doss.titre AS dossier_titre
          FROM amendement_clusters c
          JOIN amendements a ON a.uid = c.amendement_uid
          LEFT JOIN organes o ON o.uid = a.groupe_uid
          LEFT JOIN dossiers doss ON doss.uid = a.dossier_uid
         WHERE c.cluster_id = ?
         ORDER BY a.date_depot ASC LIMIT 50
        """,
        (cid,),
    ).fetchall()
    size = len(members)
    n_groups = len({m["groupe_uid"] for m in members if m["groupe_uid"]})
    type_key = classify(size, n_groups)
    return {
        "cluster_id": cid,
        "size": size,
        "n_groups": n_groups,
        "type": CLUSTER_TYPES[type_key],
        "members": members,
    }


def home_discipline_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Quick summary used by the home card "Dissidents & disciplines"."""
    nb = conn.execute(
        "SELECT COUNT(*) AS c FROM deputy_discipline_cache WHERE expressed >= 100"
    ).fetchone()["c"]
    overall = conn.execute(
        "SELECT SUM(expressed) AS exp, SUM(aligned) AS al FROM groupe_discipline_cache"
    ).fetchone()
    avg = None
    if overall and overall["exp"]:
        avg = round((overall["al"] or 0) * 100.0 / overall["exp"], 1)
    return {"deputies_analysed": nb, "avg_discipline": avg}


def home_legislative_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    # Counts come from the meta cache (refreshed after each ingestion).
    # Falls back to live COUNT(*) if the cache is empty (fresh DB).
    cached = {
        r["key"]: int(r["value"])
        for r in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'count_%'"
        ).fetchall()
    }
    counts = {}
    for t in ("dossiers", "documents", "amendements", "scrutins", "votes"):
        key = f"count_{t}"
        if key in cached:
            counts[t] = cached[key]
        else:
            counts[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
    last_amd_date = conn.execute(
        "SELECT MAX(date_depot) AS d FROM amendements"
    ).fetchone()["d"]
    last_scrutin_date = conn.execute(
        "SELECT MAX(date_scrutin) AS d FROM scrutins"
    ).fetchone()["d"]
    last_dossier_date = conn.execute(
        "SELECT MAX(date_dernier_acte) AS d FROM dossiers WHERE legislature = ?",
        (current_legislature(),),
    ).fetchone()["d"]
    latest_dossiers = conn.execute(
        """
        SELECT uid, titre, statut, initiateur, date_dernier_acte, nb_amendements_total,
               procedure_libelle
          FROM dossiers WHERE legislature = ?
         ORDER BY date_dernier_acte DESC NULLS LAST LIMIT 5
        """,
        (current_legislature(),),
    ).fetchall()
    latest_scrutins = conn.execute(
        """
        SELECT uid, numero, date_scrutin, titre, sort_code, nb_pour, nb_contre, dossier_uid
          FROM scrutins
         ORDER BY date_scrutin DESC, numero DESC LIMIT 5
        """
    ).fetchall()
    return {
        "counts": counts,
        "last_amd_date": last_amd_date,
        "last_scrutin_date": last_scrutin_date,
        "last_dossier_date": last_dossier_date,
        "latest_dossiers": latest_dossiers,
        "latest_scrutins": latest_scrutins,
    }

