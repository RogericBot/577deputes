"""Mesure d'audience légère, sans cookie et sans tracker.

Storage : SQLite séparé de la base principale (qui est ouverte en lecture seule).
Aucun service tiers, aucun script JS de pistage, **AUCUN COOKIE**.

Visiteurs uniques sans cookie : pour chaque requête on calcule un hash éphémère
de (IP + User-Agent + sel-aléatoire-du-jour). Ce hash sert uniquement à
dédoublonner les visiteurs *au sein de la journée* ; il n'est jamais conservé
au-delà de ~2 jours et ne permet aucun suivi d'un jour à l'autre (le sel change
chaque jour). L'IP elle-même n'est jamais écrite dans cette base : elle ne sert
qu'au calcul du hash, en mémoire, le temps de la requête. C'est l'approche
« cookieless » de Plausible / Umami / GoatCounter, exemptée de bandeau de
consentement (CNIL).

Données conservées : uniquement des agrégats par jour — pages vues, uniques,
catégorie de navigateur/bot, URL normalisée, source de provenance (referrer),
requêtes de recherche du site, pays (si une base GeoLite2 est présente).
Rétention : ~25 mois pour les agrégats, ~2 jours pour les hashs éphémères.
Purge automatique au changement de jour.
"""
from __future__ import annotations

import hashlib
import re as _re
import secrets
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from ..config import settings


# Rétention (en jours)
RETENTION_DAYS_AGG = 760    # ~25 mois pour les agrégats journaliers (limite CNIL)
RETENTION_DAYS_HASH = 2     # hashs éphémères : on ne garde qu'aujourd'hui + 1


def db_path() -> Path:
    return Path(settings.data_dir) / "analytics.db"


# ---------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------
def init() -> None:
    """Crée les tables si elles n'existent pas. Appelé au démarrage."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                day                  TEXT PRIMARY KEY,
                page_views           INTEGER NOT NULL DEFAULT 0,
                unique_visitors      INTEGER NOT NULL DEFAULT 0,
                bot_page_views       INTEGER NOT NULL DEFAULT 0,
                bot_unique_visitors  INTEGER NOT NULL DEFAULT 0
            );

            -- Hash éphémère du visiteur, pour dédoublonner les uniques du jour.
            -- Purgé après ~2 jours ; le sel change chaque jour (cf. daily_salt).
            CREATE TABLE IF NOT EXISTS seen_today (
                visitor_hash TEXT NOT NULL,
                day          TEXT NOT NULL,
                PRIMARY KEY (visitor_hash, day)
            );

            -- Sel aléatoire du jour, partagé entre les workers, jamais réutilisé.
            CREATE TABLE IF NOT EXISTS daily_salt (
                day  TEXT PRIMARY KEY,
                salt TEXT NOT NULL
            );

            -- Compteur agrégé par catégorie de User-Agent (navigateur / type de bot).
            CREATE TABLE IF NOT EXISTS daily_categories (
                day TEXT NOT NULL, category TEXT NOT NULL,
                page_views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, category)
            );

            -- Compteur agrégé par URL normalisée (UID -> [uid], sans query string).
            CREATE TABLE IF NOT EXISTS daily_paths (
                day TEXT NOT NULL, path TEXT NOT NULL,
                page_views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, path)
            );

            -- Compteur agrégé par source de provenance (referrer catégorisé :
            -- "(accès direct)", "Recherche : Google", "Réseau : Discord", domaine...).
            CREATE TABLE IF NOT EXISTS daily_referrers (
                day TEXT NOT NULL, source TEXT NOT NULL,
                page_views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, source)
            );

            -- Compteur agrégé des requêtes tapées dans la barre de recherche du site.
            -- Agrégé par requête (minuscules, espaces normalisés, tronqué), jamais
            -- lié à un visiteur ni horodaté individuellement.
            CREATE TABLE IF NOT EXISTS daily_searches (
                day TEXT NOT NULL, query TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, query)
            );

            -- Compteur agrégé par pays (code ISO). Rempli seulement si une base
            -- GeoLite2-Country.mmdb est présente dans data/ et geoip2 installé.
            -- L'IP n'est jamais stockée ; elle sert uniquement au lookup en mémoire.
            CREATE TABLE IF NOT EXISTS daily_countries (
                day TEXT NOT NULL, country TEXT NOT NULL,
                page_views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, country)
            );

            CREATE INDEX IF NOT EXISTS idx_seen_today_day ON seen_today(day);
            CREATE INDEX IF NOT EXISTS idx_categories_day ON daily_categories(day);
            CREATE INDEX IF NOT EXISTS idx_paths_day      ON daily_paths(day);
            CREATE INDEX IF NOT EXISTS idx_referrers_day  ON daily_referrers(day);
            CREATE INDEX IF NOT EXISTS idx_searches_day   ON daily_searches(day);
            CREATE INDEX IF NOT EXISTS idx_countries_day  ON daily_countries(day);
            """
        )
        # Migrations : colonnes manquantes (anciennes bases).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_stats)")}
        if "bot_page_views" not in cols:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN bot_page_views INTEGER NOT NULL DEFAULT 0")
        if "bot_unique_visitors" not in cols:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN bot_unique_visitors INTEGER NOT NULL DEFAULT 0")
        # On abandonne le suivi par cookie : plus de table `visitors` (mini-profils).
        conn.execute("DROP TABLE IF EXISTS visitors")
        conn.commit()
    finally:
        conn.close()
    purge_old()


