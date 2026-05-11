"""Parse Amendements.json.zip → 100k+ rows in `amendements` + FTS.

Two structural quirks documented in phase2-plan.md:
  * Files are encoded in latin-1 / cp1252 despite advertising application/json.
    We decode bytes manually before json.loads.
  * Path encodes the examen stage : `BTC####` = commission, otherwise = séance.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

from ..config import settings
from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


_ARTICLE_NUM_RE = re.compile(r"(\d+)")


def _article_numero(designation: str | None) -> int | None:
    """Best-effort numeric extraction for sortable display.

    'Article 12' → 12. 'Annexe' → None. 'Article additionnel après l'art. 5' → 5.
    """
    if not designation:
        return None
    m = _ARTICLE_NUM_RE.search(designation)
    return int(m.group(1)) if m else None


_NORMALIZED_SORT = (
    # Order matters : longest/most specific first.
    ("non soutenu",  "Non soutenu"),
    ("irrecevable",  "Irrecevable"),
    ("adopté",       "Adopté"),
    ("adopte",       "Adopté"),
    ("rejeté",       "Rejeté"),
    ("rejete",       "Rejeté"),
    ("retiré",       "Retiré"),
    ("retire",       "Retiré"),
    ("tombé",        "Tombé"),
    ("tombe",        "Tombé"),
    ("en traitement", "En traitement"),
    ("discuté",      "Discuté"),
    ("discute",      "Discuté"),
    ("effacé",       "Effacé"),
    ("efface",       "Effacé"),
)


def _normalize_sort(value: str | None) -> str | None:
    """Map AN's varied sort labels onto a small canonical set."""
    if not value:
        return None
    key = value.strip().lower()
    for token, label in _NORMALIZED_SORT:
        if token in key:
            return label
    return value.strip().capitalize()


def _resolve_sort(cycle: dict) -> tuple[str | None, str | None]:
    """Return (canonical_sort, raw_sort_for_debug).

    Strategy :
      1. `cycleDeVie.sort` as a clean string → use it directly.
      2. Otherwise `etatDesTraitements.etat.libelle` (e.g. "Retiré", "Irrecevable 40")
         then `sousEtat.libelle`.
    """
    sort_top = text_of(cycle.get("sort"))
    if sort_top:
        return _normalize_sort(sort_top), sort_top

    etat_libelle = text_of(get(cycle, "etatDesTraitements", "etat", "libelle"))
    sous_libelle = text_of(get(cycle, "etatDesTraitements", "sousEtat", "libelle"))
    raw = etat_libelle or sous_libelle
    return _normalize_sort(raw), raw


def _examen_type_from_path(name: str, identification: dict) -> str:
    """`BTC` segment in the path = commission, otherwise séance."""
    # Most reliable : the path contains `B####` (séance) or `BTC####` (commission).
    if "/BTC" in name or name.split("/")[-1].startswith("AMC") or "BTC" in name:
        return "commission"
    # Fallback on `examenRef` field when present.
    examen_ref = text_of(identification.get("examenRef"))
    if examen_ref and "BTC" in examen_ref:
        return "commission"
    return "seance"


