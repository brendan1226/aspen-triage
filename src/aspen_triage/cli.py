import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .db import connect, init_db

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

app = typer.Typer(
    help="Semantic triage tool for the Aspen Discovery JIRA.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def export(
    output: str = typer.Option("aspen-triage-export.json", "--output", "-o", help="Output JSON file."),
    include_embeddings: bool = typer.Option(False, "--include-embeddings", help="Include raw embedding vectors."),
) -> None:
    """Export all issues, comments, and AI-generated content to a single JSON file."""
    import json
    from datetime import datetime, timezone

    init_db(settings.db_path)

    with connect(settings.db_path) as conn:
        issues = [dict(r) for r in conn.execute("SELECT * FROM issues ORDER BY jira_key").fetchall()]
        comments = [dict(r) for r in conn.execute("SELECT * FROM comments ORDER BY issue_id, created").fetchall()]
        recs = [dict(r) for r in conn.execute("SELECT * FROM recommendations").fetchall()]
        code_fixes = [dict(r) for r in conn.execute("SELECT * FROM code_fixes").fetchall()]
        fix_meta = [dict(r) for r in conn.execute("SELECT * FROM code_fix_meta").fetchall()]
        qa_reviews = [dict(r) for r in conn.execute("SELECT * FROM qa_reviews").fetchall()]
        groups = [dict(r) for r in conn.execute("SELECT * FROM groups").fetchall()]
        group_members = [dict(r) for r in conn.execute("SELECT * FROM group_members").fetchall()]

    comments_by_issue: dict[int, list[dict]] = {}
    for c in comments:
        comments_by_issue.setdefault(c["issue_id"], []).append(c)

    recs_by_issue = {r["issue_id"]: r for r in recs}
    fixes_by_issue: dict[int, list[dict]] = {}
    for f in code_fixes:
        fixes_by_issue.setdefault(f["issue_id"], []).append(f)
    fix_meta_by_issue = {f["issue_id"]: f for f in fix_meta}
    qa_by_issue: dict[int, list[dict]] = {}
    for q in qa_reviews:
        qa_by_issue.setdefault(q["issue_id"], []).append(q)

    enriched = []
    for i in issues:
        internal_id = i["id"]
        if not include_embeddings:
            i.pop("embedding", None)
        i["comments"] = comments_by_issue.get(internal_id, [])
        rec = recs_by_issue.get(internal_id)
        if rec:
            try:
                rec["recommendation"] = json.loads(rec["recommendation"])
            except Exception:
                pass
        i["ai_recommendation"] = rec
        i["ai_code_fixes"] = fixes_by_issue.get(internal_id, [])
        i["ai_fix_meta"] = fix_meta_by_issue.get(internal_id)
        i["ai_qa_reviews"] = qa_by_issue.get(internal_id, [])
        enriched.append(i)

    export_data = {
        "source": "aspen-triage",
        "jira_url": "https://aspen-discovery.atlassian.net",
        "project_key": "DIS",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_issues": len(enriched),
        "total_comments": len(comments),
        "issues": enriched,
        "groups": [
            {**g, "member_issue_ids": [m["issue_id"] for m in group_members if m["group_id"] == g["id"]]}
            for g in groups
        ],
    }

    with open(output, "w") as f:
        json.dump(export_data, f, indent=2, default=str)

    size_mb = len(json.dumps(export_data, default=str)) / 1024 / 1024
    console.print(f"[green]Exported {len(enriched)} issues, {len(comments)} comments to {output} ({size_mb:.1f} MB)[/green]")


@app.command()
def harvest(
    months_back: int = typer.Option(18, "--months", help="How many months back to fetch (first run only)."),
) -> None:
    """Fetch JIRA issues and comments into the local SQLite database."""
    if not settings.jira_email or not settings.jira_api_token:
        console.print("[red]ASPEN_TRIAGE_JIRA_EMAIL and ASPEN_TRIAGE_JIRA_API_TOKEN must be set.[/red]")
        raise typer.Exit(code=1)

    from .harvest import harvest as run_harvest

    def on_page(page: int, count: int) -> None:
        console.print(f"  issues page {page}: {count} records", style="dim")

    console.print("[cyan]Harvesting Aspen Discovery JIRA...[/cyan]")
    counts = run_harvest(
        settings.db_path,
        settings.jira_email,
        settings.jira_api_token,
        months_back=months_back,
        on_page=on_page,
    )
    console.print(
        f"  {counts['issues']} issues ({counts['new_issues']} new, {counts['updated_issues']} updated), "
        f"{counts['comments']} comments"
    )
    console.print("[green]Done.[/green]")