# ---------------------------------------------------------------------
# Catégorisation User-Agent (RGPD-friendly : on ne stocke jamais l'UA brut)
# ---------------------------------------------------------------------
def categorize_ua(ua: str) -> tuple[str, bool]:
    """Retourne (catégorie_lisible, is_bot). Ne stocke jamais l'UA brut."""
    if not ua:
        return ("Bot — UA vide", True)

    u = ua.lower()

    # === Bots IA / assistants ===
    if any(p in u for p in ("claude", "anthropic")):
        return ("🤖 Claude / Anthropic", True)
    if any(p in u for p in ("gpt", "chatgpt", "openai")):
        return ("🤖 GPT / OpenAI", True)
    if "perplexity" in u:
        return ("🤖 Perplexity", True)
    if any(p in u for p in ("mistral", "gemini", "bard", "copilot")):
        return ("🤖 Autre IA", True)

    # === Bots moteurs de recherche ===
    if any(p in u for p in (
        "googlebot", "googleother", "google-other", "googleweblight",
        "feedfetcher-google", "adsbot-google", "mediapartners-google",
        "google-inspectiontool", "google-extended", "google-safety",
    )):
        return ("🤖 Googlebot", True)
    if "bingbot" in u or "msnbot" in u:
        return ("🤖 Bingbot", True)
    if "yandex" in u:
        return ("🤖 Yandex", True)
    if "baidu" in u or "baiduspider" in u:
        return ("🤖 Baidu", True)
    if "duckduckbot" in u or "duckduckgo" in u:
        return ("🤖 DuckDuckGo", True)
    if "applebot" in u:
        return ("🤖 Applebot", True)

    # === Réseaux sociaux ===
    if "facebookexternalhit" in u or "facebot" in u:
        return ("🔗 Facebook bot", True)
    if "twitterbot" in u:
        return ("🔗 Twitter / X bot", True)
    if "linkedinbot" in u:
        return ("🔗 LinkedIn bot", True)
    if "slackbot" in u:
        return ("🔗 Slack bot", True)
    if "discordbot" in u:
        return ("🔗 Discord bot", True)
    if "telegrambot" in u:
        return ("🔗 Telegram bot", True)
    if "whatsapp" in u:
        return ("🔗 WhatsApp", True)

    # === Outils SEO / monitoring ===
    if "ahrefs" in u or "semrush" in u or "mj12" in u or "dotbot" in u or "petalbot" in u:
        return ("🤖 Crawler SEO", True)
    if "uptimerobot" in u or "pingdom" in u:
        return ("🤖 Monitoring", True)

    # === Outils techniques ===
    if any(p in u for p in ("wget", "curl", "python-requests", "go-http-client",
                            "java/", "okhttp", "httpie", "axios", "node-fetch",
                            "lighthouse", "pagespeed")):
        return ("🤖 Outil technique", True)

    # === Headless browsers ===
    if any(p in u for p in ("headless", "phantomjs", "puppeteer", "playwright")):
        return ("🤖 Headless browser", True)

    # === Bots génériques ===
    if any(p in u for p in ("bot", "spider", "crawl", "fetcher", "scraper", "scan")):
        return ("🤖 Bot / crawler autre", True)

    # Heuristique forte : "(compatible;" sans matcher MSIE = bot déguisé.
    if ("(compatible;" in u or "(compatible ;" in u) and "msie" not in u:
        return ("🤖 Bot déguisé (compatible;)", True)

    # === Navigateurs réels — règle : doit contenir Mozilla ===
    if "mozilla" not in u:
        return ("🤖 UA suspect (sans Mozilla)", True)

    is_mobile = "mobile" in u or "iphone" in u or "ipad" in u or "android" in u
    suffix = " mobile" if is_mobile else " desktop"

    if "edg/" in u or "edge/" in u:
        return (f"🌐 Edge{suffix}", False)
    if "opr/" in u or "opera" in u:
        return (f"🌐 Opera{suffix}", False)
    if "firefox" in u:
        return (f"🌐 Firefox{suffix}", False)
    if "chrome" in u:
        return (f"🌐 Chrome{suffix}", False)
    if "safari" in u:
        return (f"🌐 Safari{suffix}", False)

    return (f"🌐 Autre navigateur{suffix}", False)


