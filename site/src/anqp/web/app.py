"""FastAPI app — wires routes, templates, static files, dependencies."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..db import connect
from ..logging_setup import get_logger
from . import queries as Q
from . import queries_legislative as QL
from .legislature import (
    available_legislatures,
    current_legislature,
    set_legislature,
)
from .refresh_loop import (
    is_enabled as _refresh_enabled,
    next_run_in_seconds as _next_refresh_in,
    start_refresh_loop,
    stop_refresh_loop,
)

log = get_logger(__name__)

ROOT = Path(__file__).parent

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    """Start the auto-refresh daemon on boot, stop it on shutdown.

    Also kicks off a background warm-up of the analyses cache so the
    first hit on the home page is fast even after a restart.
    """
    from . import analytics
    analytics.init()
    if _refresh_enabled():
        start_refresh_loop()

    # Warm-up des analyses dans un thread daemon (~10s en background,
    # ne bloque pas le démarrage de l'app)
    def _warmup_analyses_bg():
        try:
            conn = connect(read_only=True)
            try:
                QL.warmup_analyses_cache(conn)
                log.info("analyses_warmup_done")
            finally:
                conn.close()
        except Exception:
            log.exception("analyses_warmup_failed")

    import threading as _threading
    _threading.Thread(target=_warmup_analyses_bg, daemon=True).start()

    try:
        yield
    finally:
        stop_refresh_loop()


app = FastAPI(
    title="577députés — API",
    description=(
        "API JSON, lecture seule, du site **577députés**. "
        "Ressources : textes (dossiers législatifs), amendements, scrutins publics, "
        "questions parlementaires, députés et organes politiques de la 17ᵉ législature "
        "de l'Assemblée nationale française.\n\n"
        "Données issues de [data.assemblee-nationale.fr](https://data.assemblee-nationale.fr) "
        "(Licence Ouverte / Etalab). Site indépendant, sans affiliation."
    ),
    version="0.6.0",
    lifespan=_lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# ---------------------------------------------------------------------
# Analytics middleware — compteur de pages vues + visiteurs uniques
# ---------------------------------------------------------------------
_ANALYTICS_SKIP_PREFIXES = (
    "/static/", "/api/", "/photo/", "/admin/",
    "/sitemap.xml", "/robots.txt", "/legislature/",
    "/favicon.ico", "/.well-known/",
)


@app.middleware("http")
async def _analytics_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path
        if (
            response.status_code == 200
            and request.method == "GET"
            and not any(path.startswith(p) for p in _ANALYTICS_SKIP_PREFIXES)
        ):
            from . import analytics
            ua = request.headers.get("user-agent", "")
            category, is_bot = analytics.categorize_ua(ua)
            normalized_path = analytics.normalize_path(path)

            old_id = request.cookies.get("vid")
            new_id = analytics.record_visit(
                old_id, is_bot=is_bot,
                category=category, path=normalized_path,
            )
            # Cookie posé seulement pour les visiteurs humains
            if not is_bot and old_id != new_id:
                response.set_cookie(
                    "vid", new_id,
                    max_age=365 * 24 * 3600,
                    samesite="lax",
                    httponly=True,
                    secure=True,
                )
    except Exception:
        log.exception("analytics_middleware silent error")
    return response


_SAFE_NEXT_PATH = re.compile(r"^/[A-Za-z0-9/_\-.?&=%+]*$")


@app.get("/legislature/{leg}", include_in_schema=False)
def set_legislature_cookie(leg: int, request: Request):
    """Persist the user's legislature choice in a cookie + bounce home."""
    from fastapi.responses import RedirectResponse
    raw = request.query_params.get("next", "/") or "/"
    # Refuser tout 'next' qui n'est pas un chemin local : protège contre
    # l'open redirect (ex: ?next=https://evil.com ou ?next=//evil.com).
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or raw.startswith("/\\")
        or not _SAFE_NEXT_PATH.match(raw)
    ):
        raw = "/"
    resp = RedirectResponse(raw, status_code=303)
    resp.set_cookie(
        "legislature", str(leg),
        max_age=365 * 24 * 3600,
        samesite="lax",
        httponly=True,
        secure=True,
    )
    return resp


_PHOTO_UID_RE = re.compile(r"^PA\d+$")


