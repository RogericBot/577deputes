"""Parse Question (QE / QOSD / QG) zip dumps and upsert into SQLite."""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..config import settings
from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


# ---------------------------------------------------------------------
# JO publication URL builder.
# ---------------------------------------------------------------------
def _source_url(uid: str, qtype: str, numero: int | None) -> str:
    if numero is None:
        return ""
    if qtype == "QE":
        return f"https://questions.assemblee-nationale.fr/q17/17-{numero}QE.htm"
    if qtype == "QOSD":
        return f"https://questions.assemblee-nationale.fr/q17/17-{numero}QOSD.htm"
    if qtype == "QG":
        return f"https://questions.assemblee-nationale.fr/q17/17-{numero}QG.htm"
    return ""


# ---------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _delta_days(d1: str | None, d2: str | None) -> int | None:
    if not d1 or not d2:
        return None
    m1 = _DATE_RE.match(d1)
    m2 = _DATE_RE.match(d2)
    if not (m1 and m2):
        return None
    try:
        a = date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
        b = date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
    except ValueError:
        return None
    return (b - a).days


# ---------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------
def parse_question(raw: dict) -> dict | None:
    q = raw.get("question") or {}
    uid = text_of(q.get("uid"))
    if not uid:
        return None
    qtype = q.get("type") or _infer_type_from_uid(uid)
    legislature = to_int(get(q, "identifiant", "legislature"))
    numero = to_int(get(q, "identifiant", "numero"))

    auteur_uid = get(q, "auteur", "identite", "acteurRef")
    if isinstance(auteur_uid, dict):
        auteur_uid = text_of(auteur_uid)
    if not isinstance(auteur_uid, str):
        auteur_uid = None

    grp = q.get("auteur", {}).get("groupe") or {}
    groupe_uid = text_of(grp.get("organeRef")) if isinstance(grp.get("organeRef"), dict) else grp.get("organeRef")
    groupe_abrege = text_of(grp.get("abrege"))

    min_int = q.get("minInt") or {}
    min_int_court = text_of(min_int.get("abrege"))
    min_int_long = text_of(min_int.get("developpe")) or min_int_court

    # minAttribs can be a single dict or a list-of-dicts wrapper {"minAttrib": [...]}
    min_attribs_raw = q.get("minAttribs")
    min_attrib_list = as_list(get(min_attribs_raw, "minAttrib"))
    last_attrib = min_attrib_list[-1] if min_attrib_list else None
    min_attrib_court = min_attrib_long = None
    if isinstance(last_attrib, dict):
        denom = last_attrib.get("denomination") or {}
        min_attrib_court = text_of(denom.get("abrege"))
        min_attrib_long = text_of(denom.get("developpe")) or min_attrib_court

    indexation = q.get("indexationAN") or {}
    rubrique = text_of(indexation.get("rubrique"))
    tete = text_of(indexation.get("teteAnalyse"))
    analyses = indexation.get("analyses") or {}
    analyse_list = as_list(analyses.get("analyse"))
    analyse_text = " — ".join(a for a in analyse_list if isinstance(a, str)) or None
    if not analyse_text and analyse_list:
        # sometimes the entries are dicts with "#text"
        analyse_text = " — ".join(filter(None, (text_of(a) for a in analyse_list))) or None

    # Texte question
    tq_root = q.get("textesQuestion") or {}
    tq_list = as_list(tq_root.get("texteQuestion"))
    tq = tq_list[-1] if tq_list else None
    texte_question = None
    date_question = None
    date_publication_question = None
    if isinstance(tq, dict):
        texte_question = text_of(tq.get("texte"))
        info = tq.get("infoJO") or {}
        date_question = text_of(info.get("dateJO"))
        date_publication_question = date_question

    # Texte réponse
    tr_root = q.get("textesReponse") or {}
    tr_list = as_list(tr_root.get("texteReponse"))
    tr = tr_list[-1] if tr_list else None
    texte_reponse = None
    date_reponse = None
    if isinstance(tr, dict):
        texte_reponse = text_of(tr.get("texte"))
        info = tr.get("infoJO") or {}
        date_reponse = text_of(info.get("dateJO"))

    # Status
    if texte_reponse:
        statut = "avec_reponse"
    elif q.get("etatCloture") in ("clos", "cloturee", True):
        statut = "cloturee"
    else:
        statut = "sans_reponse"

    # Some payloads carry an explicit etat or rubrique-level closure flag — use a few hints.
    etat = q.get("etat") or q.get("etatCloture")
    if etat and isinstance(etat, str) and "clo" in etat.lower():
        if not texte_reponse:
            statut = "cloturee"

    titre = analyse_text or rubrique or (uid if not numero else f"{qtype} n°{numero}")

    return {
        "uid": uid,
        "type": qtype,
        "legislature": legislature,
        "numero": numero,
        "auteur_uid": auteur_uid,
        "auteur_nom_complet": None,                 # filled by post-pass
        "auteur_groupe_uid": groupe_uid,
        "auteur_groupe_abrege": groupe_abrege,
        "ministere_interroge": min_int_long,
        "ministere_interroge_court": min_int_court,
        "ministere_attributaire": min_attrib_long,
        "ministere_attrib_court": min_attrib_court,
        "rubrique": rubrique,
        "tete_analyse": tete,
        "analyse": analyse_text,
        "titre": titre,
        "texte_question": texte_question,
        "texte_reponse": texte_reponse,
        "date_question": date_question,
        "date_reponse": date_reponse,
        "date_publication_question": date_publication_question,
        "statut": statut,
        "delai_reponse_jours": _delta_days(date_question, date_reponse),
        "source_url": _source_url(uid, qtype, numero),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


def _infer_type_from_uid(uid: str) -> str:
    if "QOSD" in uid:
        return "QOSD"
    if "QG" in uid:
        return "QG"
    return "QE"


# ---------------------------------------------------------------------
# Bulk loader
# ---------------------------------------------------------------------
QUESTION_COLS = (
    "uid", "type", "legislature", "numero", "auteur_uid", "auteur_nom_complet",
    "auteur_groupe_uid", "auteur_groupe_abrege", "ministere_interroge",
    "ministere_interroge_court", "ministere_attributaire",
    "ministere_attrib_court", "rubrique", "tete_analyse", "analyse", "titre",
    "texte_question", "texte_reponse", "date_question", "date_reponse",
    "date_publication_question", "statut", "delai_reponse_jours", "source_url",
    "raw_json",
)


def _placeholders(cols: tuple[str, ...]) -> str:
    return "(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")"


