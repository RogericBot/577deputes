"""anqp command-line interface.

Usage:
    anqp init                             # create empty schema
    anqp bootstrap [--force]              # full ingestion from scratch
    anqp update [--source AMO10 --source QE]  # incremental update
    anqp serve [--host 0.0.0.0 --port 8000 --reload]
    anqp stats                            # quick console summary
    anqp doctor                           # connectivity + sanity checks
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows so rich emoji/symbols don't crash on cp1252.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .db import connect, init_schema
from .ingestion.pipeline import run_ingestion
from .logging_setup import configure, get_logger

app = typer.Typer(
    add_completion=False,
    help="Local explorer for parliamentary questions, 17th legislature.",
    no_args_is_help=True,
)
console = Console()
log = get_logger(__name__)


@app.command()
def init() -> None:
    """Create / migrate the SQLite schema."""
    configure()
    conn = connect()
    init_schema(conn)
    conn.close()
    console.print(f"[green]✓[/green] schema initialised at {settings.db_path}")


@app.command()
def bootstrap(
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-download even if cache says unchanged."
    ),
    skip_download: bool = typer.Option(
        False, "--skip-download", help="Use already-downloaded ZIPs in data/raw/."
    ),
    source: list[str] = typer.Option(
        None, "--source", "-s",
        help="Limit to specific source(s): AMO10, QE, QOSD, QAG. Repeatable."
    ),
) -> None:
    """Full ingestion: download all sources, parse, upsert."""
    configure()
    sources = source or list(settings.sources.keys())
    console.print(f"[bold]Bootstrap[/bold] — sources: {', '.join(sources)}")
    res = run_ingestion(sources, force=force, skip_download=skip_download)
    _print_results(res)
    # Cache deputy photos after AMO has populated the deputies table.
    if not source or any(s.startswith("AMO") for s in sources):
        from .ingestion.photos import download_all_photos
        conn = connect()
        photo_res = download_all_photos(conn, workers=8)
        conn.close()
        console.print(
            f"[green]✓[/green] photos: {photo_res['ok']} new · "
            f"{photo_res['cached']} cached · {photo_res['missing']} not published"
        )

    # Population + inscrits per circonscription.
    if not source:
        try:
            from .ingestion.circo_stats import ingest_circo_stats
            conn = connect()
            cs_res = ingest_circo_stats(conn)
            conn.close()
            console.print(
                f"[green]✓[/green] circo_stats : {cs_res['rows']} circos "
                f"({cs_res['with_population']} pop · {cs_res['with_inscrits']} inscrits)"
            )
        except Exception as e:
            console.print(f"[yellow]![/yellow] circo_stats indisponible : {e}")


@app.command()
def update(
    source: list[str] = typer.Option(
        None, "--source", "-s",
        help="Limit to specific source(s); default = all."
    ),
) -> None:
    """Incremental update — re-run all sources; cache hits skip ingestion."""
    configure()
    sources = source or list(settings.sources.keys())
    console.print(f"[bold]Update[/bold] — sources: {', '.join(sources)}")
    res = run_ingestion(sources, force=False)
    _print_results(res)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Start the local web server."""
    configure()
    if not settings.db_path.exists():
        console.print(
            f"[red]✗[/red] DB not found at {settings.db_path}. "
            "Run [bold]anqp bootstrap[/bold] first."
        )
        raise typer.Exit(1)
    import uvicorn
    uvicorn.run(
        "anqp.web.app:app",
        host=host, port=port, reload=reload,
    )


