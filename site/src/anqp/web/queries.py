"""SQL queries used by the web layer.

Kept in one place so they can be reviewed, optimised, and tested without
chasing them through route handlers.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..config import settings
from .legislature import current_legislature


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _clean_pageable(page: int, page_size: int) -> tuple[int, int]:
    """Clamp pagination params into [1, ...] / [1, page_size_max]."""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = settings.page_size_default
    page_size = max(1, min(settings.page_size_max, page_size))
    return page, page_size


# ---------------------------------------------------------------------
# Question listing — single function, every filter is optional.
# ---------------------------------------------------------------------
ALLOWED_SORTS = {
    "date_q_desc": "q.date_question DESC NULLS LAST, q.numero DESC",
    "date_q_asc": "q.date_question ASC NULLS LAST, q.numero ASC",
    "date_r_desc": "q.date_reponse DESC NULLS LAST, q.numero DESC",
    "delai_desc": "q.delai_reponse_jours DESC NULLS LAST, q.numero DESC",
    "delai_asc": "q.delai_reponse_jours ASC NULLS LAST, q.numero DESC",
    "auteur_asc": "q.auteur_nom_complet ASC NULLS LAST",
}


_FTS_SAFE = re.compile(r'[^\w\sÀ-ÿ\-\*"():]+', re.UNICODE)


def _sanitize_fts(s: str) -> str:
    """Sanitise user input for FTS5 MATCH. Supports :

      - phrases entre guillemets : ``"texte exact"``
      - opérateurs explicites AND / OR (sensibles à la casse)
      - recherche par champ : ``titre:retraite``, ``rubrique:santé``
      - négation : ``-mot`` exclut le mot
      - sinon, AND implicite avec préfixe ``terme*``

    Tout caractère non listé dans `_FTS_SAFE` est remplacé par un espace.
    """
    s = _FTS_SAFE.sub(" ", s).strip()
    if not s:
        return ""

    out_tokens: list[str] = []
    in_phrase = False
    phrase_buf: list[str] = []

    for raw_token in s.split():
        if raw_token.startswith('"') and raw_token.endswith('"') and len(raw_token) > 2:
            out_tokens.append(raw_token)
            continue
        if raw_token.startswith('"'):
            in_phrase = True
            phrase_buf = [raw_token[1:]]
            continue
        if in_phrase:
            if raw_token.endswith('"'):
                phrase_buf.append(raw_token[:-1])
                out_tokens.append('"' + " ".join(phrase_buf) + '"')
                in_phrase = False
                phrase_buf = []
            else:
                phrase_buf.append(raw_token)
            continue

        if raw_token in ("AND", "OR", "NOT", "NEAR"):
            out_tokens.append(raw_token)
            continue

        # field:value (FTS5 syntax: column:value)
        if ":" in raw_token and not raw_token.startswith(":"):
            field, value = raw_token.split(":", 1)
            if field and value and len(value) >= 2:
                out_tokens.append(f'{field}:"{value}"*')
                continue

        # negation
        if raw_token.startswith("-") and len(raw_token) > 2:
            out_tokens.append(f'NOT "{raw_token[1:]}"*')
            continue

        if len(raw_token) >= 2:
            out_tokens.append(f'"{raw_token}"*')

    return " ".join(out_tokens)


def search_questions(
    conn: sqlite3.Connection,
    *,
    q_text: str | None = None,
    qtype: str | None = None,
    statut: str | None = None,
    auteur_uid: str | None = None,
    groupe_uid: str | None = None,
    rubrique: str | None = None,
    ministere: str | None = None,
    departement_code: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
    sort: str = "date_q_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Combinable filters on questions. Returns rows + total + pagination meta."""
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size

    where: list[str] = []
    params: list[Any] = []

    if qtype:
        where.append("q.type = ?")
        params.append(qtype)
    if statut:
        where.append("q.statut = ?")
        params.append(statut)
    if auteur_uid:
        where.append("q.auteur_uid = ?")
        params.append(auteur_uid)
    if groupe_uid:
        where.append("q.auteur_groupe_uid = ?")
        params.append(groupe_uid)
    if rubrique:
        where.append("q.rubrique = ?")
        params.append(rubrique)
    if ministere:
        where.append("q.ministere_interroge_court = ?")
        params.append(ministere)
    if departement_code:
        where.append(
            "q.auteur_uid IN (SELECT uid FROM deputies WHERE departement_code = ?)"
        )
        params.append(departement_code)
    if date_min:
        where.append("q.date_question >= ?")
        params.append(date_min)
    if date_max:
        where.append("q.date_question <= ?")
        params.append(date_max)

    fts_join = ""
    if q_text:
        sanitised = _sanitize_fts(q_text)
        if sanitised:
            fts_join = "JOIN questions_fts fts ON fts.uid = q.uid AND fts.questions_fts MATCH ?"
            params.insert(0, sanitised)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sort_sql = ALLOWED_SORTS.get(sort, ALLOWED_SORTS["date_q_desc"])

    count_sql = f"""
        SELECT COUNT(*) AS c
          FROM questions q
          {fts_join}
          {where_sql}
    """
    total = conn.execute(count_sql, params).fetchone()["c"]

    list_sql = f"""
        SELECT q.uid, q.type, q.numero, q.titre, q.auteur_uid,
               q.auteur_nom_complet, q.auteur_groupe_uid, q.auteur_groupe_abrege,
               q.ministere_interroge_court, q.ministere_interroge,
               q.rubrique, q.statut, q.date_question, q.date_reponse,
               q.delai_reponse_jours, q.source_url
          FROM questions q
          {fts_join}
          {where_sql}
         ORDER BY {sort_sql}
         LIMIT ? OFFSET ?
    """
    rows = conn.execute(list_sql, params + [page_size, offset]).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": rows,
    }