# ---------------------------------------------------------------------
# Normalisation de path (RGPD : pas de query string conservée)
# ---------------------------------------------------------------------
_UID_PATTERN_PATH = _re.compile(
    r"/(deputes|questions|amendements|scrutins|textes|photo)/[A-Za-z0-9]+"
)


def normalize_path(path: str) -> str:
    """Remplace les UID dans les paths par [uid] et supprime la query string."""
    if not path:
        return "/"
    if "?" in path:
        path = path.split("?", 1)[0]
    path = _UID_PATTERN_PATH.sub(r"/\1/[uid]", path)
    return path[:120]


# ---------------------------------------------------------------------
# Catégorisation du referrer (on conserve la SOURCE, jamais l'URL complète)
# ---------------------------------------------------------------------
_OUR_HOSTS = {"577deputes.fr", "www.577deputes.fr"}


def categorize_referrer(referer: str) -> str:
    if not referer:
        return "(accès direct)"
    try:
        host = (urlparse(referer).hostname or "").lower()
    except Exception:
        return "(referer invalide)"
    if not host:
        return "(accès direct)"
    if host.startswith("www."):
        host = host[4:]
    if host in _OUR_HOSTS:
        return "(navigation interne)"

    # Moteurs de recherche
    if host == "google.com" or host.startswith("google.") or host.endswith(".google.com"):
        return "Recherche : Google"
    if host.startswith("bing."):
        return "Recherche : Bing"
    if host == "duckduckgo.com" or host == "lite.duckduckgo.com":
        return "Recherche : DuckDuckGo"
    if host == "qwant.com":
        return "Recherche : Qwant"
    if host == "ecosia.org":
        return "Recherche : Ecosia"
    if host == "search.brave.com":
        return "Recherche : Brave"
    if host in ("yandex.com", "yandex.ru"):
        return "Recherche : Yandex"
    if host == "startpage.com":
        return "Recherche : Startpage"
    if host == "search.marginalia.nu":
        return "Recherche : Marginalia"

    # Réseaux sociaux / messageries
    if host in ("facebook.com", "m.facebook.com", "l.facebook.com", "lm.facebook.com"):
        return "Réseau : Facebook"
    if host in ("twitter.com", "x.com", "t.co"):
        return "Réseau : X / Twitter"
    if host in ("linkedin.com", "lnkd.in"):
        return "Réseau : LinkedIn"
    if host in ("reddit.com", "old.reddit.com", "new.reddit.com", "out.reddit.com"):
        return "Réseau : Reddit"
    if host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"):
        return "Réseau : Discord"
    if host in ("instagram.com", "l.instagram.com"):
        return "Réseau : Instagram"
    if host in ("t.me", "telegram.me", "telegram.org"):
        return "Réseau : Telegram"
    if host == "wa.me" or host.endswith("whatsapp.com"):
        return "Réseau : WhatsApp"
    if host == "bsky.app":
        return "Réseau : Bluesky"
    if host == "news.ycombinator.com":
        return "Réseau : Hacker News"
    if "mastodon" in host or host in ("piaille.fr", "framapiaf.org", "mamot.fr"):
        return "Réseau : Mastodon"

    # Sinon : le domaine seul (pas le path, pour ne pas conserver d'URL sensible)
    return host[:60]


# ---------------------------------------------------------------------
# Normalisation des requêtes de recherche du site
# ---------------------------------------------------------------------
def normalize_search_query(q: str) -> str:
    if not q:
        return ""
    q = _re.sub(r"\s+", " ", q.strip().lower())
    return q[:80]