@app.get("/photo/{uid}", include_in_schema=False)
def photo_proxy(uid: str):
    """Serve a deputy photo from the local cache, with placeholder fallback."""
    from fastapi.responses import FileResponse, RedirectResponse
    if not _PHOTO_UID_RE.match(uid):
        raise HTTPException(404)
    photo = ROOT / "static" / "photos" / f"{uid}.jpg"
    if photo.exists() and photo.stat().st_size > 0:
        return FileResponse(photo, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    placeholder = ROOT / "static" / "photos" / "_placeholder.svg"
    return FileResponse(placeholder, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
templates = Jinja2Templates(directory=ROOT / "templates")


# ---------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------
def _format_date(value: str | None) -> str:
    if not value:
        return "—"
    return value[:10]


def _format_int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _format_float(value: Any, ndigits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{ndigits}f}"
    except (TypeError, ValueError):
        return str(value)


def _truncate_html(value: str | None, length: int = 240) -> str:
    if not value:
        return ""
    # Drop tags for the listing preview.
    import re
    s = re.sub(r"<[^>]+>", " ", value)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > length:
        s = s[:length].rsplit(" ", 1)[0] + "…"
    return s


def _statut_label(value: str | None) -> str:
    return {
        "avec_reponse": "Réponse publiée",
        "sans_reponse": "Sans réponse",
        "cloturee": "Clôturée",
    }.get(value or "", value or "—")


# Type codes used in the SQL data → user-facing French names.
_QTYPE_LONG = {
    "QE":   "Question écrite",
    "QOSD": "Question orale",
    "QG":   "Question au Gouvernement",
    "QAG":  "Question au Gouvernement",
}
_QTYPE_SHORT = {
    "QE":   "Écrite",
    "QOSD": "Orale",
    "QG":   "Au Gouv.",
    "QAG":  "Au Gouv.",
}


def _qtype_long(value: str | None) -> str:
    if not value:
        return "—"
    return _QTYPE_LONG.get(value, value)


def _qtype_short(value: str | None) -> str:
    if not value:
        return "—"
    return _QTYPE_SHORT.get(value, value)


_DOSSIER_STATUT_LONG = {
    "en_cours":  "En cours",
    "retire":    "Retiré",
    "promulgue": "Promulguée",
    "adopte":    "Adopté",
    "rejete":    "Rejeté",
    "caduc":     "Caduc",
}


def _statut_dossier(value: str | None) -> str:
    if not value:
        return "—"
    return _DOSSIER_STATUT_LONG.get(value, value)


def _is_statut_terminal(value: str | None) -> bool:
    """True for statuts that mark the end of a dossier's life."""
    return value in {"retire", "promulgue", "adopte", "rejete", "caduc"}


def _texte_type(procedure: str | None) -> dict:
    """Classify a procedure label into a coarse category for badges."""
    p = (procedure or "").strip().lower()
    if p.startswith("projet de loi") or p.startswith("projet de ratification"):
        return {"key": "pjl", "label": "PJL", "long": "Projet de loi (gouvernement)"}
    if p.startswith("proposition de loi"):
        return {"key": "ppl", "label": "PPL", "long": "Proposition de loi (parlementaires)"}
    if p.startswith("résolution") or p.startswith("resolution"):
        return {"key": "resolution", "label": "Résolution", "long": "Résolution"}
    if p.startswith("rapport") or p.startswith("mission") or "enquête" in p or "enquete" in p:
        return {"key": "rapport", "label": "Rapport", "long": "Rapport, mission ou commission d'enquête"}
    return {"key": "autre", "label": "Autre", "long": procedure or "Autre"}


templates.env.filters["fdate"] = _format_date
templates.env.filters["fint"] = _format_int
templates.env.filters["ffloat"] = _format_float
templates.env.filters["truncate_html"] = _truncate_html
templates.env.filters["statut"] = _statut_label
templates.env.filters["qtype_long"] = _qtype_long
templates.env.filters["qtype_short"] = _qtype_short
templates.env.filters["statut_dossier"] = _statut_dossier
templates.env.filters["is_terminal"] = _is_statut_terminal
templates.env.filters["texte_type"] = _texte_type


def _from_json(value):
    if not value:
        return []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


templates.env.filters["from_json"] = _from_json


# ---------------------------------------------------------------------
# safe_html : assainit du HTML brut (notamment celui issu des dumps AN)
# avant rendu, pour éviter toute XSS stockée. À utiliser à la place
# du filtre `safe` standard de Jinja sur du contenu user/externe.
# Import bleach différé : si la lib n'est pas installée (vieux déploiement),
# on échappe le HTML par défaut au lieu de planter l'app entière.
# ---------------------------------------------------------------------
from markupsafe import Markup as _Markup

_BLEACH_TAGS = {
    "p", "br", "em", "strong", "b", "i", "u",
    "ul", "ol", "li",
    "sup", "sub",
    "a", "blockquote", "code", "pre",
    "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
}
_BLEACH_ATTRS = {
    "a": ["href", "title", "rel"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}

try:
    import bleach as _bleach
except ImportError:
    _bleach = None


def _safe_html(value):
    if not value:
        return _Markup("")
    if _bleach is None:
        # Fail-safe : pas de bleach => on échappe (texte brut visible).
        # Mieux que crasher l'app, et zéro risque XSS.
        return str(value)
    cleaned = _bleach.clean(
        str(value),
        tags=_BLEACH_TAGS,
        attributes=_BLEACH_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
        strip_comments=True,
    )
    return _Markup(cleaned)


templates.env.filters["safe_html"] = _safe_html
templates.env.globals["current_legislature"] = current_legislature


def _refresh_available_legislatures() -> list[int]:
    """Cache available legislatures (rarely changes — only after ingestion)."""
    try:
        conn = connect(read_only=True)
    except Exception:
        return []
    try:
        return available_legislatures(conn)
    finally:
        conn.close()


_AVAILABLE_LEGS_CACHE = _refresh_available_legislatures()


def _get_available_legs() -> list[int]:
    """Return the cached list, refreshing if it's empty (e.g. fresh DB)."""
    global _AVAILABLE_LEGS_CACHE
    if not _AVAILABLE_LEGS_CACHE:
        _AVAILABLE_LEGS_CACHE = _refresh_available_legislatures()
    return _AVAILABLE_LEGS_CACHE


templates.env.globals["available_legislatures"] = _get_available_legs


# ---------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    conn = connect(read_only=True)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------
# SEO — robots.txt + sitemap.xml
# ---------------------------------------------------------------------
SITE_BASE_URL = "https://577deputes.fr"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /legislature/\n"
        "\n"
        f"Sitemap: {SITE_BASE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml(conn: sqlite3.Connection = Depends(get_conn)):
    """Sitemap dynamique : navigation + fiches députés + fiches textes."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")

    static_paths = [
        ("/", "daily", "1.0"),
        ("/textes", "daily", "0.9"),
        ("/scrutins", "daily", "0.9"),
        ("/questions", "daily", "0.9"),
        ("/deputes", "weekly", "0.9"),
        ("/carte", "weekly", "0.8"),
        ("/tops", "daily", "0.8"),
        ("/stats", "weekly", "0.7"),
        ("/stats/groupes", "weekly", "0.6"),
        ("/stats/ministeres", "weekly", "0.6"),
        ("/stats/themes", "weekly", "0.6"),
        ("/stats/deputes", "weekly", "0.6"),
        ("/stats/temporel", "weekly", "0.6"),
        ("/stats/textes", "weekly", "0.6"),
        ("/stats/amendements", "weekly", "0.6"),
        ("/stats/scrutins", "weekly", "0.6"),
        ("/clusters", "weekly", "0.7"),
        ("/stats/clusters", "weekly", "0.6"),
        ("/dissidents", "weekly", "0.7"),
        ("/comparer", "monthly", "0.6"),
        ("/recherche", "monthly", "0.5"),
        ("/a-propos", "monthly", "0.5"),
    ]

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    for path, freq, prio in static_paths:
        parts.append(
            f"  <url><loc>{SITE_BASE_URL}{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )

    deputies = conn.execute(
        "SELECT uid FROM deputies WHERE is_active = 1 ORDER BY uid"
    ).fetchall()
    for d in deputies:
        parts.append(
            f"  <url><loc>{SITE_BASE_URL}/deputes/{d['uid']}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>weekly</changefreq>"
            f"<priority>0.7</priority></url>"
        )

    textes = conn.execute("SELECT uid FROM dossiers ORDER BY uid").fetchall()
    for t in textes:
        parts.append(
            f"  <url><loc>{SITE_BASE_URL}/textes/{t['uid']}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>weekly</changefreq>"
            f"<priority>0.6</priority></url>"
        )

    scrutins = conn.execute(
        "SELECT uid FROM scrutins ORDER BY uid"
    ).fetchall()
    for s in scrutins:
        parts.append(
            f"  <url><loc>{SITE_BASE_URL}/scrutins/{s['uid']}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>monthly</changefreq>"
            f"<priority>0.5</priority></url>"
        )

    parts.append("</urlset>")
    xml = "\n".join(parts)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Resolve the legislature override (cookie or ?leg= query) before each request.
@app.middleware("http")
async def _legislature_middleware(request: Request, call_next):
    raw = request.query_params.get("leg") or request.cookies.get("legislature")
    if raw:
        try:
            set_legislature(int(raw))
        except ValueError:
            set_legislature(None)
    else:
        set_legislature(None)
    return await call_next(request)


# Request timing : logged server-side only (no Server-Timing header in prod
# to avoid fingerprinting / perf leakage to clients).
@app.middleware("http")
async def _timing_middleware(request: Request, call_next):
    started = time.perf_counter()
    resp = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    log.info(
        "http",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": resp.status_code,
            "ms": round(elapsed_ms, 1),
        },
    )
    return resp


# ---------------------------------------------------------------------
# Pages — server-rendered HTML
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    overview = Q.db_overview(conn)
    overall = Q.stats_overall(conn)
    latest = conn.execute(
        """
        SELECT q.uid, q.type, q.numero, q.titre, q.auteur_nom_complet,
               q.auteur_groupe_abrege, q.date_question, q.statut
          FROM questions q
         WHERE q.date_question IS NOT NULL
         ORDER BY q.date_question DESC, q.numero DESC
         LIMIT 10
        """
    ).fetchall()
    legislative = QL.home_legislative_overview(conn)
    discipline = QL.home_discipline_summary(conn)
    from . import cluster_typology as CT
    cluster_overview = CT.stats_overview(conn)
    # Version non-bloquante : si le cache analyses n'est pas chaud
    # (warm-up encore en cours après restart), on retourne None et
    # la home s'affiche sans la section "À la une" — pas de latence.
    highlights = QL.analyses_homepage_highlights_if_cached(conn)
    return templates.TemplateResponse(
        request, "home.html",
        {"overview": overview, "overall": overall, "latest": latest,
         "legislative": legislative, "discipline": discipline,
         "cluster_overview": cluster_overview,
         "highlights": highlights},
    )


@app.get("/questions", response_class=HTMLResponse)
def questions_list(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    statut: str | None = None,
    auteur_uid: str | None = None,
    groupe_uid: str | None = None,
    rubrique: str | None = None,
    ministere: str | None = None,
    departement: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
    sort: str = "date_q_desc",
    page: int = 1,
    page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = Q.search_questions(
        conn,
        q_text=q, qtype=type, statut=statut, auteur_uid=auteur_uid,
        groupe_uid=groupe_uid, rubrique=rubrique, ministere=ministere,
        departement_code=departement, date_min=date_min, date_max=date_max,
        sort=sort, page=page, page_size=page_size,
    )
    facets = {
        "groups": Q.list_groups(conn),
        "ministries": Q.list_ministries(conn),
        "rubriques": Q.list_rubriques(conn),
        "departements": Q.list_departements(conn),
    }
    filters = {
        "q": q or "", "type": type or "", "statut": statut or "",
        "auteur_uid": auteur_uid or "", "groupe_uid": groupe_uid or "",
        "rubrique": rubrique or "", "ministere": ministere or "",
        "departement": departement or "", "date_min": date_min or "",
        "date_max": date_max or "", "sort": sort, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "questions.html",
        {"result": result, "filters": filters, "facets": facets},
    )


@app.get("/questions/{uid}", response_class=HTMLResponse)
def question_detail(
    request: Request, uid: str, conn: sqlite3.Connection = Depends(get_conn)
):
    row = Q.get_question(conn, uid)
    if not row:
        raise HTTPException(404, f"Question {uid} introuvable")
    seance_ctx = Q.get_qag_seance(conn, row)
    return templates.TemplateResponse(
        request, "question_detail.html", {"q": row, "seance_ctx": seance_ctx}
    )


@app.get("/deputes", response_class=HTMLResponse)
def deputies_list(
    request: Request,
    q: str | None = None,
    groupe_uid: str | None = None,
    departement: str | None = None,
    sort: str = "nom_asc",
    is_active: int | None = 1,
    page: int = 1,
    page_size: int = 100,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = Q.list_deputies(
        conn, q_text=q, groupe_uid=groupe_uid, departement_code=departement,
        sort=sort, is_active=is_active, page=page, page_size=page_size,
    )
    facets = {
        "groups": Q.list_groups(conn),
        "departements": Q.list_departements(conn),
    }
    filters = {
        "q": q or "", "groupe_uid": groupe_uid or "", "departement": departement or "",
        "sort": sort, "is_active": is_active, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "deputies.html",
        {"result": result, "filters": filters, "facets": facets},
    )


@app.get("/deputes/{uid}", response_class=HTMLResponse)
def deputy_detail(
    request: Request, uid: str, conn: sqlite3.Connection = Depends(get_conn)
):
    dep = Q.get_deputy(conn, uid)
    if not dep:
        raise HTTPException(404, f"Député {uid} introuvable")
    activity = Q.get_deputy_activity(conn, uid)
    mandates = Q.get_deputy_mandates(conn, uid)
    recent = conn.execute(
        """
        SELECT uid, type, numero, titre, statut, date_question, date_reponse
          FROM questions
         WHERE auteur_uid = ?
         ORDER BY date_question DESC NULLS LAST
         LIMIT 25
        """,
        (uid,),
    ).fetchall()
    legislative = QL.deputy_legislative_activity(conn, uid)
    timeline = Q.deputy_monthly_activity(conn, uid)
    return templates.TemplateResponse(
        request, "deputy_detail.html",
        {"d": dep, "activity": activity, "mandates": mandates, "recent": recent,
         "legislative": legislative, "timeline": timeline},
    )


# ---------------------------------------------------------------------
# Stats pages
# ---------------------------------------------------------------------
@app.get("/stats", response_class=HTMLResponse)
def stats_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_index.html",
        {"overview": Q.db_overview(conn), "overall": Q.stats_overall(conn)},
    )


@app.get("/stats/groupes", response_class=HTMLResponse)
def stats_groups(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_groups.html", {"rows": Q.stats_by_group(conn)},
    )


@app.get("/stats/ministeres", response_class=HTMLResponse)
def stats_min(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_min.html", {"rows": Q.stats_by_ministry(conn, limit=30)},
    )


@app.get("/stats/themes", response_class=HTMLResponse)
def stats_rubriques(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_themes.html", {"rows": Q.stats_by_rubrique(conn, limit=40)},
    )


@app.get("/stats/deputes", response_class=HTMLResponse)
def stats_deputies_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_deputies.html", {"rows": Q.stats_top_deputies(conn, limit=30)},
    )


@app.get("/stats/temporel", response_class=HTMLResponse)
def stats_time(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_time.html", {"rows": Q.stats_timeseries(conn)},
    )


# ---------------------------------------------------------------------
# Exports — CSV / JSON of the current question filter set
# ---------------------------------------------------------------------
EXPORT_LIMIT = 10000


def _export_questions(
    conn: sqlite3.Connection, filters: dict[str, Any]
) -> list[dict]:
    res = Q.search_questions(
        conn,
        q_text=filters.get("q") or None,
        qtype=filters.get("type") or None,
        statut=filters.get("statut") or None,
        auteur_uid=filters.get("auteur_uid") or None,
        groupe_uid=filters.get("groupe_uid") or None,
        rubrique=filters.get("rubrique") or None,
        ministere=filters.get("ministere") or None,
        departement_code=filters.get("departement") or None,
        date_min=filters.get("date_min") or None,
        date_max=filters.get("date_max") or None,
        sort=filters.get("sort") or "date_q_desc",
        page=1,
        page_size=EXPORT_LIMIT,
    )
    return res["rows"]


@app.get("/export/questions.csv")
def export_csv(
    q: str | None = None, type: str | None = None, statut: str | None = None,
    auteur_uid: str | None = None, groupe_uid: str | None = None,
    rubrique: str | None = None, ministere: str | None = None,
    departement: str | None = None, date_min: str | None = None,
    date_max: str | None = None, sort: str = "date_q_desc",
    conn: sqlite3.Connection = Depends(get_conn),
):
    rows = _export_questions(conn, locals())

    def gen():
        buf = io.StringIO()
        cols = [
            "uid", "type", "numero", "titre", "auteur_uid",
            "auteur_nom_complet", "auteur_groupe_abrege",
            "ministere_interroge_court", "rubrique", "statut",
            "date_question", "date_reponse", "delai_reponse_jours", "source_url",
        ]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        yield buf.getvalue()
        for r in rows:
            buf.seek(0)
            buf.truncate()
            w.writerow({c: r.get(c) for c in cols})
            yield buf.getvalue()

    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=577deputes_questions.csv"},
    )


@app.get("/export/questions.json")
def export_json(
    q: str | None = None, type: str | None = None, statut: str | None = None,
    auteur_uid: str | None = None, groupe_uid: str | None = None,
    rubrique: str | None = None, ministere: str | None = None,
    departement: str | None = None, date_min: str | None = None,
    date_max: str | None = None, sort: str = "date_q_desc",
    conn: sqlite3.Connection = Depends(get_conn),
):
    rows = _export_questions(conn, locals())
    body = json.dumps(
        {"count": len(rows), "rows": rows}, ensure_ascii=False, indent=2,
    )
    return StreamingResponse(
        iter([body]),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=577deputes_questions.json"},
    )


# ---------------------------------------------------------------------
# JSON API — small, read-only, for programmatic clients
# ---------------------------------------------------------------------
@app.get("/api/questions")
def api_questions(
    q: str | None = None, type: str | None = None, statut: str | None = None,
    auteur_uid: str | None = None, groupe_uid: str | None = None,
    rubrique: str | None = None, ministere: str | None = None,
    departement: str | None = None, date_min: str | None = None,
    date_max: str | None = None, sort: str = "date_q_desc",
    page: int = 1, page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    res = Q.search_questions(
        conn, q_text=q, qtype=type, statut=statut, auteur_uid=auteur_uid,
        groupe_uid=groupe_uid, rubrique=rubrique, ministere=ministere,
        departement_code=departement, date_min=date_min, date_max=date_max,
        sort=sort, page=page, page_size=page_size,
    )
    return JSONResponse(res)


@app.get("/api/questions/{uid}")
def api_question(uid: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = Q.get_question(conn, uid)
    if not row:
        raise HTTPException(404, "not found")
    return JSONResponse(row)


@app.get("/api/deputes")
def api_deputies(
    q: str | None = None, groupe_uid: str | None = None,
    departement: str | None = None, sort: str = "nom_asc",
    page: int = 1, page_size: int = 100,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return JSONResponse(
        Q.list_deputies(
            conn, q_text=q, groupe_uid=groupe_uid, departement_code=departement,
            sort=sort, page=page, page_size=page_size,
        )
    )


@app.get("/api/deputes/{uid}")
def api_deputy(uid: str, conn: sqlite3.Connection = Depends(get_conn)):
    dep = Q.get_deputy(conn, uid)
    if not dep:
        raise HTTPException(404, "not found")
    return JSONResponse({
        "deputy": dep,
        "activity": Q.get_deputy_activity(conn, uid),
        "mandates": Q.get_deputy_mandates(conn, uid),
    })


@app.get("/api/health")
def api_health():
    """Health check minimaliste — ne divulgue pas l'état interne en public."""
    return {"ok": True}


@app.get("/.well-known/security.txt", response_class=PlainTextResponse, include_in_schema=False)
def security_txt():
    """RFC 9116 — point de contact pour disclosure responsable."""
    return (
        "Contact: mailto:martinez-eric@hotmail.fr\n"
        "Contact: https://577deputes.fr/mentions-legales\n"
        "Expires: 2027-12-31T23:59:59Z\n"
        "Preferred-Languages: fr, en\n"
        "Canonical: https://577deputes.fr/.well-known/security.txt\n"
    )


@app.get("/mentions-legales", response_class=HTMLResponse, include_in_schema=False)
def mentions_legales(request: Request):
    return templates.TemplateResponse(request, "mentions_legales.html", {})


# ---------------------------------------------------------------------
# Admin — statistiques de visite (HTTP Basic auth)
# ---------------------------------------------------------------------
import os as _os
import secrets as _secrets

from fastapi.security import HTTPBasic as _HTTPBasic, HTTPBasicCredentials as _HTTPBasicCredentials

_admin_security = _HTTPBasic()


def _verify_admin(credentials: _HTTPBasicCredentials = Depends(_admin_security)):
    expected = _os.environ.get("ANQP_ADMIN_PASSWORD", "")
    if not expected or not _secrets.compare_digest(credentials.password, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="577deputes admin"'},
        )
    return credentials.username


@app.get("/admin/stats", response_class=HTMLResponse, include_in_schema=False)
def admin_stats(request: Request, _user: str = Depends(_verify_admin)):
    from . import analytics
    summary = analytics.get_summary(days=30)
    return templates.TemplateResponse(
        request, "admin_stats.html", {"summary": summary},
    )


# =====================================================================
# PHASE 2 — Legislative routes : textes, amendements, scrutins.
# =====================================================================

# ----- TEXTES (dossiers législatifs) -----
@app.get("/textes", response_class=HTMLResponse)
def textes_list(
    request: Request,
    q: str | None = None,
    statut: str | None = None,
    initiateur: str | None = None,
    sort: str = "date_dernier_acte_desc",
    page: int = 1,
    page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = QL.list_dossiers(
        conn, q_text=q, statut=statut, initiateur_type=initiateur,
        sort=sort, page=page, page_size=page_size,
    )
    filters = {
        "q": q or "", "statut": statut or "", "initiateur": initiateur or "",
        "sort": sort, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "textes.html",
        {"result": result, "filters": filters},
    )


@app.get("/textes/{uid}", response_class=HTMLResponse)
def texte_detail(
    request: Request, uid: str, conn: sqlite3.Connection = Depends(get_conn)
):
    dossier = QL.get_dossier(conn, uid)
    if not dossier:
        raise HTTPException(404, f"Texte {uid} introuvable")
    actes = QL.get_dossier_actes(conn, uid)
    documents = QL.get_dossier_documents(conn, uid)
    summary = QL.dossier_amendements_summary(conn, uid)
    scrutins = QL.dossier_scrutins(conn, uid)
    from . import cluster_typology as CT
    cluster_summary = CT.texte_cluster_summary(conn, uid)
    return templates.TemplateResponse(
        request, "texte_detail.html",
        {"d": dossier, "actes": actes, "documents": documents,
         "summary": summary, "scrutins": scrutins,
         "cluster_summary": cluster_summary},
    )


@app.get("/textes/{uid}/amendements", response_class=HTMLResponse)
def texte_amendements(
    request: Request,
    uid: str,
    article: str | None = None,
    sort_filter: str | None = None,
    examen: str | None = None,
    groupe_uid: str | None = None,
    auteur_uid: str | None = None,
    q: str | None = None,
    sort: str = "article_asc",
    page: int = 1,
    page_size: int = 100,
    conn: sqlite3.Connection = Depends(get_conn),
):
    dossier = QL.get_dossier(conn, uid)
    if not dossier:
        raise HTTPException(404, f"Texte {uid} introuvable")
    articles = QL.list_articles_for_dossier(conn, uid)
    result = QL.search_amendements(
        conn, q_text=q, dossier_uid=uid, auteur_uid=auteur_uid, groupe_uid=groupe_uid,
        article_designation=article, sort_filter=sort_filter, examen_type=examen,
        sort=sort, page=page, page_size=page_size,
    )
    filters = {
        "q": q or "", "article": article or "", "sort_filter": sort_filter or "",
        "examen": examen or "", "groupe_uid": groupe_uid or "", "auteur_uid": auteur_uid or "",
        "sort": sort, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "texte_amendements.html",
        {"d": dossier, "articles": articles, "result": result, "filters": filters},
    )


# ----- AMENDEMENTS (recherche transverse) -----
@app.get("/amendements", response_class=HTMLResponse)
def amendements_list(
    request: Request,
    q: str | None = None,
    auteur_uid: str | None = None,
    groupe_uid: str | None = None,
    sort_filter: str | None = None,
    examen: str | None = None,
    sort: str = "date_depot_desc",
    page: int = 1,
    page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = QL.search_amendements(
        conn, q_text=q, auteur_uid=auteur_uid, groupe_uid=groupe_uid,
        sort_filter=sort_filter, examen_type=examen,
        sort=sort, page=page, page_size=page_size,
    )
    filters = {
        "q": q or "", "auteur_uid": auteur_uid or "", "groupe_uid": groupe_uid or "",
        "sort_filter": sort_filter or "", "examen": examen or "",
        "sort": sort, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "amendements.html",
        {"result": result, "filters": filters,
         "groups": Q.list_groups(conn)},
    )


@app.get("/amendements/{uid}", response_class=HTMLResponse)
def amendement_detail(
    request: Request, uid: str, conn: sqlite3.Connection = Depends(get_conn)
):
    a = QL.get_amendement(conn, uid)
    if not a:
        raise HTTPException(404, f"Amendement {uid} introuvable")
    cluster = QL.get_amendement_cluster(conn, uid)
    return templates.TemplateResponse(
        request, "amendement_detail.html", {"a": a, "cluster": cluster},
    )


# ----- SCRUTINS -----
@app.get("/scrutins", response_class=HTMLResponse)
def scrutins_list(
    request: Request,
    q: str | None = None,
    sort_filter: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
    sort: str = "date_desc",
    page: int = 1,
    page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = QL.list_scrutins(
        conn, q_text=q, sort_filter=sort_filter,
        date_min=date_min, date_max=date_max,
        sort=sort, page=page, page_size=page_size,
    )
    filters = {
        "q": q or "", "sort_filter": sort_filter or "",
        "date_min": date_min or "", "date_max": date_max or "",
        "sort": sort, "page_size": page_size,
    }
    return templates.TemplateResponse(
        request, "scrutins.html", {"result": result, "filters": filters},
    )


@app.get("/scrutins/{uid}", response_class=HTMLResponse)
def scrutin_detail(
    request: Request, uid: str, conn: sqlite3.Connection = Depends(get_conn)
):
    s = QL.get_scrutin(conn, uid)
    if not s:
        raise HTTPException(404, f"Scrutin {uid} introuvable")
    ventilation = QL.scrutin_ventilation(conn, uid)
    return templates.TemplateResponse(
        request, "scrutin_detail.html",
        {"s": s, "ventilation": ventilation},
    )


@app.get("/scrutins/{uid}/votants", response_class=HTMLResponse)
def scrutin_votants_page(
    request: Request, uid: str,
    position: str | None = None,
    groupe_uid: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    s = QL.get_scrutin(conn, uid)
    if not s:
        raise HTTPException(404, f"Scrutin {uid} introuvable")
    rows = QL.scrutin_votants(conn, uid, position=position, groupe_uid=groupe_uid)
    return templates.TemplateResponse(
        request, "scrutin_votants.html",
        {"s": s, "rows": rows, "position": position, "groupe_uid": groupe_uid},
    )


# ----- STATS Phase 2 -----
@app.get("/stats/textes", response_class=HTMLResponse)
def stats_textes(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_textes.html", {"data": QL.stats_textes_overview(conn)},
    )


@app.get("/stats/amendements", response_class=HTMLResponse)
def stats_amendements(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_amendements.html",
        {"data": QL.stats_amendements_overview(conn)},
    )


@app.get("/stats/scrutins", response_class=HTMLResponse)
def stats_scrutins(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "stats_scrutins.html",
        {"data": QL.stats_scrutins_overview(conn)},
    )


@app.get("/tops", response_class=HTMLResponse)
def tops_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "tops.html", {"data": QL.tops_overview(conn, limit=10)},
    )


@app.get("/tops/custom", response_class=HTMLResponse)
def tops_custom_page(
    request: Request,
    entity: str = "deputes",
    metric: str | None = None,
    direction: str = "desc",
    n: int = 25,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Constructeur de tops paramétrables."""
    from . import queries_custom_top as QCT

    # Catalogue exposé à la page (entités + métriques par catégorie)
    entities = list(QCT.ENTITY_LABELS.items())
    metrics_by_entity = {
        e: QCT.list_metrics_for_entity(e) for e in QCT.ENTITY_LABELS
    }

    # Forcer la cohérence entity↔metric : si la métrique n'existe pas, ou
    # n'appartient pas à l'entité demandée, on retombe sur la 1re métrique
    # disponible pour l'entité.
    if entity not in QCT.ENTITY_LABELS:
        entity = "deputes"
    if (
        not metric
        or metric not in QCT.METRICS
        or QCT.METRICS[metric].entity != entity
    ):
        if metrics_by_entity[entity]:
            metric = metrics_by_entity[entity][0].key
        else:
            metric = next(iter(QCT.METRICS))
            entity = QCT.METRICS[metric].entity

    metric_spec = QCT.METRICS[metric]
    # Récupère les filtres acceptés par CETTE métrique depuis la query string
    filters_in = {}
    for k in metric_spec.filter_map:
        v = request.query_params.get(k)
        if v:
            filters_in[k] = v

    result = QCT.run_top(
        conn, metric_key=metric,
        direction=direction, limit=n,
        filters=filters_in,
    )

    # Pour le sélecteur de groupe : on a besoin de la liste des groupes
    groupes = Q.list_groups(conn)

    # URL de partage (canonique)
    share_qs = [f"entity={entity}", f"metric={metric}",
                f"direction={direction}", f"n={result['limit']}"]
    for k, v in result["filters_applied"].items():
        # Pour le filtre ministere on a stocké %v% — réafficher v sans wildcards
        if k == "ministere":
            v = v.strip("%")
        share_qs.append(f"{k}={v}")
    share_url = "/tops/custom?" + "&".join(share_qs)

    suggestions = QCT.random_suggestions(n=4, exclude_metric_key=metric)

    return templates.TemplateResponse(
        request, "tops_custom.html",
        {
            "entities": entities,
            "metrics_by_entity": metrics_by_entity,
            "selected_entity": entity,
            "selected_metric": metric,
            "metric_spec": metric_spec,
            "filters_for_metric": QCT.list_filters_for_metric(metric_spec),
            "filter_types": QCT.FILTER_TYPES,
            "result": result,
            "groupes": groupes,
            "share_url": share_url,
            "suggestions": suggestions,
            "all_metrics": QCT.METRICS,
        },
    )


@app.get("/api/tops/custom")
def api_tops_custom(
    metric: str,
    direction: str = "desc",
    n: int = 25,
    request: Request = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Version JSON du générateur de tops (mêmes paramètres que la page HTML)."""
    from . import queries_custom_top as QCT

    if metric not in QCT.METRICS:
        raise HTTPException(404, detail="Métrique inconnue")

    spec = QCT.METRICS[metric]
    filters_in = {
        k: request.query_params.get(k)
        for k in spec.filter_map
        if request.query_params.get(k)
    }
    result = QCT.run_top(
        conn, metric_key=metric, direction=direction, limit=n, filters=filters_in,
    )
    return JSONResponse({
        "metric": {
            "key": spec.key, "entity": spec.entity, "label": spec.label,
            "value_label": spec.value_label, "is_percentage": spec.is_percentage,
        },
        "direction": result["direction"],
        "limit": result["limit"],
        "filters_applied": result["filters_applied"],
        "rows": result["rows"],
    })


@app.get("/carte", response_class=HTMLResponse)
def carte_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from .cartography import render_map_svg, map_legend, dom_circos
    svg = render_map_svg(conn)
    legend = map_legend(conn)
    dom = dom_circos(conn)
    return templates.TemplateResponse(
        request, "carte.html",
        {"map_svg": svg, "legend": legend, "dom": dom},
    )


@app.get("/a-propos", response_class=HTMLResponse)
def about_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    overview = Q.db_overview(conn)
    leg_overview = QL.home_legislative_overview(conn)
    return templates.TemplateResponse(
        request, "a_propos.html",
        {"overview": overview, "legislative": leg_overview,
         "refresh_state": {
             "enabled": _refresh_enabled(),
             "next_run_in_s": _next_refresh_in(),
         }},
    )


@app.get("/clusters", response_class=HTMLResponse)
def clusters_page(
    request: Request, page: int = 1, type: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    from . import cluster_typology as CT
    if type not in CT.CLUSTER_TYPES:
        type = None
    return templates.TemplateResponse(
        request, "clusters.html",
        {
            "result": QL.list_amendement_clusters_by_dossier(
                conn, page=page, type_filter=type,
            ),
            "overview": CT.stats_overview(conn),
            "selected_type": type,
            "types_catalog": CT.CLUSTER_TYPES,
        },
    )


@app.get("/stats/clusters", response_class=HTMLResponse)
def stats_clusters_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn),
):
    from . import cluster_typology as CT
    return templates.TemplateResponse(
        request, "stats_clusters.html",
        {
            "overview": CT.stats_overview(conn),
            "top_textes": CT.top_textes_by_clusters(conn, limit=15),
            "top_deputes": CT.top_deputes_in_clusters(conn, limit=15),
        },
    )


@app.get("/recherche", response_class=HTMLResponse)
def recherche_page(
    request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    res = QL.unified_search(conn, q) if q else None
    return templates.TemplateResponse(
        request, "recherche.html", {"q": q, "res": res},
    )


@app.get("/comparer", response_class=HTMLResponse)
def comparer_page(
    request: Request,
    a: str | None = None,
    b: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    metrics_a = Q.deputy_compare_metrics(conn, a) if a else None
    metrics_b = Q.deputy_compare_metrics(conn, b) if b else None
    # Lookup helpers for the form (typeahead).
    return templates.TemplateResponse(
        request, "comparer.html",
        {
            "a": a, "b": b,
            "metrics_a": metrics_a, "metrics_b": metrics_b,
            "deputies": Q.list_deputies(conn, page=1, page_size=625, sort="nom_asc")["rows"],
        },
    )


@app.get("/dissidents", response_class=HTMLResponse)
def dissidents_page(
    request: Request,
    min_votes: int = 100,
    groupe_uid: str | None = None,
    sort: str = "dissidence_desc",
    page: int = 1,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = QL.dissidents_list(
        conn, min_votes=min_votes, groupe_uid=groupe_uid, sort=sort, page=page,
    )
    return templates.TemplateResponse(
        request, "dissidents.html",
        {"result": result, "groups": Q.list_groups(conn)},
    )


_COALITIONS_TABS = {"blocs", "matrice", "sujets", "carte"}
_ANALYSES_TABS = {"templates", "absenteisme", "fantomes"}


@app.get("/analyses", response_class=HTMLResponse)
def analyses_page(
    request: Request,
    tab: str = "templates",
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Page Analyses — 3 outils de detection : templates ministeriels,
    absenteisme strategique, amendements fantomes."""
    if tab not in _ANALYSES_TABS:
        tab = "templates"

    data = {}
    if tab == "templates":
        data["templates"] = QL.analyses_minister_templates(conn, top_n=15)
    elif tab == "absenteisme":
        data["absence"] = QL.analyses_strategic_absence(conn, top_n=30, min_clivants=30)
    elif tab == "fantomes":
        data["phantom"] = QL.analyses_phantom_amendments(conn, top_n=30, min_amdts=50)

    return templates.TemplateResponse(
        request, "analyses.html",
        {"tab": tab, "data": data},
    )


@app.get("/coalitions", response_class=HTMLResponse)
def coalitions_page(
    request: Request,
    tab: str = "blocs",
    at: str | None = None,
    abl: str | None = None,
    abr: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Page Coalitions — 4 onglets analysant les blocs et alliances de vote."""
    if tab not in _COALITIONS_TABS:
        tab = "blocs"

    data = {}
    if tab == "blocs":
        data["overview"] = QL.coalitions_overview(conn)
    elif tab == "matrice":
        data["matrix_data"] = QL.coalitions_matrix(conn)
    elif tab == "sujets":
        data["topics"] = QL.coalitions_by_topic(conn, top_n=10)
    elif tab == "carte":
        data["ternary"] = QL.coalitions_ternary(
            conn,
            anchor_top_uid=at,
            anchor_bl_uid=abl,
            anchor_br_uid=abr,
        )

    return templates.TemplateResponse(
        request, "coalitions.html",
        {"tab": tab, "data": data},
    )


# ----- API JSON Phase 2 -----
@app.get("/api/textes")
def api_textes(
    q: str | None = None, statut: str | None = None,
    initiateur: str | None = None, sort: str = "date_dernier_acte_desc",
    page: int = 1, page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return JSONResponse(QL.list_dossiers(
        conn, q_text=q, statut=statut, initiateur_type=initiateur,
        sort=sort, page=page, page_size=page_size,
    ))


@app.get("/api/textes/{uid}")
def api_texte(uid: str, conn: sqlite3.Connection = Depends(get_conn)):
    d = QL.get_dossier(conn, uid)
    if not d:
        raise HTTPException(404, "not found")
    return JSONResponse({
        "dossier": d,
        "actes": QL.get_dossier_actes(conn, uid),
        "documents": QL.get_dossier_documents(conn, uid),
        "summary": QL.dossier_amendements_summary(conn, uid),
    })


@app.get("/api/amendements")
def api_amendements(
    q: str | None = None, dossier_uid: str | None = None,
    auteur_uid: str | None = None, groupe_uid: str | None = None,
    sort_filter: str | None = None, examen: str | None = None,
    sort: str = "date_depot_desc", page: int = 1, page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return JSONResponse(QL.search_amendements(
        conn, q_text=q, dossier_uid=dossier_uid, auteur_uid=auteur_uid,
        groupe_uid=groupe_uid, sort_filter=sort_filter, examen_type=examen,
        sort=sort, page=page, page_size=page_size,
    ))


@app.get("/api/amendements/{uid}")
def api_amendement(uid: str, conn: sqlite3.Connection = Depends(get_conn)):
    a = QL.get_amendement(conn, uid)
    if not a:
        raise HTTPException(404, "not found")
    return JSONResponse(a)


@app.get("/api/scrutins")
def api_scrutins(
    q: str | None = None, sort_filter: str | None = None,
    date_min: str | None = None, date_max: str | None = None,
    sort: str = "date_desc", page: int = 1, page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return JSONResponse(QL.list_scrutins(
        conn, q_text=q, sort_filter=sort_filter,
        date_min=date_min, date_max=date_max,
        sort=sort, page=page, page_size=page_size,
    ))


@app.get("/api/scrutins/{uid}")
def api_scrutin(uid: str, conn: sqlite3.Connection = Depends(get_conn)):
    s = QL.get_scrutin(conn, uid)
    if not s:
        raise HTTPException(404, "not found")
    return JSONResponse({
        "scrutin": s,
        "ventilation": QL.scrutin_ventilation(conn, uid),
    })