def _iter_zip_json(zip_path: Path) -> Iterator[tuple[str, dict]]:
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            with z.open(name) as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    log.warning("question_json_error", extra={"file": name, "error": str(e)})
                    continue
            yield name, data


def ingest_questions(
    conn: sqlite3.Connection,
    zip_path: Path,
    expected_type: str,
) -> dict[str, int]:
    """Ingest all questions from a zip. `expected_type` is informational only."""
    seen = inserted = updated = errors = 0
    rows: list[tuple] = []
    fts_rows: list[tuple] = []
    log.info("ingest_questions_start", extra={"zip": str(zip_path), "type": expected_type})

    for fname, raw in _iter_zip_json(zip_path):
        try:
            r = parse_question(raw)
            if r is None:
                continue
            rows.append(tuple(r[c] for c in QUESTION_COLS))
            seen += 1
        except Exception as e:
            errors += 1
            log.warning(
                "question_parse_error",
                extra={"file": fname, "error": str(e), "type": expected_type},
            )

    if not rows:
        return {"seen": seen, "inserted": 0, "updated": 0, "errors": errors}

    # Detect inserts vs updates + status transitions (sans → avec, etc.)
    uids = [r[0] for r in rows]
    existing_state: dict[str, dict] = {}
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        qmarks = ",".join("?" for _ in chunk)
        cur = conn.execute(
            f"SELECT uid, statut, date_reponse, delai_reponse_jours "
            f"FROM questions WHERE uid IN ({qmarks})", chunk
        )
        existing_state.update({row["uid"]: row for row in cur.fetchall()})

    statut_idx = QUESTION_COLS.index("statut")
    date_rep_idx = QUESTION_COLS.index("date_reponse")
    delai_idx = QUESTION_COLS.index("delai_reponse_jours")
    new_status = {r[0]: r[statut_idx] for r in rows}

    inserted = sum(1 for u in uids if u not in existing_state)
    updated = sum(1 for u in uids if u in existing_state)
    status_changes = sum(
        1 for u in uids
        if u in existing_state and existing_state[u]["statut"] != new_status.get(u)
    )
    answers_published = sum(
        1 for u in uids
        if u in existing_state
        and existing_state[u]["statut"] == "sans_reponse"
        and new_status.get(u) == "avec_reponse"
    )

    # Capture history for rows whose statut or date_reponse changed.
    history_rows: list[tuple] = []
    for r in rows:
        u = r[0]
        old = existing_state.get(u)
        if old is None:
            continue
        new_statut = r[statut_idx]
        new_date_rep = r[date_rep_idx]
        new_delai = r[delai_idx]
        if (old["statut"] != new_statut
                or old["date_reponse"] != new_date_rep):
            history_rows.append((
                u, old["statut"], new_statut,
                old["date_reponse"], new_date_rep,
                old["delai_reponse_jours"], new_delai,
            ))

    conn.execute("BEGIN")
    if history_rows:
        conn.executemany(
            "INSERT INTO questions_history(uid, statut_avant, statut_apres, "
            "date_reponse_avant, date_reponse_apres, delai_avant, delai_apres) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            history_rows,
        )
    conn.executemany(
        f"INSERT OR REPLACE INTO questions {_placeholders(QUESTION_COLS)}",
        rows,
    )
    # Fill auteur_nom_complet from deputies in one shot (only for the rows we
    # just touched — this keeps subsequent listings fast without a JOIN).
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        qmarks = ",".join("?" for _ in chunk)
        conn.execute(
            f"""
            UPDATE questions
               SET auteur_nom_complet = (
                   SELECT nom_complet FROM deputies WHERE deputies.uid = questions.auteur_uid
               )
             WHERE uid IN ({qmarks})
            """,
            chunk,
        )

    # Sync FTS — easiest is to delete and re-insert the touched uids.
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        qmarks = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM questions_fts WHERE uid IN ({qmarks})", chunk
        )
    conn.execute(
        """
        INSERT INTO questions_fts (uid, titre, texte_question, texte_reponse,
                                   rubrique, analyse, ministere_interroge,
                                   auteur_nom_complet)
        SELECT uid, titre, texte_question, texte_reponse, rubrique, analyse,
               ministere_interroge, auteur_nom_complet
          FROM questions
         WHERE uid IN (%s)
        """ % ",".join("?" for _ in uids),
        uids,
    )
    conn.execute("COMMIT")

    log.info(
        "ingest_questions_done",
        extra={
            "type": expected_type, "seen": seen, "inserted": inserted,
            "updated": updated, "errors": errors,
            "status_changes": status_changes, "answers_published": answers_published,
        },
    )
    return {
        "seen": seen, "inserted": inserted, "updated": updated, "errors": errors,
        "status_changes": status_changes, "answers_published": answers_published,
    }