def parse_amendement(name: str, raw: dict, *, only_legislature: int | None = None) -> dict | None:
    a = raw.get("amendement") or {}
    uid = text_of(a.get("uid"))
    if not uid:
        return None

    identification = a.get("identification") or {}
    legislature = to_int(a.get("legislature") or identification.get("legislature"))
    if only_legislature is not None and legislature != only_legislature:
        return None
    numero = to_int(identification.get("numeroLong") or identification.get("numero"))

    examen_type = _examen_type_from_path(name, identification)

    # Cycle de vie / sort
    cycle = a.get("cycleDeVie") or {}
    sort_norm, sort_brut = _resolve_sort(cycle)

    date_depot = text_of(cycle.get("dateDepot")) or text_of(get(cycle, "dateDepot"))
    date_publication = text_of(cycle.get("datePublication"))
    date_sort = text_of(cycle.get("dateSort")) or text_of(get(cycle, "dateMiseEnLigne"))

    # Auteurs / signataires
    sig = a.get("signataires") or {}
    auteur = sig.get("auteur") or {}
    auteur_uid = text_of(auteur.get("acteurRef"))
    groupe_uid = text_of(auteur.get("groupePolitiqueRef"))
    cosigs = as_list(get(sig, "cosignataires", "cosignataire"))
    cosig_count = len([c for c in cosigs if isinstance(c, dict) and text_of(c.get("acteurRef"))])

    # Pointeur sur le texte
    pointeur = a.get("pointeurFragmentTexte") or {}
    division = pointeur.get("division") or {}
    article_designation = text_of(division.get("articleDesignation"))
    avant_apres = text_of(division.get("avant_A_Apres"))
    if avant_apres:
        avant_apres = avant_apres.lower()
    article_addition = avant_apres if avant_apres in ("avant", "apres", "après") else None
    if article_addition == "après":
        article_addition = "apres"
    alinea = text_of(get(pointeur, "amendementStandard", "alinea"))
    article_numero = _article_numero(article_designation)

    # Texte
    corps = a.get("corps") or {}
    contenu = corps.get("contenuAuteur") or {}
    texte = text_of(contenu.get("dispositif"))
    expose = text_of(contenu.get("exposeSommaire"))

    # Liens
    representations = as_list(get(a, "representations", "representation"))
    pdf_url = None
    for rep in representations:
        if not isinstance(rep, dict):
            continue
        uri = text_of(get(rep, "contenu", "documentURI"))
        if uri and uri.endswith(".pdf"):
            pdf_url = uri
            break

    document_uid = text_of(a.get("texteLegislatifRef"))
    seance_discussion_ref = text_of(a.get("seanceDiscussionRef"))
    parent_uid = text_of(a.get("amendementParentRef"))
    discussion_commune = text_of(a.get("discussionCommune"))
    discussion_identique = text_of(a.get("discussionIdentique"))
    article_99 = 1 if text_of(a.get("article99")) == "true" else 0

    # Source URL — derive from the dossier path in the zip.
    # `json/{DLR5L17N#####}/{texteRef}/AM…` → dossier_uid is the first segment after json/.
    parts = name.split("/")
    dossier_uid = parts[1] if len(parts) >= 3 else None
    if not (dossier_uid and dossier_uid.startswith("DLR")):
        dossier_uid = None

    # Public URL : `https://www.assemblee-nationale.fr/dyn/{leg}/amendements/{texteNum}{stage}/AN/{numero}`
    leg = legislature or 17
    source_url = ""
    if numero and document_uid:
        # Strip prefix ('PRJLANR5L17B', 'PIONANR5L17B'…) → text number
        m = re.search(r"B(\d+)$", document_uid)
        if m:
            stage = "C" if examen_type == "commission" else ""
            source_url = (
                f"https://www.assemblee-nationale.fr/dyn/{leg}"
                f"/amendements/{m.group(1)}{stage}/AN/{numero}"
            )

    return {
        "uid": uid,
        "legislature": legislature,
        "numero": numero,
        "examen_type": examen_type,
        "dossier_uid": dossier_uid,
        "document_uid": document_uid,
        "auteur_uid": auteur_uid,
        "auteur_nom_complet": None,           # post-pass
        "groupe_uid": groupe_uid,
        "groupe_abrege": None,                 # post-pass
        "cosignataires_count": cosig_count,
        "article_designation": article_designation,
        "article_numero": article_numero,
        "article_addition": article_addition,
        "alinea": alinea,
        "sort": sort_norm,
        "sort_brut": sort_brut,
        "date_depot": date_depot[:10] if date_depot else None,
        "date_publication": date_publication[:10] if date_publication else None,
        "date_sort": date_sort[:10] if date_sort else None,
        "seance_discussion_ref": seance_discussion_ref,
        "article_99": article_99,
        "parent_uid": parent_uid,
        "discussion_commune": discussion_commune,
        "discussion_identique": discussion_identique,
        "texte": texte,
        "expose_sommaire": expose,
        "pdf_url": pdf_url,
        "source_url": source_url,
    }


# ---------------------------------------------------------------------
# Bulk loader
# ---------------------------------------------------------------------
AMD_COLS = (
    "uid", "legislature", "numero", "examen_type", "dossier_uid", "document_uid",
    "auteur_uid", "auteur_nom_complet", "groupe_uid", "groupe_abrege",
    "cosignataires_count", "article_designation", "article_numero",
    "article_addition", "alinea", "sort", "sort_brut",
    "date_depot", "date_publication", "date_sort",
    "seance_discussion_ref", "article_99", "parent_uid",
    "discussion_commune", "discussion_identique",
    "texte", "expose_sommaire", "pdf_url", "source_url",
)


def _placeholders(cols: tuple[str, ...]) -> str:
    return "(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")"


def _decode(blob: bytes) -> str:
    """Try UTF-8 first (most files), fall back to latin-1 (some legacy files)."""
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob.decode("latin-1", errors="replace")


def _iter_amd_zip(zip_path: Path) -> Iterator[tuple[str, dict]]:
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            # Quick path-based pre-filter on the current legislature. Saves
            # ~30% of parsing time on the L17 dump (which contains some L16 entries).
            leg_marker = str(settings.legislature)
            if (f"DLR5L{leg_marker}" not in name
                    and f"AMANR5L{leg_marker}" not in name):
                continue
            with z.open(name) as f:
                blob = f.read()
            try:
                data = json.loads(_decode(blob))
            except json.JSONDecodeError as e:
                log.warning("amendement_json_error", extra={"file": name, "error": str(e)})
                continue
            yield name, data