@app.command()
def stats() -> None:
    """Print a one-screen summary of the DB."""
    configure()
    if not settings.db_path.exists():
        console.print("[red]✗[/red] DB not found. Run bootstrap first.")
        raise typer.Exit(1)
    conn = connect(read_only=True)
    overview = {}
    for t in ("organes", "deputies", "mandates", "questions"):
        overview[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
    by_type = conn.execute(
        "SELECT type, count(*) AS c, "
        "sum(case when statut='avec_reponse' then 1 else 0 end) AS r "
        "FROM questions GROUP BY type ORDER BY c DESC"
    ).fetchall()
    console.print(f"[bold]anqp database[/bold]  {settings.db_path}")
    for k, v in overview.items():
        console.print(f"  {k:>10}: {v:>6}")
    table = Table(title="Questions par type")
    table.add_column("Type"); table.add_column("Total", justify="right")
    table.add_column("Avec réponse", justify="right"); table.add_column("Taux", justify="right")
    for r in by_type:
        ratio = (r["r"] / r["c"] * 100) if r["c"] else 0
        table.add_row(r["type"], str(r["c"]), str(r["r"]), f"{ratio:.1f}%")
    console.print(table)
    conn.close()


@app.command()
def photos(
    only_active: bool = typer.Option(
        False, "--only-active", help="Skip photos of ex-deputies."
    ),
    workers: int = typer.Option(8, "--workers", "-w"),
) -> None:
    """Download every deputy portrait into the local cache (~6 MB)."""
    configure()
    from .ingestion.photos import download_all_photos
    conn = connect()
    res = download_all_photos(conn, only_active=only_active, workers=workers)
    conn.close()
    console.print(
        f"[green]✓[/green] photos: {res['ok']} new · {res['cached']} cached "
        f"· {res['missing']} not published · {res['error']} errors"
    )


@app.command()
def circo_stats() -> None:
    """Download INSEE population + Min. Intérieur inscrits, populate circo_stats."""
    configure()
    from .ingestion.circo_stats import ingest_circo_stats
    conn = connect()
    res = ingest_circo_stats(conn)
    conn.close()
    console.print(
        f"[green]✓[/green] circo_stats : {res['rows']} circos · "
        f"{res['with_population']} avec population · "
        f"{res['with_inscrits']} avec inscrits"
    )


@app.command()
def cluster_amendements(
    legislature: int = typer.Option(None, "--legislature", "-l"),
) -> None:
    """Detect near-identical amendments via MinHash. Updates `amendement_clusters`."""
    configure()
    from .ingestion.amd_clusters import compute_clusters
    conn = connect()
    leg = legislature if legislature is not None else settings.legislature
    res = compute_clusters(conn, only_legislature=leg)
    console.print(
        f"[green]✓[/green] {res['clusters']} clusters · "
        f"{res['amendements_clustered']} amendements regroupés · "
        f"{res['elapsed_s']:.1f}s"
    )


@app.command()
def doctor() -> None:
    """Quick health check: env, sqlite version, source URL reachability."""
    configure()
    import sqlite3 as _sql
    import httpx
    console.print(f"[bold]Python[/bold]  {sys.version.split()[0]}")
    console.print(f"[bold]SQLite[/bold]  {_sql.sqlite_version}")
    # FTS5 detection
    try:
        c = _sql.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        c.close()
        console.print("[bold]FTS5[/bold]    [green]available[/green]")
    except Exception as e:
        console.print(f"[bold]FTS5[/bold]    [red]missing: {e}[/red]")
    console.print(f"[bold]DB path[/bold] {settings.db_path}  exists={settings.db_path.exists()}")
    console.print("\n[bold]Sources[/bold]")
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        for name, url in settings.sources.items():
            try:
                r = client.head(url)
                size = int(r.headers.get("Content-Length", 0))
                console.print(
                    f"  {name:>5}: HTTP {r.status_code}  {size:>10,} B  "
                    f"last-modified={r.headers.get('Last-Modified','?')}"
                )
            except Exception as e:
                console.print(f"  {name:>5}: [red]error: {e}[/red]")


@app.command()
def export_questions(
    out: Path = typer.Argument(..., help="Output path (.csv or .json)."),
    type: str = typer.Option(None, help="Filter on type: QE, QOSD, QG"),
    auteur_uid: str = typer.Option(None),
    statut: str = typer.Option(None),
    rubrique: str = typer.Option(None),
    ministere: str = typer.Option(None),
) -> None:
    """Bulk-export filtered questions to CSV or JSON."""
    from .web import queries as Q
    configure()
    conn = connect(read_only=True)
    res = Q.search_questions(
        conn, qtype=type, auteur_uid=auteur_uid, statut=statut,
        rubrique=rubrique, ministere=ministere, page=1, page_size=10000,
    )
    rows = res["rows"]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        out.write_text(
            json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        import csv
        cols = list(rows[0].keys()) if rows else []
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    conn.close()
    console.print(
        f"[green]✓[/green] {len(rows)} rows → [bold]{out}[/bold]"
    )


def _print_results(res: dict[str, dict]) -> None:
    table = Table(title="Ingestion results")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Seen", justify="right")
    table.add_column("Inserted", justify="right")
    table.add_column("Updated", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Bytes", justify="right")
    table.add_column("Duration (s)", justify="right")
    for src, r in res.items():
        table.add_row(
            src, r["status"], str(r["rows_seen"]), str(r["inserted"]),
            str(r.get("updated", 0)), str(r["errors"]),
            f"{r['bytes_downloaded']:,}", f"{r['duration_seconds']:.1f}",
        )
    console.print(table)
    failures = [s for s, r in res.items() if r["status"] == "failure"]
    if failures:
        console.print(f"[red]✗[/red] failures: {', '.join(failures)}")
        raise typer.Exit(2)
    console.print("[green]✓[/green] done")


if __name__ == "__main__":
    app()
