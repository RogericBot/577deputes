"""Centralised configuration. All paths are resolved from PROJECT_ROOT.

Override any setting with an environment variable of the same name (uppercase).
Example: ANQP_DB_PATH=/tmp/test.db.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Default legislature : the one currently sitting (XVII began July 2024).
# Override via env var ANQP_LEGISLATURE=16 to ingest a previous legislature.
DEFAULT_LEGISLATURE = 17

DATA_BASE_TPL = "https://data.assemblee-nationale.fr/static/openData/repository/{legislature}"


def build_sources(legislature: int) -> dict[str, str]:
    """Build the source URL map for any legislature number.

    Note : older legislatures (≤ 16) still publish at the same path, but
    not all dumps are guaranteed (e.g. the AMO50 historical snapshot is
    only useful at the time of a legislature transition).
    """
    base = DATA_BASE_TPL.format(legislature=legislature)
    # NOTE: AMO50 (historical seed) MUST come before AMO10 (current state)
    # so that AMO10's INSERT OR REPLACE wins for active deputies and AMO50
    # only contributes the ex-deputies who left during the legislature.
    return {
        "AMO50": (
            f"{base}/amo/acteurs_mandats_organes_divises/"
            "AMO50_acteurs_mandats_organes_divises.json.zip"
        ),
        "AMO10": (
            f"{base}/amo/deputes_actifs_mandats_actifs_organes/"
            "AMO10_deputes_actifs_mandats_actifs_organes.json.zip"
        ),
        "QE": (
            f"{base}/questions/questions_ecrites/Questions_ecrites.json.zip"
        ),
        "QOSD": (
            f"{base}/questions/questions_orales_sans_debat/"
            "Questions_orales_sans_debat.json.zip"
        ),
        "QAG": (
            f"{base}/questions/questions_gouvernement/Questions_gouvernement.json.zip"
        ),
        "DOSSIERS": (
            f"{base}/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip"
        ),
        "AMENDEMENTS": (
            f"{base}/loi/amendements_div_legis/Amendements.json.zip"
        ),
        "SCRUTINS": (
            f"{base}/loi/scrutins/Scrutins.json.zip"
        ),
        # Phase 4 — séances + comptes rendus pour la reconstruction QAG.
        "AGENDA": (
            f"{base}/vp/reunions/Agenda.json.zip"
        ),
        "SYSERON": (
            f"{base}/vp/syceronbrut/syseron.xml.zip"
        ),
    }


# Legacy alias kept for any code reaching directly into this module.
SOURCES = build_sources(DEFAULT_LEGISLATURE)


def db_path_for(legislature: int) -> Path:
    """Path of the SQLite file holding a given legislature's data.

    Strict-isolation design : each legislature lives in its own DB file, so
    a query can never accidentally mix two legislatures. The *current*
    legislature keeps the historical name ``anqp.db`` (so the production
    deployment and the auto-refresh loop are unaffected) ; past legislatures
    get ``anqp-{n}.db``.
    """
    data_dir = PROJECT_ROOT / "data"
    if legislature == DEFAULT_LEGISLATURE:
        return data_dir / "anqp.db"
    return data_dir / f"anqp-{legislature}.db"


# Public-facing canonical URL templates for verification + UI deep links.
def web_question_url(legislature: int, numero: int | str, type_suffix: str) -> str:
    """questions.assemblee-nationale.fr deep link for any legislature."""
    return f"https://questions.assemblee-nationale.fr/q{legislature}/{legislature}-{numero}{type_suffix}.htm"


# Legacy alias (legislature 17) — kept so older call sites don't break.
WEB_QUESTION_URL = "https://questions.assemblee-nationale.fr/q17/17-{numero}{type}.htm"
WEB_DEPUTY_URL = "https://www.assemblee-nationale.fr/dyn/deputes/{uid}"


@dataclass
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    photos_dir: Path = PROJECT_ROOT / "src" / "anqp" / "web" / "static" / "photos"
    db_path: Path = PROJECT_ROOT / "data" / "anqp.db"
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"
    log_json: bool = True
    http_timeout: float = 60.0
    user_agent: str = "anqp/0.2 (+local; https://github.com/local/anqp)"
    page_size_default: int = 50
    page_size_max: int = 200
    legislature: int = DEFAULT_LEGISLATURE
    backend: str = "sqlite"     # 'sqlite' | future: 'postgres'
    sources: dict = field(default_factory=lambda: build_sources(DEFAULT_LEGISLATURE))


def load_settings() -> Settings:
    s = Settings()
    if v := os.environ.get("ANQP_LEGISLATURE"):
        try:
            s.legislature = int(v)
            s.sources = build_sources(s.legislature)
            # Each legislature has its own DB file (strict isolation). An
            # explicit ANQP_DB_PATH below still wins if the user wants it.
            s.db_path = db_path_for(s.legislature)
        except ValueError:
            pass
    if v := os.environ.get("ANQP_DB_PATH"):
        s.db_path = Path(v)
    if v := os.environ.get("ANQP_DATA_DIR"):
        s.data_dir = Path(v)
        s.raw_dir = s.data_dir / "raw"
    if v := os.environ.get("ANQP_LOG_LEVEL"):
        s.log_level = v
    if v := os.environ.get("ANQP_LOG_JSON"):
        s.log_json = v.lower() in {"1", "true", "yes"}
    if v := os.environ.get("ANQP_USER_AGENT"):
        s.user_agent = v
    if v := os.environ.get("ANQP_BACKEND"):
        s.backend = v
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.raw_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    s.photos_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = load_settings()