# ---------------------------------------------------------------------
# Single-question detail (with auteur join)
# ---------------------------------------------------------------------
def get_question(conn: sqlite3.Connection, uid: str) -> dict | None:
    sql = """
        SELECT q.*,
               d.uid AS d_uid, d.nom_complet AS d_nom, d.groupe_libelle AS d_groupe,
               d.groupe_couleur AS d_couleur, d.departement, d.circonscription,
               d.photo_url
          FROM questions q
          LEFT JOIN deputies d ON d.uid = q.auteur_uid
         WHERE q.uid = ?
    """
    return conn.execute(sql, (uid,)).fetchone()


def get_qag_seance(conn: sqlite3.Connection, q: dict) -> dict | None:
    """For a QAG question, locate the matching séance (by date) and pull the
    full QAG section's interventions in order. Returns None if not a QAG or
    no matching séance is found.
    """
    if not q or q.get("type") != "QG":
        return None
    date_rep = q.get("date_reponse")
    if not date_rep:
        return None
    seance = conn.execute(
        """
        SELECT s.uid, s.date_seance, s.compte_rendu_uid, s.quantieme,
               s.captation_video, s.session_ref, s.organe_uid
          FROM seances s
         WHERE s.date_seance = ?
           AND s.type_xsi = 'seance_type'
           AND EXISTS (
               SELECT 1 FROM seance_interventions i
                WHERE i.seance_uid = s.uid
                  AND i.sommaire1_titre LIKE 'Questions au Gouvernement%'
           )
         ORDER BY s.num_seance_jour DESC
         LIMIT 1
        """,
        (date_rep[:10],),
    ).fetchone()
    if not seance:
        return None
    interventions = conn.execute(
        """
        SELECT ordre, sommaire2_titre, speakers_json, syceron_id
          FROM seance_interventions
         WHERE seance_uid = ?
           AND sommaire1_titre LIKE 'Questions au Gouvernement%'
         ORDER BY ordre
        """,
        (seance["uid"],),
    ).fetchall()
    return {
        "seance": seance,
        "interventions": interventions,
    }