def ingest_amendements(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    """Streaming ingestion : commit every 5 000 rows. ~3-5 minutes total."""
    log.info("ingest_amendements_start", extra={"zip": str(zip_path)})

    conn.execute("PRAGMA synchronous = OFF")

    seen = inserted = errors = 0
    BATCH = 5_000
    batch_rows: list[tuple] = []
    batch_uids: list[str] = []

    for name, raw in _iter_amd_zip(zip_path):
        try:
            row = parse_amendement(name, raw, only_legislature=settings.legislature)
            if row is None:
                continue
            batch_rows.append(tuple(row[c] for c in AMD_COLS))
            batch_uids.append(row["uid"])
            seen += 1

            if len(batch_rows) >= BATCH:
                inserted += _flush_batch(conn, batch_rows, batch_uids)
                batch_rows.clear(); batch_uids.clear()
                if seen % 25_000 == 0:
                    log.info("ingest_amendements_progress", extra={"seen": seen})
        except Exception as e:
            errors += 1
            log.warning("amendement_parse_error", extra={"file": name, "error": str(e)})

    if batch_rows:
        inserted += _flush_batch(conn, batch_rows, batch_uids)

    # Post-pass : denormalise auteur_nom_complet + groupe_abrege.
    log.info("ingest_amendements_post_pass")
    conn.execute("BEGIN")
    conn.execute(
        """
        UPDATE amendements SET
          auteur_nom_complet = (
              SELECT nom_complet FROM deputies WHERE deputies.uid = amendements.auteur_uid
          ),
          groupe_abrege = (
              SELECT libelle_abrege FROM organes WHERE organes.uid = amendements.groupe_uid
          )
        """
    )
    # Update dossiers cache counts.
    conn.execute(
        """
        UPDATE dossiers SET
          nb_amendements_total = COALESCE((
              SELECT COUNT(*) FROM amendements WHERE dossier_uid = dossiers.uid
          ), 0),
          nb_amendements_adoptes = COALESCE((
              SELECT COUNT(*) FROM amendements
               WHERE dossier_uid = dossiers.uid AND sort = 'Adopté'
          ), 0)
        """
    )
    # Materialise per-deputy and per-group amendement counts so /stats/amendements stays fast.
    conn.execute("DELETE FROM deputy_amd_cache")
    conn.execute(
        """
        INSERT INTO deputy_amd_cache(acteur_uid, total, adoptes, rejetes, retires, commission, seance)
        SELECT auteur_uid,
               COUNT(*) AS total,
               SUM(CASE WHEN sort='Adopté' THEN 1 ELSE 0 END) AS adoptes,
               SUM(CASE WHEN sort='Rejeté' THEN 1 ELSE 0 END) AS rejetes,
               SUM(CASE WHEN sort='Retiré' THEN 1 ELSE 0 END) AS retires,
               SUM(CASE WHEN examen_type='commission' THEN 1 ELSE 0 END) AS commission,
               SUM(CASE WHEN examen_type='seance' THEN 1 ELSE 0 END) AS seance
          FROM amendements
         WHERE legislature = ? AND auteur_uid IS NOT NULL
         GROUP BY auteur_uid
        """,
        (settings.legislature,),
    )
    conn.execute("DELETE FROM groupe_amd_cache")
    conn.execute(
        """
        INSERT INTO groupe_amd_cache(groupe_uid, total, adoptes)
        SELECT groupe_uid,
               COUNT(*) AS total,
               SUM(CASE WHEN sort='Adopté' THEN 1 ELSE 0 END) AS adoptes
          FROM amendements
         WHERE legislature = ? AND groupe_uid IS NOT NULL
         GROUP BY groupe_uid
        """,
        (settings.legislature,),
    )
    conn.execute("COMMIT")

    # Rebuild FTS in one shot for the rows we touched.
    log.info("ingest_amendements_fts")
    conn.execute("BEGIN")
    conn.execute("DELETE FROM amendements_fts")
    conn.execute(
        """
        INSERT INTO amendements_fts(uid, texte, expose_sommaire,
                                    article_designation, auteur_nom_complet)
        SELECT uid, texte, expose_sommaire, article_designation, auteur_nom_complet
          FROM amendements
        """
    )
    conn.execute("COMMIT")

    conn.execute("PRAGMA synchronous = NORMAL")

    n = conn.execute("SELECT COUNT(*) AS c FROM amendements").fetchone()["c"]
    log.info(
        "ingest_amendements_done",
        extra={"seen": seen, "rows": n, "errors": errors},
    )
    return {"seen": seen, "rows": n, "errors": errors, "inserted": inserted}


def _flush_batch(conn, rows: list[tuple], uids: list[str]) -> int:
    conn.execute("BEGIN")
    conn.executemany(
        f"INSERT OR REPLACE INTO amendements {_placeholders(AMD_COLS)}",
        rows,
    )
    conn.execute("COMMIT")
    return len(rows)
