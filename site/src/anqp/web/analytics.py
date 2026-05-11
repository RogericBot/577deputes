"""Analytics légères : compteur de pages vues + visiteurs uniques.

Storage : SQLite séparé de la DB principale (qui est ouverte en read-only).
Aucun service tiers, aucun tracker JS, juste un cookie pour identifier
les visiteurs revenant (id aléatoire, jamais corrélé à une identité).
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..config import settings


def db_path() -> Path:
    return Path(settings.data_dir) / "analytics.db"


def init() -> None:
    """Crée les tables si elles n'existent pas. Appelé au démarrage."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        # WAL : permet les lectures concurrentes pendant les écritures.
        # busy_timeout : attend jusqu'à 5s avant de lever SQLITE_BUSY.
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

            CREATE TABLE IF NOT EXISTS seen_today (
                visitor_id  TEXT NOT NULL,
                day         TEXT NOT NULL,
                PRIMARY KEY (visitor_id, day)
            );

            CREATE TABLE IF NOT EXISTS visitors (
                visitor_id  TEXT PRIMARY KEY,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                visits      INTEGER NOT NULL DEFAULT 1,
                is_bot      INTEGER NOT NULL DEFAULT 0
            );

            -- Compteur agrégé par catégorie de User-Agent
            -- (= navigateur ou type de bot ; aucune donnée perso stockée)
            CREATE TABLE IF NOT EXISTS daily_categories (
                day         TEXT NOT NULL,
                category    TEXT NOT NULL,
                page_views  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, category)
            );

            -- Compteur agrégé par path (URL avec UID normalisés)
            CREATE TABLE IF NOT EXISTS daily_paths (
                day         TEXT NOT NULL,
                path        TEXT NOT NULL,
                page_views  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, path)
            );

            CREATE INDEX IF NOT EXISTS idx_seen_today_day ON seen_today(day);
            CREATE INDEX IF NOT EXISTS idx_categories_day ON daily_categories(day);
            CREATE INDEX IF NOT EXISTS idx_paths_day ON daily_paths(day);
            """
        )
        # Migrations en place : ajoute les colonnes si elles manquent.
        cols_daily = {r[1] for r in conn.execute("PRAGMA table_info(daily_stats)")}
        if "bot_page_views" not in cols_daily:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN bot_page_views INTEGER NOT NULL DEFAULT 0")
        if "bot_unique_visitors" not in cols_daily:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN bot_unique_visitors INTEGER NOT NULL DEFAULT 0")

        cols_visitors = {r[1] for r in conn.execute("PRAGMA table_info(visitors)")}
        if "is_bot" not in cols_visitors:
            conn.execute("ALTER TABLE visitors ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Catégorisation User-Agent (RGPD-friendly : on ne stocke pas l'UA brut)
# ---------------------------------------------------------------------
def categorize_ua(ua: str) -> tuple[str, bool]:
    """Retourne (catégorie_lisible, is_bot).

    Ne stocke jamais l'UA brut, seulement une catégorie agrégée.
    """
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

    # Heuristique forte : "(compatible;" sans matcher MSIE = bot
    # qui se déguise en navigateur (genre "Mozilla/5.0 ... (compatible; XYZBot/1.0)").
    # Les vrais navigateurs récents ne mettent jamais ça.
    if ("(compatible;" in u or "(compatible ;" in u) and "msie" not in u:
        return ("🤖 Bot déguisé (compatible;)", True)

    # === Navigateurs réels — règle : doit contenir Mozilla ===
    if "mozilla" not in u:
        return ("🤖 UA suspect (sans Mozilla)", True)

    # Mobile
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
import re as _re

_UID_PATTERN_PATH = _re.compile(
    r"/(deputes|questions|amendements|scrutins|textes|photo)/[A-Za-z0-9]+"
)


def normalize_path(path: str) -> str:
    """Remplace les UID dans les paths par [uid] pour agréger.
    Ex: /deputes/PA12345 → /deputes/[uid]
    """
    if not path:
        return "/"
    # Supprime la query string
    if "?" in path:
        path = path.split("?", 1)[0]
    # Normalise les UIDs
    path = _UID_PATTERN_PATH.sub(r"/\1/[uid]", path)
    # Cap la longueur pour éviter les abus
    return path[:120]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def record_visit(
    visitor_id: str | None,
    is_bot: bool = False,
    category: str = "Autre",
    path: str = "/",
) -> str:
    """Enregistre une page vue. Retourne l'ID visiteur (nouveau si absent).

    is_bot=True → comptabilisé dans les colonnes bot_* (séparément).
    Une fois flaggué bot, un visiteur reste bot (sticky via MAX()).

    category : libellé de catégorie de UA (Chrome desktop, Bot Googlebot...).
    path : URL normalisée (avec [uid] à la place des identifiants).

    Toutes les écritures sont dans une transaction BEGIN IMMEDIATE
    pour éviter les races entre workers uvicorn.
    """
    today = date.today().isoformat()
    now = datetime.utcnow().isoformat(timespec="seconds")

    if not visitor_id:
        visitor_id = secrets.token_urlsafe(12)

    bot_flag = 1 if is_bot else 0

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Suivi tout-temps du visiteur (sticky bot flag)
        conn.execute(
            "INSERT INTO visitors (visitor_id, first_seen, last_seen, visits, is_bot) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(visitor_id) DO UPDATE SET "
            "  last_seen = excluded.last_seen, "
            "  visits    = visits + 1, "
            "  is_bot    = MAX(is_bot, excluded.is_bot)",
            (visitor_id, now, now, bot_flag),
        )

        # Marquer le visiteur comme vu aujourd'hui (= unique du jour)
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_today (visitor_id, day) VALUES (?, ?)",
            (visitor_id, today),
        )
        is_new_today = cur.rowcount > 0

        # Compteurs du jour, séparés humains / bots
        if is_bot:
            conn.execute(
                "INSERT INTO daily_stats (day, bot_page_views, bot_unique_visitors) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "  bot_page_views      = bot_page_views + 1, "
                "  bot_unique_visitors = bot_unique_visitors + ?",
                (today, 1 if is_new_today else 0, 1 if is_new_today else 0),
            )
        else:
            conn.execute(
                "INSERT INTO daily_stats (day, page_views, unique_visitors) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "  page_views      = page_views + 1, "
                "  unique_visitors = unique_visitors + ?",
                (today, 1 if is_new_today else 0, 1 if is_new_today else 0),
            )

        # Compteur agrégé par catégorie de UA
        conn.execute(
            "INSERT INTO daily_categories (day, category, page_views) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(day, category) DO UPDATE SET page_views = page_views + 1",
            (today, category),
        )

        # Compteur agrégé par path (URL normalisée)
        conn.execute(
            "INSERT INTO daily_paths (day, path, page_views) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(day, path) DO UPDATE SET page_views = page_views + 1",
            (today, path),
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

    return visitor_id


def get_summary(days: int = 30) -> dict:
    """Renvoie les stats pour la page admin (humains + bots + top UA + top paths)."""
    conn = _connect()
    try:
        total_pv, total_bot_pv = conn.execute(
            "SELECT IFNULL(SUM(page_views), 0), IFNULL(SUM(bot_page_views), 0) "
            "FROM daily_stats"
        ).fetchone()

        total_humans = conn.execute(
            "SELECT COUNT(*) FROM visitors WHERE is_bot = 0"
        ).fetchone()[0]
        total_bots = conn.execute(
            "SELECT COUNT(*) FROM visitors WHERE is_bot = 1"
        ).fetchone()[0]

        today = date.today().isoformat()
        today_row = conn.execute(
            "SELECT page_views, unique_visitors, bot_page_views, bot_unique_visitors "
            "FROM daily_stats WHERE day = ?",
            (today,),
        ).fetchone() or (0, 0, 0, 0)

        recent = conn.execute(
            "SELECT day, page_views, unique_visitors, bot_page_views, bot_unique_visitors "
            "FROM daily_stats ORDER BY day DESC LIMIT ?",
            (days,),
        ).fetchall()

        avg_visits = conn.execute(
            "SELECT IFNULL(AVG(visits), 0) FROM visitors WHERE is_bot = 0"
        ).fetchone()[0]

        # Top 30 catégories d'UA sur les N derniers jours
        top_categories = conn.execute(
            "SELECT category, SUM(page_views) AS pv "
            "FROM daily_categories "
            "WHERE day >= date('now', ?) "
            "GROUP BY category ORDER BY pv DESC LIMIT 30",
            (f"-{days} days",),
        ).fetchall()

        # Top 30 paths visités sur les N derniers jours
        top_paths = conn.execute(
            "SELECT path, SUM(page_views) AS pv "
            "FROM daily_paths "
            "WHERE day >= date('now', ?) "
            "GROUP BY path ORDER BY pv DESC LIMIT 30",
            (f"-{days} days",),
        ).fetchall()

        return {
            "total_page_views": total_pv,
            "total_unique_visitors": total_humans,
            "total_bot_page_views": total_bot_pv,
            "total_bot_visitors": total_bots,
            "today_page_views": today_row[0],
            "today_unique_visitors": today_row[1],
            "today_bot_page_views": today_row[2],
            "today_bot_unique_visitors": today_row[3],
            "avg_visits_per_visitor": round(avg_visits, 2),
            "daily": [
                {
                    "day": r[0],
                    "page_views": r[1],
                    "unique_visitors": r[2],
                    "bot_page_views": r[3],
                    "bot_unique_visitors": r[4],
                }
                for r in recent
            ],
            "top_categories": [
                {"category": r[0], "page_views": r[1]} for r in top_categories
            ],
            "top_paths": [
                {"path": r[0], "page_views": r[1]} for r in top_paths
            ],
        }
    finally:
        conn.close()
