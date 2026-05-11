"""Parse Dossiers_Legislatifs.json.zip → dossiers + documents + actes_legislatifs.

The dossier JSON has a deeply nested `actesLegislatifs.acteLegislatif` tree.
We flatten it depth-first into the `actes_legislatifs` table, recording
parent_uid + ordre so the hierarchy is reconstructible at read time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


# ---------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _date_only(value):
    """Strip time component from an ISO 8601 timestamp."""
    s = text_of(value)
    if not s:
        return None
    return s[:10] if _DATE_RE.match(s) else None


def _walk_actes(node, dossier_uid: str, parent_uid: str | None,
                ordre: list[int], rows: list[dict]) -> None:
    """DFS over the nested actesLegislatifs tree, emitting one row per acte."""
    if node is None:
        return
    for entry in as_list(node):
        if not isinstance(entry, dict):
            continue
        uid = text_of(entry.get("uid"))
        if not uid:
            continue
        ordre[0] += 1
        rows.append({
            "uid": uid,
            "dossier_uid": dossier_uid,
            "parent_uid": parent_uid,
            "ordre": ordre[0],
            "code_acte": text_of(entry.get("codeActe")),
            "libelle": text_of(get(entry, "libelleActe", "nomCanonique")),
            "libelle_court": text_of(get(entry, "libelleActe", "libelleCourt")),
            "date_acte": _date_only(entry.get("dateActe")),
            "organe_uid": text_of(entry.get("organeRef")),
            "document_uid": text_of(entry.get("texteAssocie")),
            "texte_adopte_uid": text_of(entry.get("texteAdopte")),
            "type_xsi": entry.get("@xsi:type"),
            "raw_json": json.dumps(entry, ensure_ascii=False),
        })
        # Recurse
        children = entry.get("actesLegislatifs")
        if isinstance(children, dict):
            _walk_actes(children.get("acteLegislatif"), dossier_uid, uid, ordre, rows)


def _classify_initiateur(raw: dict) -> tuple[str, str | None, list[str]]:
    """(initiateur_type, lisible, [acteur_uids])."""
    init = raw.get("initiateur")
    if not init:
        return "gouvernement", None, []
    acteurs = as_list(get(init, "acteurs", "acteur"))
    uids = []
    for a in acteurs:
        if not isinstance(a, dict):
            continue
        u = text_of(a.get("acteurRef"))
        if u:
            uids.append(u)
    if uids:
        return "parlementaire", None, uids
    return "autre", None, []


def _statut_from_actes(actes: list[dict]) -> str:
    """Deduce a high-level status from the navette.

    Reads `type_xsi` (XML schema type, stable) before `code_acte` (textual
    code, varies by leg/lecture). The codes the AN actually uses :

    | type_xsi                  | meaning                |
    | ------------------------- | ---------------------- |
    | Promulgation_Type         | loi promulguée         |
    | RetraitInitiative_Type    | initiative retirée     |
    | DepotMotionCensure_Type   | motion de censure (informatif) |
    | RenvoiCMP_Type            | renvoi en CMP (informatif) |

    Adoption / rejet ne sont pas explicites dans la nomenclature : ils
    apparaissent comme `Decision_Type` génériques. On laisse `en_cours`
    par défaut quand on n'a pas de signal terminal.
    """
    types = [a.get("type_xsi") or "" for a in actes]
    codes = [a.get("code_acte") or "" for a in actes]

    if "Promulgation_Type" in types:
        return "promulgue"
    if "RetraitInitiative_Type" in types or any("RTRINI" in c for c in codes):
        return "retire"
    if any("CADUC" in c for c in codes):
        return "caduc"
    # Fallback heuristics on legacy code patterns.
    if any("PROMULGATION" in c for c in codes):
        return "promulgue"
    if any("REJET-DEFINITIF" in c for c in codes):
        return "rejete"
    if any("ADOPTION-DEFINITIVE" in c for c in codes):
        return "adopte"
    return "en_cours"


def parse_dossier(raw: dict) -> tuple[dict | None, list[dict]]:
    d = raw.get("dossierParlementaire") or {}
    uid = text_of(d.get("uid"))
    if not uid:
        return None, []

    legislature = to_int(d.get("legislature"))
    titre = text_of(get(d, "titreDossier", "titre"))
    titre_chemin = text_of(get(d, "titreDossier", "titreChemin"))
    proc = d.get("procedureParlementaire") or {}
    proc_code = text_of(proc.get("code"))
    proc_libelle = text_of(proc.get("libelle"))
    init_type, _init_label, init_uids = _classify_initiateur(d)

    # Walk the navette tree.
    acte_rows: list[dict] = []
    ordre = [0]
    _walk_actes(get(d, "actesLegislatifs", "acteLegislatif"), uid, None, ordre, acte_rows)

    # Find the "first deposition" act → document_initial.
    document_initial = None
    date_depot = None
    for a in acte_rows:
        if a.get("code_acte") and a["code_acte"].endswith("-DEPOT"):
            document_initial = document_initial or a.get("document_uid")
            date_depot = date_depot or a.get("date_acte")
    if not date_depot and acte_rows:
        # fall back to the earliest dated act
        dated = sorted([a for a in acte_rows if a.get("date_acte")], key=lambda x: x["date_acte"])
        if dated:
            date_depot = dated[0]["date_acte"]

    date_dernier_acte = None
    if acte_rows:
        dated = sorted(
            [a for a in acte_rows if a.get("date_acte")],
            key=lambda x: x["date_acte"], reverse=True,
        )
        if dated:
            date_dernier_acte = dated[0]["date_acte"]

    statut = _statut_from_actes(acte_rows)

    # Commission saisie au fond — first acte with codeActe ending COM-FOND.
    commission_uid = None
    for a in acte_rows:
        if a.get("code_acte") and a["code_acte"].endswith("COM-FOND"):
            commission_uid = a.get("organe_uid")
            break

    # Source URL — the canonical "dossier" page.
    chemin = titre_chemin or uid.lower()
    source_url = f"https://www.assemblee-nationale.fr/dyn/{legislature or 17}/dossiers/{chemin}"

    dossier = {
        "uid": uid,
        "legislature": legislature,
        "titre": titre,
        "titre_chemin": titre_chemin,
        "procedure_code": proc_code,
        "procedure_libelle": proc_libelle,
        "initiateur_type": init_type,
        "initiateur": None,                 # filled by post-pass from acteurs
        "initiateur_acteur_uids": json.dumps(init_uids, ensure_ascii=False),
        "commission_fond_uid": commission_uid,
        "commission_fond_libelle": None,    # post-pass
        "document_initial_uid": document_initial,
        "rapporteur_uids": "[]",             # post-pass when documents loaded
        "date_depot": date_depot,
        "date_dernier_acte": date_dernier_acte,
        "statut": statut,
        "nb_amendements_total": 0,
        "nb_amendements_adoptes": 0,
        "nb_scrutins": 0,
        "source_url": source_url,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }
    return dossier, acte_rows


def parse_document(raw: dict) -> dict | None:
    d = raw.get("document") or {}
    uid = text_of(d.get("uid"))
    if not uid:
        return None
    classification = d.get("classification") or {}
    type_info = classification.get("type") or {}
    soustype = classification.get("sousType") or {}
    chrono = get(d, "cycleDeVie", "chrono") or {}
    titres = d.get("titres") or {}

    # Auteurs — premier acteurRef
    auteur = get(d, "auteurs", "auteur", "acteur")
    if isinstance(auteur, list):
        auteur = auteur[0] if auteur else None
    auteur_uid = None
    auteur_qualite = None
    if isinstance(auteur, dict):
        auteur_uid = text_of(auteur.get("acteurRef"))
        auteur_qualite = text_of(auteur.get("qualite"))

    # Co-signataires
    cosig_uids: list[str] = []
    for cs in as_list(get(d, "coSignataires", "coSignataire")):
        if not isinstance(cs, dict):
            continue
        u = text_of(get(cs, "acteur", "acteurRef"))
        if u:
            cosig_uids.append(u)

    organe_referent = text_of(get(d, "organesReferents", "organeRef"))
    if not organe_referent:
        organe_referent = text_of(get(d, "organeReferent", "organeRef"))

    return {
        "uid": uid,
        "dossier_uid": text_of(d.get("dossierRef")),
        "legislature": to_int(d.get("legislature")),
        "type_code": text_of(type_info.get("code")),
        "type_libelle": text_of(type_info.get("libelle")),
        "sous_type": text_of(soustype.get("libelle")),
        "titre_principal": text_of(titres.get("titrePrincipal")),
        "titre_court": text_of(titres.get("titrePrincipalCourt")),
        "numero": to_int(get(d, "notice", "numNotice")),
        "date_creation": _date_only(chrono.get("dateCreation")),
        "date_depot": _date_only(chrono.get("dateDepot")),
        "date_publication": _date_only(chrono.get("datePublication")),
        "auteur_principal_uid": auteur_uid,
        "auteur_qualite": auteur_qualite,
        "cosignataires_uids": json.dumps(cosig_uids, ensure_ascii=False),
        "organe_referent_uid": organe_referent,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


# ---------------------------------------------------------------------
# Bulk ingest
# ---------------------------------------------------------------------
DOSSIER_COLS = (
    "uid", "legislature", "titre", "titre_chemin", "procedure_code",
    "procedure_libelle", "initiateur_type", "initiateur",
    "initiateur_acteur_uids", "commission_fond_uid", "commission_fond_libelle",
    "document_initial_uid", "rapporteur_uids", "date_depot", "date_dernier_acte",
    "statut", "nb_amendements_total", "nb_amendements_adoptes", "nb_scrutins",
    "source_url", "raw_json",
)
DOCUMENT_COLS = (
    "uid", "dossier_uid", "legislature", "type_code", "type_libelle", "sous_type",
    "titre_principal", "titre_court", "numero", "date_creation", "date_depot",
    "date_publication", "auteur_principal_uid", "auteur_qualite",
    "cosignataires_uids", "organe_referent_uid", "raw_json",
)
ACTE_COLS = (
    "uid", "dossier_uid", "parent_uid", "ordre", "code_acte", "libelle",
    "libelle_court", "date_acte", "organe_uid", "document_uid",
    "texte_adopte_uid", "type_xsi", "raw_json",
)


def _placeholders(cols: tuple[str, ...]) -> str:
    return "(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")"


def _iter_zip_json(zip_path: Path, prefix: str) -> Iterator[tuple[str, dict]]:
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.startswith(f"json/{prefix}/") or not name.endswith(".json"):
                continue
            with z.open(name) as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    log.warning("dossiers_json_error", extra={"file": name, "error": str(e)})
                    continue
            yield name, data


def ingest_dossiers(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    """Ingest the dossiers ZIP : both `dossierParlementaire` and `document` entries."""
    dossier_rows: list[tuple] = []
    acte_rows: list[tuple] = []
    document_rows: list[tuple] = []
    errors = 0

    log.info("ingest_dossiers_start", extra={"zip": str(zip_path)})

    # 1) Dossiers + actes
    for fname, raw in _iter_zip_json(zip_path, "dossierParlementaire"):
        try:
            d, actes = parse_dossier(raw)
            if d is None:
                continue
            dossier_rows.append(tuple(d[c] for c in DOSSIER_COLS))
            for a in actes:
                acte_rows.append(tuple(a[c] for c in ACTE_COLS))
        except Exception as e:
            errors += 1
            log.warning("dossier_parse_error", extra={"file": fname, "error": str(e)})

    # 2) Documents (textes au sens large)
    for fname, raw in _iter_zip_json(zip_path, "document"):
        try:
            row = parse_document(raw)
            if row is None:
                continue
            document_rows.append(tuple(row[c] for c in DOCUMENT_COLS))
        except Exception as e:
            errors += 1
            log.warning("document_parse_error", extra={"file": fname, "error": str(e)})

    conn.execute("BEGIN")

    if dossier_rows:
        # Wipe-and-replace strategy for actes_legislatifs of the dossiers we re-saw.
        seen_dossier_uids = sorted({r[0] for r in dossier_rows})
        for i in range(0, len(seen_dossier_uids), 500):
            chunk = seen_dossier_uids[i:i + 500]
            qmarks = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM actes_legislatifs WHERE dossier_uid IN ({qmarks})", chunk,
            )
        conn.executemany(
            f"INSERT OR REPLACE INTO dossiers {_placeholders(DOSSIER_COLS)}",
            dossier_rows,
        )
    if acte_rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO actes_legislatifs {_placeholders(ACTE_COLS)}",
            acte_rows,
        )
    if document_rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO documents {_placeholders(DOCUMENT_COLS)}",
            document_rows,
        )

    # 3) Post-pass : derive readable initiateur + commission_fond_libelle + rapporteur_uids
    conn.execute(
        """
        UPDATE dossiers SET
          commission_fond_libelle = (
              SELECT libelle FROM organes WHERE uid = dossiers.commission_fond_uid
          ),
          initiateur = (
              CASE WHEN initiateur_type = 'gouvernement' THEN 'Gouvernement'
                   ELSE (
                       SELECT GROUP_CONCAT(d2.nom_complet, ', ')
                         FROM deputies d2
                        WHERE d2.uid IN (SELECT value FROM json_each(dossiers.initiateur_acteur_uids))
                   )
              END
          )
        """
    )

    # 4) Rapporteur(s) — collect rapport documents linked to each dossier.
    conn.execute(
        """
        UPDATE dossiers SET rapporteur_uids = COALESCE((
            SELECT json_group_array(d.auteur_principal_uid)
              FROM documents d
             WHERE d.dossier_uid = dossiers.uid
               AND d.type_code = 'RAPP'
               AND d.auteur_principal_uid IS NOT NULL
        ), '[]')
        """
    )

    conn.execute("COMMIT")

    log.info(
        "ingest_dossiers_done",
        extra={
            "dossiers": len(dossier_rows), "actes": len(acte_rows),
            "documents": len(document_rows), "errors": errors,
        },
    )
    return {
        "dossiers": len(dossier_rows),
        "actes": len(acte_rows),
        "documents": len(document_rows),
        "errors": errors,
    }
