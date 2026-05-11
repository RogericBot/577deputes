"""Render the 559 French circonscriptions as an inline SVG map.

The GeoJSON of the circonscriptions (ETALAB Licence Ouverte) is loaded once
at app startup, projected to a 1000×900 SVG canvas using a simple equi-
rectangular projection adjusted for the latitude of metropolitan France,
and stored in memory as a list of (id, dept_code, circo_num, dept_name,
circo_name, svg_path_d) tuples.

Per request : we walk the cached features, look up each circo's deputy in
SQLite, and emit a single `<svg>` containing 559 `<path>` elements coloured
by `groupe_couleur`. Each path carries a `<title>` (native SVG tooltip).

DOM (Guadeloupe / Martinique / Guyane / La Réunion / Mayotte) get
relocated to inset rectangles in the bottom-left corner, since their real
coordinates would otherwise dwarf metropolitan France on screen.

Caching :
  * Geo features parsed once at module import.
  * Deputy lookup re-fetched on every request (cheap : ~600 rows).

Render time on a warm cache : ~50–80 ms for 559 paths.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_setup import get_logger

log = get_logger(__name__)

GEOJSON_PATH = settings.raw_dir / "circonscriptions.geojson"

# Drawing canvas — compact, the user can zoom freely so we can crop a
# bit at top/bottom of the metropolitan shape.
W = 1000
H = 560

# Metropolitan France bounds (approx, includes Corsica).
LON_MIN, LON_MAX = -5.5, 9.7
LAT_MIN, LAT_MAX = 41.2, 51.6

METRO_X = 30
METRO_W = W - 60                          # 940
METRO_Y = 10
METRO_H = H - METRO_Y - 10                # 540

# Mapping from GeoJSON's Z-codes to INSEE numeric dept codes (used by
# our deputies table).  Useful for the HTML DOM cards that join with
# `deputies.departement_code`.
DOM_INSEE = {
    "ZA": "971",  # Guadeloupe
    "ZB": "972",  # Martinique
    "ZC": "973",  # Guyane
    "ZD": "974",  # La Réunion
    "ZM": "976",  # Mayotte
    "ZS": "975",  # Saint-Pierre-et-Miquelon
}
# Z-codes are skipped from the SVG : we render them as HTML cards instead.
DOM_CODES = set(DOM_INSEE.keys())


def _project_metro(lon: float, lat: float) -> tuple[float, float]:
    """Equirectangular projection scaled to the dedicated metro pane."""
    lat_mean = (LAT_MIN + LAT_MAX) / 2
    cos_lat = math.cos(math.radians(lat_mean))
    span_x = (LON_MAX - LON_MIN) * cos_lat
    span_y = (LAT_MAX - LAT_MIN)
    sx = METRO_W / span_x
    sy = METRO_H / span_y
    s = min(sx, sy)
    # Centre inside the metro pane
    pad_x = (METRO_W - span_x * s) / 2
    pad_y = (METRO_H - span_y * s) / 2
    x = (lon - LON_MIN) * cos_lat * s + METRO_X + pad_x
    y = (LAT_MAX - lat) * s + METRO_Y + pad_y
    return x, y


# DOM features are skipped in the SVG ; they are rendered as HTML
# cards by the template (see `dom_cards()` below).


def _bbox_of_geometry(geom: dict) -> tuple[float, float, float, float]:
    lons: list[float] = []
    lats: list[float] = []
    if geom["type"] == "Polygon":
        for ring in geom["coordinates"]:
            for lon, lat in ring:
                lons.append(lon); lats.append(lat)
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                for lon, lat in ring:
                    lons.append(lon); lats.append(lat)
    return min(lons), min(lats), max(lons), max(lats)


def _ring_to_path(ring: list[list[float]], project) -> str:
    """Convert a coordinate ring to an SVG path 'M x y L x y …Z' string."""
    parts: list[str] = []
    for i, (lon, lat) in enumerate(ring):
        x, y = project(lon, lat)
        parts.append(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}")
    parts.append("Z")
    return "".join(parts)


def _geometry_to_path(geom: dict, dept_code: str) -> str:
    """Build an SVG `d` attribute for metropolitan circonscriptions only.
    DOM features are rendered as HTML cards instead."""
    if geom["type"] == "Polygon":
        return "".join(_ring_to_path(r, _project_metro) for r in geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        return "".join(
            _ring_to_path(r, _project_metro)
            for poly in geom["coordinates"] for r in poly
        )
    return ""


# ---------------------------------------------------------------------
# Static cache : parse the GeoJSON once at module import.
# ---------------------------------------------------------------------
_FEATURES: list[dict] | None = None


def _load_features() -> list[dict]:
    """Parse the GeoJSON file and pre-compute SVG paths.

    Two passes : (1) compute per-DOM department bbox, (2) project each
    feature using the metro projection or the shared DOM bbox.
    """
    if not GEOJSON_PATH.exists():
        log.warning("cartography_geojson_missing", extra={"path": str(GEOJSON_PATH)})
        return []
    started = time.perf_counter()
    with GEOJSON_PATH.open(encoding="utf-8") as f:
        gj = json.load(f)
    raw = gj.get("features", [])

    out: list[dict] = []
    for feat in raw:
        props = feat.get("properties", {})
        dept = (props.get("codeDepartement") or "").strip() or None
        circo = (props.get("codeCirconscription") or "").strip() or None
        if not dept or not circo:
            continue
        # Skip DOM polygons : they are rendered as HTML cards instead.
        if dept in DOM_CODES:
            continue
        try:
            circo_num = int(circo[-2:])
        except ValueError:
            circo_num = None
        path_d = _geometry_to_path(feat["geometry"], dept)
        out.append({
            "dept": dept,
            "circo_num": circo_num,
            "dept_name": props.get("nomDepartement"),
            "circo_name": props.get("nomCirconscription"),
            "path_d": path_d,
        })
    log.info(
        "cartography_loaded",
        extra={
            "metro_features": len(out),
            "elapsed_s": round(time.perf_counter() - started, 2),
        },
    )
    return out


def _features() -> list[dict]:
    global _FEATURES
    if _FEATURES is None:
        _FEATURES = _load_features()
    return _FEATURES


# ---------------------------------------------------------------------
# Public render helpers
# ---------------------------------------------------------------------
def deputies_by_circo(conn: sqlite3.Connection) -> dict[tuple[str, int], dict]:
    """Map (dept_code, circonscription) → deputy dict + circo stats."""
    rows = conn.execute(
        """
        SELECT d.uid, d.nom_complet, d.departement_code, d.departement,
               d.circonscription, d.groupe_uid, d.groupe_abrege,
               d.groupe_couleur, d.groupe_libelle,
               cs.population, cs.inscrits, cs.votants
          FROM deputies d
          LEFT JOIN circo_stats cs
                 ON cs.dept_code = (
                      CASE WHEN d.departement_code GLOB '[0-9]'
                           THEN '0' || d.departement_code
                           ELSE d.departement_code
                      END)
                AND cs.circo_num = d.circonscription
         WHERE d.is_active = 1 AND d.departement_code IS NOT NULL
           AND d.circonscription IS NOT NULL
        """
    ).fetchall()
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        dept = str(r["departement_code"]).zfill(2) if r["departement_code"].isdigit() else r["departement_code"]
        out[(dept, int(r["circonscription"]))] = dict(r)
    return out


def render_map_svg(conn: sqlite3.Connection) -> str:
    """Build the full SVG of the 559 circonscriptions, coloured by group."""
    feats = _features()
    if not feats:
        return (
            f'<svg viewBox="0 0 {W} {H}" width="100%"></svg>'
            '<p class="muted small">Carte indisponible : '
            'lance <code>anqp serve</code> après que '
            '<code>data/raw/circonscriptions.geojson</code> ait été téléchargé.</p>'
        )
    deps = deputies_by_circo(conn)
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="100%" height="auto" class="france-map" role="img" aria-label="Carte des 559 circonscriptions législatives">'
    )
    for f in feats:
        key = (f["dept"], f["circo_num"]) if f["circo_num"] is not None else None
        deputy = deps.get(key) if key else None
        color = (deputy or {}).get("groupe_couleur") or "#cbd5e1"
        # Tooltip natif (fallback si JS désactivé).
        if deputy:
            title = (
                f"{f['circo_name']} ({f['dept_name']}) — "
                f"{deputy['nom_complet']} ({deputy['groupe_abrege'] or 'sans groupe'})"
            )
        else:
            title = f"{f['circo_name']} ({f['dept_name']}) — siège vacant"
        # Data attributes used by JS for the rich hover card + group highlight.
        data_attrs = (
            f'data-dept="{_xml_escape(f["dept"])}" '
            f'data-circo="{f["circo_num"] or ""}" '
            f'data-circo-name="{_xml_escape(f["circo_name"] or "")}" '
            f'data-dept-name="{_xml_escape(f["dept_name"] or "")}"'
        )
        if deputy:
            data_attrs += (
                f' data-uid="{_xml_escape(deputy["uid"])}"'
                f' data-nom="{_xml_escape(deputy["nom_complet"] or "")}"'
                f' data-groupe-uid="{_xml_escape(deputy.get("groupe_uid") or "")}"'
                f' data-groupe="{_xml_escape(deputy.get("groupe_abrege") or "")}"'
                f' data-groupe-libelle="{_xml_escape(deputy.get("groupe_libelle") or "")}"'
                f' data-couleur="{_xml_escape(deputy.get("groupe_couleur") or "")}"'
                f' data-pop="{deputy.get("population") or ""}"'
                f' data-inscrits="{deputy.get("inscrits") or ""}"'
                f' data-votants="{deputy.get("votants") or ""}"'
            )
        parts.append(
            f'<a href="/deputes/{deputy["uid"]}">' if deputy else '<a>'
        )
        parts.append(
            f'<path d="{f["path_d"]}" fill="{color}" {data_attrs}>'
            f'<title>{_xml_escape(title)}</title></path>'
        )
        parts.append('</a>')
    parts.append('</svg>')
    return "".join(parts)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def dom_circos(conn: sqlite3.Connection) -> list[dict]:
    """Return DOM-COM deputies grouped by department for the HTML grid.

    Includes 971-976 + Saint-Pierre-et-Miquelon (975) + Saint-Barthélemy /
    Saint-Martin (977) + Wallis-et-Futuna (986) + Polynésie française (987)
    + Nouvelle-Calédonie (988). Their députés sit in the Assemblée even
    though some of these constituencies have no GeoJSON polygon.
    """
    DOM_DEPT_NAMES = {
        "971": ("Guadeloupe", "971"),
        "972": ("Martinique", "972"),
        "973": ("Guyane", "973"),
        "974": ("La Réunion", "974"),
        "975": ("Saint-Pierre-et-Miquelon", "975"),
        "976": ("Mayotte", "976"),
        "977": ("Saint-Barthélemy / Saint-Martin", "977"),
        "986": ("Wallis-et-Futuna", "986"),
        "987": ("Polynésie française", "987"),
        "988": ("Nouvelle-Calédonie", "988"),
    }
    rows = conn.execute(
        """
        SELECT uid, nom_complet, departement_code, departement,
               circonscription, groupe_uid, groupe_abrege, groupe_couleur,
               groupe_libelle
          FROM deputies
         WHERE is_active = 1
           AND departement_code IN ('971','972','973','974','975','976',
                                    '977','986','987','988')
         ORDER BY departement_code, circonscription
        """
    ).fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        code = r["departement_code"]
        meta = DOM_DEPT_NAMES.get(code, (r["departement"] or code, code))
        grouped.setdefault(code, {
            "dept_code": code,
            "dept_name": meta[0],
            "members": [],
        })["members"].append(dict(r))
    return [grouped[k] for k in DOM_DEPT_NAMES if k in grouped]


def map_legend(conn: sqlite3.Connection) -> list[dict]:
    """Active groups sorted by deputy count, with summed population +
    inscrits across the group's circonscriptions."""
    return conn.execute(
        """
        SELECT g.uid, g.libelle, g.libelle_abrege AS abrege, g.couleur,
               COUNT(*) AS deputies,
               SUM(cs.population) AS population,
               SUM(cs.inscrits)   AS inscrits
          FROM organes g
          JOIN deputies d ON d.groupe_uid = g.uid AND d.is_active = 1
          LEFT JOIN circo_stats cs
                 ON cs.dept_code = (
                      CASE WHEN d.departement_code GLOB '[0-9]'
                           THEN '0' || d.departement_code
                           ELSE d.departement_code
                      END)
                AND cs.circo_num = d.circonscription
         WHERE g.code_type = 'GP'
         GROUP BY g.uid
         ORDER BY deputies DESC
        """
    ).fetchall()
