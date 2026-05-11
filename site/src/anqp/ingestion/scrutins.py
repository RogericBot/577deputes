"""Parse Scrutins.json.zip → scrutins + votes (3.7M individual positions)."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


def parse_scrutin(raw: dict) -> tuple[dict | None, list[tuple]]:
    s = raw.get("scrutin") or {}
    uid = text_of(s.get("uid"))
    if not uid:
        return None, []

    type_vote = s.get("typeVote") or {}
    sort_ = s.get("sort") or {}
    objet = s.get("objet") or {}
    syn = s.get("syntheseVote") or {}
    decompte = syn.get("decompte") or {}
    demandeur = s.get("demandeur") or {}
    legislature = to_int(s.get("legislature"))
    numero = to_int(s.get("numero"))

    scrutin_row = {
        "uid": uid,
        "legislature": legislature,
        "numero": numero,
        "date_scrutin": text_of(s.get("dateScrutin")),
        "seance_ref": text_of(s.get("seanceRef")),
        "session_ref": text_of(s.get("sessionRef")),
        "organe_uid": text_of(s.get("organeRef")),
        "type_vote_code": text_of(type_vote.get("codeTypeVote")),
        "type_vote_libelle": text_of(type_vote.get("libelleTypeVote")),
        "type_majorite": text_of(type_vote.get("typeMajorite")),
        "sort_code": text_of(sort_.get("code")),
        "sort_libelle": text_of(sort_.get("libelle")),
        "titre": text_of(s.get("titre")),
        "objet": text_of(objet.get("libelle")) or text_of(s.get("titre")),
        "demandeur": text_of(demandeur.get("texte")),
        "mode_publication": text_of(s.get("modePublicationDesVotes")),
        "nombre_votants": to_int(syn.get("nombreVotants")),
        "suffrages_exprimes": to_int(syn.get("suffragesExprimes")),
        "seuil_majorite": to_int(syn.get("nbrSuffragesRequis")),
        "nb_pour": to_int(decompte.get("pour")),
        "nb_contre": to_int(decompte.get("contre")),
        "nb_abstentions": to_int(decompte.get("abstentions")),
        "nb_non_votants": to_int(decompte.get("nonVotants")),
        "dossier_uid": _try_link_dossier(objet, s.get("titre")),
        "source_url": (
            f"https://www.assemblee-nationale.fr/dyn/{legislature or 17}"
            f"/scrutins/detail/{numero}" if numero else ""
        ),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }

    # Walk ventilationVotes → groupes → vote.decompteNominatif → votant[]
    votes_rows: list[tuple] = []
    groupe_rows: list[tuple] = []
    for organe in as_list(get(s, "ventilationVotes", "organe")):
        for grp in as_list(get(organe, "groupes", "groupe")):
            grp_uid = text_of(grp.get("organeRef"))
            if not grp_uid:
                continue
            vote = grp.get("vote") or {}
            position_majoritaire = text_of(vote.get("positionMajoritaire"))
            decompte_voix = vote.get("decompteVoix") or {}
            decompte_nom = vote.get("decompteNominatif") or {}
            nb_membres = to_int(grp.get("nombreMembresGroupe")) or 0

            groupe_rows.append((
                uid, grp_uid, position_majoritaire,
                to_int(decompte_voix.get("pour")) or 0,
                to_int(decompte_voix.get("contre")) or 0,
                to_int(decompte_voix.get("abstentions")) or 0,
                to_int(decompte_voix.get("nonVotants")) or 0,
                nb_membres,
            ))

            for position_key, position_label in (
                ("pours", "pour"),
                ("contres", "contre"),
                ("abstentions", "abstention"),
                ("nonVotants", "non_votant"),
            ):
                bloc = decompte_nom.get(position_key)
                if not bloc:
                    continue
                for votant in as_list(bloc.get("votant") if isinstance(bloc, dict) else None):
                    if not isinstance(votant, dict):
                        continue
                    acteur_uid = text_of(votant.get("acteurRef"))
                    if not acteur_uid:
                        continue
                    par_del = 1 if text_of(votant.get("parDelegation")) == "true" else 0
                    votes_rows.append((uid, acteur_uid, grp_uid, position_label, par_del))

    return scrutin_row, votes_rows, groupe_rows


def _try_link_dossier(objet: dict, titre: str | None) -> str | None:
    """Best-effort dossier_uid extraction. Most scrutins reference a dossierLegislatif."""
    if isinstance(objet, dict):
        ref = objet.get("dossierLegislatif")
        if isinstance(ref, dict):
            uid = text_of(ref.get("uid")) or text_of(ref)
            return uid
        if isinstance(ref, str) and ref.startswith("DLR"):
            return ref
    return None


# ---------------------------------------------------------------------
# Bulk loader
# ---------------------------------------------------------------------
SCRUTIN_COLS = (
    "uid", "legislature", "numero", "date_scrutin", "seance_ref", "session_ref",
    "organe_uid", "type_vote_code", "type_vote_libelle", "type_majorite",
    "sort_code", "sort_libelle", "titre", "objet", "demandeur",
    "mode_publication", "nombre_votants", "suffrages_exprimes", "seuil_majorite",
    "nb_pour", "nb_contre", "nb_abstentions", "nb_non_votants",
    "dossier_uid", "source_url", "raw_json",
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
                    log.warning("scrutin_json_error", extra={"file": name, "error": str(e)})
                    continue
            yield name, data


def ingest_scrutins(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    scrutin_rows: list[tuple] = []
    all_votes: list[tuple] = []
    all_groupes: list[tuple] = []
    seen_uids: list[str] = []
    errors = 0

    log.info("ingest_scrutins_start", extra={"zip": str(zip_path)})

    # Speed up massive write : disable durability constraints during ingestion.
    conn.execute("PRAGMA synchronous = OFF")

    for fname, raw in _iter_zip_json(zip_path):
        try:
            srow, vrows, grows = parse_scrutin(raw)
            if srow is None:
                continue
            scrutin_rows.append(tuple(srow[c] for c in SCRUTIN_COLS))
            seen_uids.append(srow["uid"])
            all_votes.extend(vrows)
            all_groupes.extend(grows)

            # Flush in batches so memory stays bounded.
            if len(all_votes) >= 50_000:
                _flush(conn, scrutin_rows, all_votes, all_groupes, seen_uids)
                scrutin_rows.clear(); all_votes.clear()
                all_groupes.clear(); seen_uids.clear()
        except Exception as e:
            errors += 1
            log.warning("scrutin_parse_error", extra={"file": fname, "error": str(e)})

    if scrutin_rows or all_votes or all_groupes:
        _flush(conn, scrutin_rows, all_votes, all_groupes, seen_uids)

    # Re-enable durability.
    conn.execute("PRAGMA synchronous = NORMAL")

    # Update dossiers.nb_scrutins cache + materialise discipline aggregates.
    conn.execute("BEGIN")
    conn.execute(
        """
        UPDATE dossiers SET nb_scrutins = COALESCE((
            SELECT COUNT(*) FROM scrutins WHERE scrutins.dossier_uid = dossiers.uid
        ), 0)
        """
    )
    conn.execute("DELETE FROM groupe_discipline_cache")
    conn.execute(
        """
        INSERT INTO groupe_discipline_cache(groupe_uid, expressed, aligned)
        SELECT v.groupe_uid,
               COUNT(*) AS expressed,
               SUM(CASE WHEN v.position = sg.position_majoritaire THEN 1 ELSE 0 END) AS aligned
          FROM votes v
          JOIN scrutin_groupes sg
            ON sg.scrutin_uid = v.scrutin_uid AND sg.groupe_uid = v.groupe_uid
         WHERE v.position IN ('pour','contre','abstention')
         GROUP BY v.groupe_uid
        """
    )
    conn.execute("DELETE FROM deputy_discipline_cache")
    conn.execute(
        """
        INSERT INTO deputy_discipline_cache(
            acteur_uid, expressed, aligned, nb_pour, nb_contre, nb_abstention
        )
        SELECT v.acteur_uid,
               COUNT(*) AS expressed,
               SUM(CASE WHEN v.position = sg.position_majoritaire THEN 1 ELSE 0 END) AS aligned,
               SUM(CASE WHEN v.position = 'pour' THEN 1 ELSE 0 END) AS nb_pour,
               SUM(CASE WHEN v.position = 'contre' THEN 1 ELSE 0 END) AS nb_contre,
               SUM(CASE WHEN v.position = 'abstention' THEN 1 ELSE 0 END) AS nb_abstention
          FROM votes v
          JOIN scrutin_groupes sg
            ON sg.scrutin_uid = v.scrutin_uid AND sg.groupe_uid = v.groupe_uid
         WHERE v.position IN ('pour','contre','abstention')
         GROUP BY v.acteur_uid
        """
    )
    conn.execute("COMMIT")

    n_scrutins = conn.execute("SELECT COUNT(*) AS c FROM scrutins").fetchone()["c"]
    n_votes = conn.execute("SELECT COUNT(*) AS c FROM votes").fetchone()["c"]
    log.info(
        "ingest_scrutins_done",
        extra={"scrutins": n_scrutins, "votes": n_votes, "errors": errors},
    )
    return {"scrutins": n_scrutins, "votes": n_votes, "errors": errors}


def _flush(conn, scrutin_rows, votes_rows, groupe_rows, seen_uids) -> None:
    conn.execute("BEGIN")
    if scrutin_rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO scrutins {_placeholders(SCRUTIN_COLS)}",
            scrutin_rows,
        )
    if seen_uids:
        # Wipe votes + scrutin_groupes for these scrutins before re-insert.
        for i in range(0, len(seen_uids), 500):
            chunk = seen_uids[i:i + 500]
            qmarks = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM votes WHERE scrutin_uid IN ({qmarks})", chunk,
            )
            conn.execute(
                f"DELETE FROM scrutin_groupes WHERE scrutin_uid IN ({qmarks})", chunk,
            )
    if votes_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO votes(scrutin_uid, acteur_uid, groupe_uid, position, par_delegation) "
            "VALUES (?, ?, ?, ?, ?)",
            votes_rows,
        )
    if groupe_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO scrutin_groupes(scrutin_uid, groupe_uid, position_majoritaire, "
            "nb_pour, nb_contre, nb_abstentions, nb_non_votants, nb_membres) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            groupe_rows,
        )
    conn.execute("COMMIT")