# ---------------------------------------------------------------------
# GeoIP — optionnel : inerte tant qu'aucune base GeoLite2 n'est présente.
# Activation : `pip install geoip2` + déposer GeoLite2-Country.mmdb dans data/.
# ---------------------------------------------------------------------
_GEOIP_READER = None
_GEOIP_TRIED = False


def country_from_ip(ip: str) -> str:
    """Code pays ISO ('FR', 'BE'...) ou '??' si indisponible. IP utilisée
    seulement en mémoire ici, jamais écrite."""
    global _GEOIP_READER, _GEOIP_TRIED
    if not _GEOIP_TRIED:
        _GEOIP_TRIED = True
        try:
            import geoip2.database  # type: ignore
            mmdb = Path(settings.data_dir) / "GeoLite2-Country.mmdb"
            if mmdb.exists():
                _GEOIP_READER = geoip2.database.Reader(str(mmdb))
        except Exception:
            _GEOIP_READER = None
    if _GEOIP_READER is None or not ip:
        return "??"
    try:
        return _GEOIP_READER.country(ip).country.iso_code or "??"
    except Exception:
        return "??"


# ---------------------------------------------------------------------
# Sel du jour + hash éphémère
# ---------------------------------------------------------------------
_salt_cache: dict[str, str] = {}  # {day: salt} — un seul jour à la fois


def _daily_salt(conn: sqlite3.Connection, day: str) -> str:
    s = _salt_cache.get(day)
    if s:
        return s
    conn.execute(
        "INSERT OR IGNORE INTO daily_salt (day, salt) VALUES (?, ?)",
        (day, secrets.token_hex(16)),
    )
    row = conn.execute("SELECT salt FROM daily_salt WHERE day = ?", (day,)).fetchone()
    s = row[0] if row else secrets.token_hex(16)
    _salt_cache.clear()
    _salt_cache[day] = s
    return s


def _visitor_hash(salt: str, ip: str, ua: str) -> str:
    raw = f"{salt}|{ip or '-'}|{ua or '-'}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------
# Connexion + purge
# ---------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_last_purge_day = ""