@app.command()
def status() -> None:
    """Show current harvest state."""
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        open_issues = conn.execute("SELECT COUNT(*) FROM issues WHERE status_category != 'done'").fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM issues WHERE embedding IS NOT NULL").fetchone()[0]
        comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        harvest = conn.execute("SELECT * FROM harvest_state WHERE id = 1").fetchone()

        types = conn.execute(
            "SELECT issue_type, COUNT(*) as cnt FROM issues GROUP BY issue_type ORDER BY cnt DESC"
        ).fetchall()

    table = Table(title="aspen-triage status")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total issues", str(total))
    table.add_row("Open issues", str(open_issues))
    table.add_row("Comments", str(comments))
    table.add_row("Embedded", str(embedded))
    table.add_row("Last harvested", (harvest["last_harvested_at"] if harvest else "never"))
    console.print(table)

    if types:
        type_table = Table(title="Issue types")
        type_table.add_column("Type")
        type_table.add_column("Count", justify="right")
        for t in types:
            type_table.add_row(t["issue_type"] or "(unknown)", str(t["cnt"]))
        console.print(type_table)


@app.command()
def serve(
    host: str = typer.Option(DEFAULT_HOST, "--host"),
    port: int = typer.Option(DEFAULT_PORT, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the web dashboard."""
    import uvicorn

    init_db(settings.db_path)
    console.print(f"[cyan]aspen-triage serving on http://{host}:{port}[/cyan]")
    uvicorn.run(
        "aspen_triage.web:app",
        host=host, port=port, reload=reload,
        proxy_headers=True, forwarded_allow_ips="*",
    )


@app.command()
def embed(
    batch_size: int = typer.Option(32, "--batch-size"),
    chunk_size: int = typer.Option(500, "--chunk-size"),
) -> None:
    """Compute embeddings for issues that changed since the last run."""
    from .embed import embed_pending

    def on_progress(stage: str, payload) -> None:
        if stage == "loading_model":
            console.print(f"[cyan]Loading embedding model {payload}...[/cyan]")
        elif stage == "embedding":
            console.print(f"[cyan]Embedding {payload} issues in chunks of {chunk_size}...[/cyan]")
        elif stage == "chunk_done":
            console.print(f"  [dim]saved {payload}[/dim]")

    counts = embed_pending(settings.db_path, settings.embedding_model, batch_size, chunk_size=chunk_size, on_progress=on_progress)
    console.print(
        f"[green]Embedded {counts['embedded']} / {counts['total']}  "
        f"(skipped {counts['skipped']} unchanged)[/green]"
    )


@app.command()
def search(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(5, "--top-k", "-k"),
) -> None:
    """Rank JIRA issues by semantic similarity."""
    from .search import NoEmbeddingsError, search as semantic_search

    try:
        results = semantic_search(settings.db_path, query, settings.embedding_model, top_k=top_k)
    except NoEmbeddingsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Top {len(results)} matches for: {query!r}")
    table.add_column("Score", justify="right")
    table.add_column("Key")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Summary")

    for r in results:
        table.add_row(
            f"{r['score']:.3f}",
            r["jira_key"],
            r["issue_type"],
            r["status"],
            r["summary"],
        )
    console.print(table)


@app.command()
def classify(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(5, "--top-k", "-k"),
) -> None:
    """Semantic search plus Claude-generated verdicts."""
    from .classify import classify as run_classify
    from .search import NoEmbeddingsError

    if not settings.anthropic_api_key:
        console.print("[red]ASPEN_TRIAGE_ANTHROPIC_API_KEY is not set.[/red]")
        raise typer.Exit(code=1)

    try:
        results, verdicts = run_classify(
            settings.db_path, query, settings.embedding_model,
            settings.anthropic_api_key, settings.classification_model, top_k=top_k,
        )
    except NoEmbeddingsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    if not results:
        console.print("[yellow]No matches found.[/yellow]")
        return

    verdicts_by_idx = {i: v for i, v in enumerate(verdicts) if i < len(results)}
    for i, r in enumerate(results):
        console.print()
        console.print(
            f"[bold cyan]{r['jira_key']}[/bold cyan] "
            f"[dim]({r['issue_type']}, {r['status']}, score {r['score']:.3f})[/dim]"
        )
        console.print(f"  [bold]{r['summary']}[/bold]")
        v = verdicts_by_idx.get(i)
        if v is not None:
            console.print(f"  Verdict:   [yellow]{v.verdict}[/yellow]")
            console.print(f"  Why:       {v.rationale}")
            console.print(f"  Suggested: {v.suggested_action}")
        console.print(f"  {r['url']}")


if __name__ == "__main__":
    app()