# ---------------------------------------------------------------------
# Deputy listing
# ---------------------------------------------------------------------
def list_deputies(
    conn: sqlite3.Connection,
    *,
    q_text: str | None = None,
    groupe_uid: str | None = None,
    departement_code: str | None = None,
    sort: str = "nom_asc",
    is_active: int | None = 1,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    page, page_size = _clean_pageable(page, page_size)
    offset = (page - 1) * page_size

    where: list[str] = []
    params: list[Any] = []
    if is_active is not None:
        where.append("d.is_active = ?")
        params.append(is_active)
    if q_text:
        like = f"%{q_text}%"
        where.append("(d.nom_complet LIKE ? OR d.nom LIKE ? OR d.prenom LIKE ?)")
        params.extend([like, like, like])
    if groupe_uid:
        where.append("d.groupe_uid = ?")
        params.append(groupe_uid)
    if departement_code:
        where.append("d.departement_code = ?")
        params.append(departement_code)

    sort_sql = {
        "nom_asc": "d.nom ASC, d.prenom ASC",
        "nom_desc": "d.nom DESC, d.prenom DESC",
        "groupe": "d.groupe_abrege ASC, d.nom ASC",
        "departement": "d.departement_code ASC, d.circonscription ASC",
        "questions_desc": "qcount DESC, d.nom ASC",
    }.get(sort, "d.nom ASC")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT count(*) AS c FROM deputies d {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT d.uid, d.nom_complet, d.prenom, d.nom, d.civilite,
               d.groupe_uid, d.groupe_libelle, d.groupe_abrege, d.groupe_couleur,
               d.departement, d.departement_code, d.circonscription,
               d.is_active, d.photo_url,
               (SELECT COUNT(*) FROM questions q WHERE q.auteur_uid = d.uid) AS qcount
          FROM deputies d
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


# ---------------------------------------------------------------------
# Single deputy detail + activity aggregates
# ---------------------------------------------------------------------
def get_deputy(conn: sqlite3.Connection, uid: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM deputies WHERE uid = ?", (uid,)
    ).fetchone()


def get_deputy_activity(conn: sqlite3.Connection, uid: str) -> dict[str, Any]:
    base = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS answered,
               SUM(CASE WHEN statut='sans_reponse' THEN 1 ELSE 0 END) AS unanswered,
               SUM(CASE WHEN statut='cloturee' THEN 1 ELSE 0 END) AS closed,
               AVG(delai_reponse_jours) AS avg_delay
          FROM questions WHERE auteur_uid = ?
        """,
        (uid,),
    ).fetchone()

    by_type = conn.execute(
        "SELECT type, COUNT(*) AS c FROM questions WHERE auteur_uid = ? GROUP BY type",
        (uid,),
    ).fetchall()

    top_themes = conn.execute(
        """
        SELECT rubrique, COUNT(*) AS c
          FROM questions WHERE auteur_uid = ? AND rubrique IS NOT NULL
         GROUP BY rubrique ORDER BY c DESC LIMIT 10
        """,
        (uid,),
    ).fetchall()

    top_min = conn.execute(
        """
        SELECT ministere_interroge_court AS m, COUNT(*) AS c
          FROM questions WHERE auteur_uid = ? AND ministere_interroge_court IS NOT NULL
         GROUP BY m ORDER BY c DESC LIMIT 10
        """,
        (uid,),
    ).fetchall()

    return {
        "totals": base, "by_type": by_type, "top_themes": top_themes, "top_min": top_min,
    }


def deputy_monthly_activity(conn: sqlite3.Connection, uid: str) -> dict[str, Any]:
    """Activité mensuelle d'un député : questions + amendements + votes par mois.

    Renvoie {months: [...], questions: [...], amendements: [...], votes: [...]}
    où chaque liste a la même longueur que months. Les mois sans activité
    sont remplis à zéro pour produire une courbe propre.
    """
    rows_q = conn.execute(
        """
        SELECT substr(date_publication_question, 1, 7) AS m, COUNT(*) AS c
          FROM questions
         WHERE auteur_uid = ?
           AND date_publication_question IS NOT NULL
         GROUP BY m ORDER BY m
        """,
        (uid,),
    ).fetchall()
    rows_a = conn.execute(
        """
        SELECT substr(date_depot, 1, 7) AS m, COUNT(*) AS c
          FROM amendements
         WHERE auteur_uid = ? AND date_depot IS NOT NULL
         GROUP BY m ORDER BY m
        """,
        (uid,),
    ).fetchall()
    rows_v = conn.execute(
        """
        SELECT substr(s.date_scrutin, 1, 7) AS m, COUNT(*) AS c
          FROM votes v JOIN scrutins s ON s.uid = v.scrutin_uid
         WHERE v.acteur_uid = ?
           AND s.date_scrutin IS NOT NULL
           AND v.position IN ('pour', 'contre', 'abstention')
         GROUP BY m ORDER BY m
        """,
        (uid,),
    ).fetchall()

    qmap = {r["m"]: r["c"] for r in rows_q}
    amap = {r["m"]: r["c"] for r in rows_a}
    vmap = {r["m"]: r["c"] for r in rows_v}
    all_months = sorted(set(qmap) | set(amap) | set(vmap))

    return {
        "months": all_months,
        "questions": [qmap.get(m, 0) for m in all_months],
        "amendements": [amap.get(m, 0) for m in all_months],
        "votes": [vmap.get(m, 0) for m in all_months],
    }


def deputy_compare_metrics(conn: sqlite3.Connection, uid: str) -> dict:
    """Single bag of metrics for the side-by-side comparator."""
    base = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.civilite, d.groupe_abrege, d.groupe_libelle,
               d.groupe_couleur, d.departement, d.departement_code, d.circonscription,
               d.profession, d.date_naissance, d.photo_url, d.is_active
          FROM deputies d WHERE d.uid = ?
        """,
        (uid,),
    ).fetchone()
    if not base:
        return {"deputy": None}
    questions = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS answered,
               ROUND(AVG(delai_reponse_jours), 1) AS avg_delay
          FROM questions WHERE auteur_uid = ?
        """,
        (uid,),
    ).fetchone()
    amdt = conn.execute(
        """
        SELECT total, adoptes, rejetes, retires, commission, seance
          FROM deputy_amd_cache WHERE acteur_uid = ?
        """,
        (uid,),
    ).fetchone() or {}
    discipline = conn.execute(
        """
        SELECT expressed, aligned, nb_pour, nb_contre, nb_abstention
          FROM deputy_discipline_cache WHERE acteur_uid = ?
        """,
        (uid,),
    ).fetchone() or {}
    return {
        "deputy": base,
        "questions": questions,
        "amdt": amdt,
        "discipline": discipline,
    }


def get_deputy_mandates(conn: sqlite3.Connection, uid: str) -> list[dict]:
    return conn.execute(
        """
        SELECT m.*, o.libelle AS organe_libelle, o.libelle_abrege AS organe_abrege,
               o.code_type AS organe_code_type
          FROM mandates m
          LEFT JOIN organes o ON o.uid = m.organe_uid
         WHERE m.acteur_uid = ?
         ORDER BY m.date_debut DESC
        """,
        (uid,),
    ).fetchall()


# ---------------------------------------------------------------------
# Aggregates for stats pages
# ---------------------------------------------------------------------
def stats_overall(conn: sqlite3.Connection) -> dict:
    return {
        "questions": conn.execute(
            "SELECT type, COUNT(*) AS c, "
            "SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS answered, "
            "ROUND(AVG(delai_reponse_jours), 1) AS avg_delay "
            "FROM questions GROUP BY type ORDER BY c DESC"
        ).fetchall(),
        "deputies_active": conn.execute(
            "SELECT COUNT(*) AS c FROM deputies WHERE is_active = 1"
        ).fetchone()["c"],
        "groups": conn.execute(
            """
            SELECT d.groupe_abrege, d.groupe_libelle, d.groupe_couleur,
                   COUNT(*) AS deputies
              FROM deputies d
             WHERE d.is_active = 1 AND d.groupe_abrege IS NOT NULL
             GROUP BY d.groupe_uid
             ORDER BY deputies DESC
            """
        ).fetchall(),
    }


def stats_by_group(conn: sqlite3.Connection) -> list[dict]:
    """For each active GP: deputy count, question count, avg delay, response rate."""
    # Two pre-aggregated CTEs joined to the GP organes — much faster than
    # 4 correlated subqueries.
    return conn.execute(
        """
        WITH
          q AS (
            SELECT auteur_groupe_uid AS uid,
                   COUNT(*) AS questions_total,
                   SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS questions_answered,
                   ROUND(AVG(delai_reponse_jours), 1) AS avg_delay
              FROM questions
             WHERE auteur_groupe_uid IS NOT NULL
             GROUP BY auteur_groupe_uid
          ),
          d AS (
            SELECT groupe_uid AS uid, COUNT(*) AS deputies
              FROM deputies
             WHERE is_active=1 AND groupe_uid IS NOT NULL
             GROUP BY groupe_uid
          )
        SELECT g.uid, g.libelle, g.libelle_abrege, g.couleur,
               COALESCE(d.deputies, 0)             AS deputies,
               COALESCE(q.questions_total, 0)      AS questions_total,
               COALESCE(q.questions_answered, 0)   AS questions_answered,
               q.avg_delay                          AS avg_delay
          FROM organes g
          LEFT JOIN q ON q.uid = g.uid
          LEFT JOIN d ON d.uid = g.uid
         WHERE g.code_type='GP' AND g.legislature=?
         ORDER BY questions_total DESC
        """,
        (current_legislature(),),
    ).fetchall()


def stats_by_ministry(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return conn.execute(
        """
        SELECT ministere_interroge_court AS ministere, COUNT(*) AS questions_total,
               SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS questions_answered,
               ROUND(AVG(delai_reponse_jours), 1) AS avg_delay
          FROM questions
         WHERE ministere_interroge_court IS NOT NULL
         GROUP BY ministere
         ORDER BY questions_total DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def stats_by_rubrique(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    return conn.execute(
        """
        SELECT rubrique, COUNT(*) AS questions_total,
               SUM(CASE WHEN statut='avec_reponse' THEN 1 ELSE 0 END) AS questions_answered
          FROM questions WHERE rubrique IS NOT NULL
         GROUP BY rubrique
         ORDER BY questions_total DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def stats_top_deputies(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.groupe_abrege, d.groupe_couleur,
               d.departement, d.photo_url,
               COUNT(q.uid) AS questions_total
          FROM deputies d
          JOIN questions q ON q.auteur_uid = d.uid
         GROUP BY d.uid
         ORDER BY questions_total DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def stats_timeseries(conn: sqlite3.Connection) -> list[dict]:
    """Monthly question volume over the legislature."""
    return conn.execute(
        """
        SELECT substr(date_question, 1, 7) AS month,
               type,
               COUNT(*) AS c
          FROM questions
         WHERE date_question IS NOT NULL
         GROUP BY month, type
         ORDER BY month ASC
        """
    ).fetchall()


# ---------------------------------------------------------------------
# Filter facets / dropdown values
# ---------------------------------------------------------------------
def list_groups(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT uid, libelle, libelle_abrege, couleur
          FROM organes
         WHERE code_type='GP' AND legislature=?
         ORDER BY libelle_abrege
        """,
        (current_legislature(),),
    ).fetchall()


def list_ministries(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT ministere_interroge_court AS m, COUNT(*) AS c
          FROM questions WHERE ministere_interroge_court IS NOT NULL
         GROUP BY m ORDER BY c DESC
        """
    ).fetchall()


def list_rubriques(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT rubrique, COUNT(*) AS c
          FROM questions WHERE rubrique IS NOT NULL
         GROUP BY rubrique ORDER BY rubrique
        """
    ).fetchall()


def list_departements(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT departement_code AS code, departement AS name, COUNT(*) AS deputies
          FROM deputies WHERE is_active=1 AND departement IS NOT NULL
         GROUP BY departement_code
         ORDER BY departement_code
        """
    ).fetchall()


# ---------------------------------------------------------------------
# DB overview — counters used on the home and /a-propos pages.
# ---------------------------------------------------------------------
def db_overview(conn: sqlite3.Connection) -> dict:
    counts = {}
    for t in ("organes", "deputies", "mandates", "questions", "ingestion_runs"):
        counts[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
    last_q = conn.execute(
        "SELECT MAX(date_question) AS d FROM questions"
    ).fetchone()["d"]
    last_r = conn.execute(
        "SELECT MAX(date_reponse) AS d FROM questions"
    ).fetchone()["d"]
    return {
        "counts": counts,
        "max_question_date": last_q,
        "max_reponse_date": last_r,
    }