def purge_old() -> None:
    """Supprime les vieux agrégats (> RETENTION_DAYS_AGG) et les hashs éphémères
    (> RETENTION_DAYS_HASH). Idempotent."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for tbl in ("daily_stats", "daily_categories", "daily_paths",
                    "daily_referrers", "daily_searches", "daily_countries"):
            conn.execute(f"DELETE FROM {tbl} WHERE day < date('now', ?)",
                         (f"-{RETENTION_DAYS_AGG} days",))
        for tbl in ("seen_today", "daily_salt"):
            conn.execute(f"DELETE FROM {tbl} WHERE day < date('now', ?)",
                         (f"-{RETENTION_DAYS_HASH} days",))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Enregistrement d'une page vue
# ---------------------------------------------------------------------
def record_visit(ip: str = "", ua: str = "", raw_path: str = "/",
                 referer: str = "", search_query: str = "") -> None:
    """Enregistre une page vue (agrégats uniquement, sans cookie, sans stocker l'IP).

    `ip` n'est utilisée que pour calculer le hash éphémère du jour et le lookup
    GeoIP éventuel ; elle n'est jamais écrite dans la base.
    """
    global _last_purge_day
    today = date.today().isoformat()

    category, is_bot = categorize_ua(ua)
    path = normalize_path(raw_path)
    ref_source = categorize_referrer(referer)
    sq = normalize_search_query(search_query)
    country = country_from_ip(ip)

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        salt = _daily_salt(conn, today)
        vh = _visitor_hash(salt, ip, ua)

        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_today (visitor_hash, day) VALUES (?, ?)",
            (vh, today),
        )
        is_new_today = cur.rowcount > 0

        if is_bot:
            conn.execute(
                "INSERT INTO daily_stats (day, bot_page_views, bot_unique_visitors) "
                "VALUES (?, 1, ?) ON CONFLICT(day) DO UPDATE SET "
                "bot_page_views = bot_page_views + 1, "
                "bot_unique_visitors = bot_unique_visitors + ?",
                (today, 1 if is_new_today else 0, 1 if is_new_today else 0),
            )
        else:
            conn.execute(
                "INSERT INTO daily_stats (day, page_views, unique_visitors) "
                "VALUES (?, 1, ?) ON CONFLICT(day) DO UPDATE SET "
                "page_views = page_views + 1, "
                "unique_visitors = unique_visitors + ?",
                (today, 1 if is_new_today else 0, 1 if is_new_today else 0),
            )

        conn.execute(
            "INSERT INTO daily_categories (day, category, page_views) VALUES (?, ?, 1) "
            "ON CONFLICT(day, category) DO UPDATE SET page_views = page_views + 1",
            (today, category),
        )
        conn.execute(
            "INSERT INTO daily_paths (day, path, page_views) VALUES (?, ?, 1) "
            "ON CONFLICT(day, path) DO UPDATE SET page_views = page_views + 1",
            (today, path),
        )
        conn.execute(
            "INSERT INTO daily_referrers (day, source, page_views) VALUES (?, ?, 1) "
            "ON CONFLICT(day, source) DO UPDATE SET page_views = page_views + 1",
            (today, ref_source),
        )
        if sq:
            conn.execute(
                "INSERT INTO daily_searches (day, query, count) VALUES (?, ?, 1) "
                "ON CONFLICT(day, query) DO UPDATE SET count = count + 1",
                (today, sq),
            )
        if country != "??":
            conn.execute(
                "INSERT INTO daily_countries (day, country, page_views) VALUES (?, ?, 1) "
                "ON CONFLICT(day, country) DO UPDATE SET page_views = page_views + 1",
                (today, country),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    # Purge opportuniste au changement de jour (pas besoin de cron).
    if today != _last_purge_day:
        _last_purge_day = today
        try:
            purge_old()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Synthèse pour la page admin
# ---------------------------------------------------------------------
def get_summary(days: int = 30) -> dict:
    conn = _connect()
    try:
        total_pv, total_bot_pv, total_uniq_days, total_bot_uniq_days = conn.execute(
            "SELECT IFNULL(SUM(page_views),0), IFNULL(SUM(bot_page_views),0), "
            "IFNULL(SUM(unique_visitors),0), IFNULL(SUM(bot_unique_visitors),0) "
            "FROM daily_stats"
        ).fetchone()
        avg_pages = round(total_pv / total_uniq_days, 2) if total_uniq_days else 0

        today = date.today().isoformat()
        today_row = conn.execute(
            "SELECT page_views, unique_visitors, bot_page_views, bot_unique_visitors "
            "FROM daily_stats WHERE day = ?", (today,)
        ).fetchone() or (0, 0, 0, 0)

        recent = conn.execute(
            "SELECT day, page_views, unique_visitors, bot_page_views, bot_unique_visitors "
            "FROM daily_stats ORDER BY day DESC LIMIT ?", (days,)
        ).fetchall()

        def top(table: str, label_col: str, val_col: str, n: int = 30):
            return conn.execute(
                f"SELECT {label_col}, SUM({val_col}) AS v FROM {table} "
                f"WHERE day >= date('now', ?) GROUP BY {label_col} ORDER BY v DESC LIMIT ?",
                (f"-{days} days", n),
            ).fetchall()

        top_categories = top("daily_categories", "category", "page_views")
        top_paths = top("daily_paths", "path", "page_views")
        top_referrers = top("daily_referrers", "source", "page_views")
        top_searches = top("daily_searches", "query", "count", 40)
        top_countries = top("daily_countries", "country", "page_views", 40)

        return {
            "total_page_views": total_pv,
            # = somme des uniques journaliers (un visiteur qui revient un autre
            #   jour est recompté ; c'est l'agrégat honnête sans identifiant persistant)
            "total_unique_visitors": total_uniq_days,
            "total_bot_page_views": total_bot_pv,
            "total_bot_visitors": total_bot_uniq_days,
            # ~ pages vues par visiteur-jour
            "avg_visits_per_visitor": avg_pages,
            "today_page_views": today_row[0],
            "today_unique_visitors": today_row[1],
            "today_bot_page_views": today_row[2],
            "today_bot_unique_visitors": today_row[3],
            "daily": [
                {"day": r[0], "page_views": r[1], "unique_visitors": r[2],
                 "bot_page_views": r[3], "bot_unique_visitors": r[4]}
                for r in recent
            ],
            "top_categories": [{"category": r[0], "page_views": r[1]} for r in top_categories],
            "top_paths": [{"path": r[0], "page_views": r[1]} for r in top_paths],
            "top_referrers": [{"source": r[0], "page_views": r[1]} for r in top_referrers],
            "top_searches": [{"query": r[0], "count": r[1]} for r in top_searches],
            "top_countries": [{"country": r[0], "page_views": r[1]} for r in top_countries],
        }
    finally:
        conn.close()
