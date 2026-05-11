"""Parse the AMO10 zip into rows for organes / deputies / mandates."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..config import settings
from ..logging_setup import get_logger
from .parse_helpers import as_list, get, text_of, to_int

log = get_logger(__name__)


def _photo_filename(uid: str) -> str:
    """Just the basename, used both for the public URL and on-disk caching."""
    return uid + ".jpg"


# ---------------------------------------------------------------------
# Iterators
# ---------------------------------------------------------------------
def _iter_zip_json(zip_path: Path, prefix: str) -> Iterator[tuple[str, dict]]:
    """Yield (filename, parsed json) for every entry under `(json/)?<prefix>/*.json`.

    AMO10 stores files under `json/<prefix>/...` ; AMO50 stores them
    directly at `<prefix>/...`. We accept both layouts.
    """
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            if not (name.startswith(f"json/{prefix}/") or name.startswith(f"{prefix}/")):
                continue
            with z.open(name) as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    log.warning("amo_parse_error", extra={"file": name, "error": str(e)})
                    continue
            yield name, data


# ---------------------------------------------------------------------
# Organes
# ---------------------------------------------------------------------
def parse_organe(raw: dict) -> dict | None:
    o = raw.get("organe") or {}
    uid = text_of(o.get("uid")) or (o.get("uid") if isinstance(o.get("uid"), str) else None)
    if not isinstance(uid, str):
        return None
    legislature = to_int(o.get("legislature"))
    parent = o.get("organeParent")
    parent_uid = text_of(parent) if parent else None
    return {
        "uid": uid,
        "code_type": text_of(o.get("codeType")),
        "libelle": text_of(o.get("libelle")),
        "libelle_abrege": text_of(o.get("libelleAbrege")) or text_of(o.get("libelleAbrev")),
        "libelle_edition": text_of(o.get("libelleEdition")),
        "legislature": legislature,
        "date_debut": text_of(get(o, "viMoDe", "dateDebut")),
        "date_fin": text_of(get(o, "viMoDe", "dateFin")),
        "couleur": text_of(o.get("couleurAssociee")),
        "organe_parent_uid": parent_uid,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


# ---------------------------------------------------------------------
# Acteurs (deputies)
# ---------------------------------------------------------------------
def _photo_url(uid: str, legislature: int | None = None) -> str:
    """Public URL going through the photo proxy : serves the local cache when
    available, falls back to a placeholder SVG otherwise."""
    return f"/photo/{uid}"


def _photo_url_remote(uid: str, legislature: int | None = None) -> str:
    """Original URL on the AN CDN — used by the photo-downloader."""
    leg = legislature if legislature is not None else settings.legislature
    return f"https://www2.assemblee-nationale.fr/static/tribun/{leg}/photos/carre/{uid[2:]}.jpg"


def parse_acteur(raw: dict) -> tuple[dict | None, list[dict]]:
    """Return (deputy_row, [mandate_rows]). Falls back gracefully on missing fields."""
    a = raw.get("acteur") or {}
    uid = text_of(a.get("uid"))
    if not uid:
        return None, []

    ec = a.get("etatCivil") or {}
    ident = ec.get("ident") or {}
    nais = ec.get("infoNaissance") or {}
    prof = a.get("profession") or {}

    prenom = text_of(ident.get("prenom")) or ""
    nom = text_of(ident.get("nom")) or ""
    nom_complet = f"{prenom} {nom}".strip()
    civilite = text_of(ident.get("civ"))
    date_naissance = text_of(nais.get("dateNais"))
    ville_nais = text_of(nais.get("villeNais"))
    dep_nais = text_of(nais.get("depNais"))
    lieu_naissance = " ".join(p for p in (ville_nais, dep_nais) if p) or None
    profession_lib = text_of(prof.get("libelleCourant"))
    cat_socpro = text_of(get(prof, "socProcINSEE", "catSocPro"))
    uri_hatvp = text_of(a.get("uri_hatvp"))

    # ----- contact info -----
    email = adresse_postale = None
    for ad in as_list(get(a, "adresses", "adresse")):
        if not isinstance(ad, dict):
            continue
        atype = ad.get("@xsi:type") or ""
        if "AdresseMail" in atype and not email:
            email = ad.get("valElec")
        elif "AdressePostale" in atype and not adresse_postale:
            parts = [
                ad.get("intitule"), ad.get("numeroRue"), ad.get("nomRue"),
                ad.get("complementAdresse"), ad.get("codePostal"), ad.get("ville"),
            ]
            adresse_postale = " ".join(p for p in parts if p)

    # ----- mandates -----
    mandate_rows: list[dict] = []
    parl_mandate = None
    gp_mandate = None  # current group
    parpol_mandate = None

    for m in as_list(get(a, "mandats", "mandat")):
        if not isinstance(m, dict):
            continue
        m_uid = m.get("uid")
        if not isinstance(m_uid, str):
            continue
        type_organe = text_of(m.get("typeOrgane"))
        organe_uid = get(m, "organes", "organeRef")
        if isinstance(organe_uid, list):
            organe_uid = organe_uid[0] if organe_uid else None
        if isinstance(organe_uid, dict):
            organe_uid = text_of(organe_uid)
        leg = to_int(m.get("legislature"))
        date_fin = text_of(m.get("dateFin"))
        is_open = date_fin is None
        row = {
            "uid": m_uid,
            "acteur_uid": uid,
            "organe_uid": organe_uid,
            "type_organe": type_organe,
            "legislature": leg,
            "date_debut": text_of(m.get("dateDebut")),
            "date_fin": date_fin,
            "qualite": text_of(get(m, "infosQualite", "libQualite")),
            "nomin_principale": to_int(m.get("nominPrincipale")),
            "raw_json": json.dumps(m, ensure_ascii=False),
        }
        mandate_rows.append(row)

        # Pick the "current" parliamentary mandate for this legislature.
        # ⚠️ Il faut EXIGER typeOrgane == "ASSEMBLEE" : un même député peut
        # avoir plusieurs mandats de type "MandatParlementaire_type" (BUREAU,
        # PRESIDENCE, COMMISSION en tant que président…) et seul le mandat
        # ASSEMBLEE porte la circonscription (champ election.lieu). Sans ce
        # filtre, ~50 députés (ceux ayant un mandat de bureau/présidence qui
        # précède dans le JSON) se retrouvaient sans circo/département.
        if (
            type_organe == "ASSEMBLEE"
            and m.get("@xsi:type") == "MandatParlementaire_type"
            and leg == settings.legislature
            and is_open
            and parl_mandate is None
        ):
            parl_mandate = m
        if type_organe == "GP" and leg == settings.legislature and is_open and gp_mandate is None:
            gp_mandate = m
        if type_organe == "PARPOL" and is_open and parpol_mandate is None:
            parpol_mandate = m

    # ----- circonscription / election (from parl_mandate) -----
    region = departement = departement_code = None
    circonscription = None
    place_hemicycle = None
    date_debut_mandat = date_fin_mandat = None
    is_active = 0
    if parl_mandate:
        is_active = 1
        date_debut_mandat = parl_mandate.get("dateDebut")
        date_fin_mandat = parl_mandate.get("dateFin")
        place_hemicycle = get(parl_mandate, "mandature", "placeHemicycle")
        lieu = get(parl_mandate, "election", "lieu") or {}
        region = lieu.get("region")
        departement = lieu.get("departement")
        departement_code = lieu.get("numDepartement")
        circonscription = to_int(lieu.get("numCirco"))

    # ----- group / party -----
    groupe_uid = groupe_libelle = groupe_abrege = groupe_couleur = None
    if gp_mandate:
        groupe_uid = get(gp_mandate, "organes", "organeRef")

    parti_uid = None
    if parpol_mandate:
        parti_uid = get(parpol_mandate, "organes", "organeRef")

    deputy = {
        "uid": uid,
        "civilite": civilite,
        "prenom": prenom,
        "nom": nom,
        "nom_complet": nom_complet,
        "date_naissance": date_naissance,
        "lieu_naissance": lieu_naissance,
        "profession": profession_lib,
        "cat_socpro": cat_socpro,
        "legislature": settings.legislature if parl_mandate else None,
        "region": text_of(region),
        "departement": text_of(departement),
        "departement_code": text_of(departement_code),
        "circonscription": circonscription,
        "place_hemicycle": text_of(place_hemicycle),
        "date_debut_mandat": text_of(date_debut_mandat),
        "date_fin_mandat": text_of(date_fin_mandat),
        "is_active": is_active,
        "groupe_uid": text_of(groupe_uid) if isinstance(groupe_uid, dict) else groupe_uid,
        "groupe_libelle": None,        # filled by post-pass once organes are loaded
        "groupe_abrege": None,
        "groupe_couleur": None,
        "parti_uid": text_of(parti_uid) if isinstance(parti_uid, dict) else parti_uid,
        "parti_libelle": None,
        "email_an": text_of(email),
        "adresse_postale": adresse_postale,
        "uri_hatvp": uri_hatvp,
        "photo_url": _photo_url(uid),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }
    return deputy, mandate_rows


# ---------------------------------------------------------------------
# Bulk-loaders into SQLite
# ---------------------------------------------------------------------
ORGANE_COLS = (
    "uid", "code_type", "libelle", "libelle_abrege", "libelle_edition",
    "legislature", "date_debut", "date_fin", "couleur", "organe_parent_uid",
    "raw_json",
)
DEPUTY_COLS = (
    "uid", "civilite", "prenom", "nom", "nom_complet", "date_naissance",
    "lieu_naissance", "profession", "cat_socpro", "legislature", "region",
    "departement", "departement_code", "circonscription", "place_hemicycle",
    "date_debut_mandat", "date_fin_mandat", "is_active", "groupe_uid",
    "groupe_libelle", "groupe_abrege", "groupe_couleur", "parti_uid",
    "parti_libelle", "email_an", "adresse_postale", "uri_hatvp",
    "photo_url", "raw_json",
)
MANDATE_COLS = (
    "uid", "acteur_uid", "organe_uid", "type_organe", "legislature",
    "date_debut", "date_fin", "qualite", "nomin_principale", "raw_json",
)


def _placeholders(cols: tuple[str, ...]) -> str:
    return "(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")"


def ingest_amo(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    """Ingest organes + deputies + mandates from an AMO zip. Returns counters."""
    org_seen = dep_seen = mand_seen = errors = 0

    # 1. Organes (load first — deputies reference them via FK).
    log.info("ingest_amo_organes_start", extra={"zip": str(zip_path)})
    organe_rows: list[tuple] = []
    for fname, raw in _iter_zip_json(zip_path, "organe"):
        try:
            row = parse_organe(raw)
            if row is None:
                continue
            organe_rows.append(tuple(row[c] for c in ORGANE_COLS))
            org_seen += 1
        except Exception as e:
            errors += 1
            log.warning("organe_parse_error", extra={"file": fname, "error": str(e)})

    if organe_rows:
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT OR REPLACE INTO organes {_placeholders(ORGANE_COLS)}",
            organe_rows,
        )
        conn.execute("COMMIT")
    log.info("ingest_amo_organes_done", extra={"count": org_seen})

    # 2. Deputies + mandates.
    log.info("ingest_amo_acteurs_start")
    deputy_rows: list[tuple] = []
    mandate_rows: list[tuple] = []
    for fname, raw in _iter_zip_json(zip_path, "acteur"):
        try:
            dep, mans = parse_acteur(raw)
            if dep is None:
                continue
            deputy_rows.append(tuple(dep[c] for c in DEPUTY_COLS))
            for m in mans:
                mandate_rows.append(tuple(m[c] for c in MANDATE_COLS))
            dep_seen += 1
            mand_seen += len(mans)
        except Exception as e:
            errors += 1
            log.warning("acteur_parse_error", extra={"file": fname, "error": str(e)})

    if deputy_rows:
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT OR REPLACE INTO deputies {_placeholders(DEPUTY_COLS)}",
            deputy_rows,
        )
        conn.execute("COMMIT")
    if mandate_rows:
        conn.execute("BEGIN")
        # We don't know which mandates have been deleted server-side from a
        # single snapshot, so we wipe + reinsert mandates of seen acteurs.
        seen_uids = sorted({r[1] for r in mandate_rows})
        # Bulk delete in chunks of 500 to stay under sqlite's variable limit.
        for i in range(0, len(seen_uids), 500):
            chunk = seen_uids[i:i + 500]
            qmarks = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM mandates WHERE acteur_uid IN ({qmarks})", chunk,
            )
        conn.executemany(
            f"INSERT OR REPLACE INTO mandates {_placeholders(MANDATE_COLS)}",
            mandate_rows,
        )
        conn.execute("COMMIT")
    log.info(
        "ingest_amo_acteurs_done",
        extra={"deputies": dep_seen, "mandates": mand_seen, "errors": errors},
    )

    # 3. Post-pass: fill in group/party labels + colour from organes.
    conn.execute("BEGIN")
    conn.execute(
        """
        UPDATE deputies SET
            groupe_libelle = (SELECT libelle FROM organes WHERE uid = deputies.groupe_uid),
            groupe_abrege  = (SELECT libelle_abrege FROM organes WHERE uid = deputies.groupe_uid),
            groupe_couleur = (SELECT couleur FROM organes WHERE uid = deputies.groupe_uid),
            parti_libelle  = (SELECT libelle FROM organes WHERE uid = deputies.parti_uid)
        """
    )
    conn.execute("COMMIT")

    return {
        "organes": org_seen,
        "deputies": dep_seen,
        "mandates": mand_seen,
        "errors": errors,
    }
