"""Parse Agenda.json.zip + syseron.xml.zip → seances + seance_interventions.

Two complementary feeds:
  * Agenda — 6.6 MB ZIP of JSON, one file per réunion (séance ou commission)
  * Syseron — 45 MB ZIP of XML, one file per compte-rendu de séance.

We index both : Agenda gives us the réunion metadata (date, type, link to
compte-rendu UID) ; Syseron gives us the ordered list of interventions
(sommaire1/sommaire2 entries) so we can rebuild the session flow.
"""
from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Iterator

from ..config import settings
from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


# XML namespace used by syseron documents.
_NS = {"a": "http://schemas.assemblee-nationale.fr/referentiel"}
_TAG_RE = re.compile(r"\{[^}]+\}")


def _strip_ns(tag: str) -> str:
    """Drop the namespace prefix from an ElementTree tag."""
    return _TAG_RE.sub("", tag)


def _all_text(node) -> str:
    """Concatenate text content of an element and its descendants."""
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(_all_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------
# Agenda parsing (JSON)
# ---------------------------------------------------------------------
def parse_reunion(raw: dict) -> dict | None:
    r = raw.get("reunion") or {}
    uid = text_of(r.get("uid"))
    if not uid:
        return None
    type_xsi = r.get("@xsi:type")
    ts_debut = text_of(r.get("timeStampDebut"))
    ts_fin = text_of(r.get("timeStampFin"))
    date_seance = text_of(get(r, "identifiants", "DateSeance"))
    if not date_seance and ts_debut:
        date_seance = ts_debut[:10]
    return {
        "uid": uid,
        "legislature": settings.legislature,
        "type_xsi": type_xsi,
        "date_seance": (date_seance or "")[:10] or None,
        "num_seance_jour": to_int(get(r, "identifiants", "numSeanceJour"))
                           or to_int(get(r, "identifiants", "numSeanceJO")),
        "num_seance_jo": to_int(get(r, "identifiants", "numSeanceJO")),
        "quantieme": text_of(get(r, "identifiants", "quantieme")),
        "date_debut": ts_debut,
        "date_fin": ts_fin,
        "organe_uid": text_of(r.get("organeReuniRef")),
        "session_ref": text_of(r.get("sessionRef")),
        "compte_rendu_uid": text_of(r.get("compteRenduRef")),
        "captation_video": 1 if text_of(r.get("captationVideo")) == "true" else 0,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


# ---------------------------------------------------------------------
# Syseron parsing (XML)
# ---------------------------------------------------------------------
def parse_compte_rendu(blob: bytes) -> tuple[str | None, str | None, list[dict]]:
    """Return (compte_rendu_uid, seance_uid, interventions_rows)."""
    try:
        tree = ET.fromstring(blob)
    except ET.ParseError as e:
        log.warning("syseron_xml_error", extra={"error": str(e)})
        return None, None, []

    # The root is `compteRendu` ; namespace handling:
    cr_uid = None
    seance_uid = None
    for child in tree:
        tag = _strip_ns(child.tag)
        if tag == "uid":
            cr_uid = (child.text or "").strip() or None
        elif tag == "seanceRef":
            seance_uid = (child.text or "").strip() or None
        elif tag == "metadonnees":
            # Some XMLs put uid + seanceRef under metadonnees instead.
            for m in child:
                mtag = _strip_ns(m.tag)
                if mtag == "uid" and not cr_uid:
                    cr_uid = (m.text or "").strip() or None
                elif mtag == "seanceRef" and not seance_uid:
                    seance_uid = (m.text or "").strip() or None
                elif mtag == "sommaire":
                    # Sommaire can live under metadonnees or top-level.
                    pass

    # Walk to find the sommaire (anywhere in the tree).
    sommaire = None
    for elem in tree.iter():
        if _strip_ns(elem.tag) == "sommaire":
            sommaire = elem
            break
    if sommaire is None or not seance_uid:
        return cr_uid, seance_uid, []

    interventions: list[dict] = []
    ordre = 0
    for s1 in sommaire:
        if _strip_ns(s1.tag) != "sommaire1":
            continue
        # s1 may directly contain a titreStruct/intitule + multiple sommaire2
        s1_titre = None
        for ts in s1:
            if _strip_ns(ts.tag) == "titreStruct":
                for it in ts:
                    if _strip_ns(it.tag) == "intitule":
                        s1_titre = (_all_text(it) or "").strip() or None
                        break
                break
        for s2 in s1:
            if _strip_ns(s2.tag) != "sommaire2":
                continue
            ordre += 1
            s2_titre = None
            speakers: list[dict] = []
            syceron_id = None
            for child in s2:
                ctag = _strip_ns(child.tag)
                if ctag == "titreStruct":
                    for it in child:
                        if _strip_ns(it.tag) == "intitule":
                            s2_titre = (_all_text(it) or "").strip() or None
                            syceron_id = it.attrib.get("id_syceron") or syceron_id
                            break
                elif ctag == "para":
                    txt = (_all_text(child) or "").strip()
                    if txt:
                        speakers.append({
                            "label": txt,
                            "syceron_id": child.attrib.get("id_syceron"),
                        })
            if s2_titre or speakers:
                interventions.append({
                    "seance_uid": seance_uid,
                    "compte_rendu_uid": cr_uid,
                    "ordre": ordre,
                    "sommaire1_titre": s1_titre,
                    "sommaire2_titre": s2_titre,
                    "speakers_json": json.dumps(speakers, ensure_ascii=False),
                    "syceron_id": syceron_id,
                })

    return cr_uid, seance_uid, interventions


# ---------------------------------------------------------------------
# Bulk loaders
# ---------------------------------------------------------------------
SEANCE_COLS = (
    "uid", "legislature", "type_xsi", "date_seance",
    "num_seance_jour", "num_seance_jo", "quantieme",
    "date_debut", "date_fin", "organe_uid", "session_ref",
    "compte_rendu_uid", "captation_video", "raw_json",
)


def _placeholders(cols: tuple[str, ...]) -> str:
    return "(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")"


def ingest_agenda(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    rows: list[tuple] = []
    errors = 0
    log.info("ingest_agenda_start", extra={"zip": str(zip_path)})
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            with z.open(name) as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    errors += 1
                    log.warning("agenda_json_error", extra={"file": name, "error": str(e)})
                    continue
            try:
                row = parse_reunion(data)
                if row:
                    rows.append(tuple(row[c] for c in SEANCE_COLS))
            except Exception as e:
                errors += 1
                log.warning("reunion_parse_error", extra={"file": name, "error": str(e)})

    if rows:
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT OR REPLACE INTO seances {_placeholders(SEANCE_COLS)}",
            rows,
        )
        conn.execute("COMMIT")
    log.info("ingest_agenda_done", extra={"count": len(rows), "errors": errors})
    return {"seances": len(rows), "errors": errors}


def ingest_syseron(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    """Walk every compte-rendu XML, extract sommaire2 → seance_interventions."""
    n_cr = 0
    n_int = 0
    errors = 0
    seen_seances: list[str] = []
    log.info("ingest_syseron_start", extra={"zip": str(zip_path)})

    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".xml"):
                continue
            with z.open(name) as f:
                blob = f.read()
            try:
                cr_uid, seance_uid, interventions = parse_compte_rendu(blob)
            except Exception as e:
                errors += 1
                log.warning("syseron_parse_error", extra={"file": name, "error": str(e)})
                continue
            if not seance_uid:
                continue
            n_cr += 1
            if interventions:
                conn.execute("BEGIN")
                conn.execute(
                    "DELETE FROM seance_interventions WHERE seance_uid = ?", (seance_uid,)
                )
                conn.executemany(
                    "INSERT INTO seance_interventions(seance_uid, compte_rendu_uid, "
                    "ordre, sommaire1_titre, sommaire2_titre, speakers_json, syceron_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            i["seance_uid"], i["compte_rendu_uid"], i["ordre"],
                            i["sommaire1_titre"], i["sommaire2_titre"],
                            i["speakers_json"], i["syceron_id"],
                        )
                        for i in interventions
                    ],
                )
                conn.execute("COMMIT")
                n_int += len(interventions)
                seen_seances.append(seance_uid)

    log.info(
        "ingest_syseron_done",
        extra={"comptes_rendus": n_cr, "interventions": n_int, "errors": errors},
    )
    return {"comptes_rendus": n_cr, "interventions": n_int, "errors": errors}
