"""Fetch commands: fetch, batch, search, cache."""

from pathlib import Path

import typer

from ._utils import (
    app, console, load_config, _ensure_email, _read_dois, _setup_logging,
)


def register_commands(app_: typer.Typer) -> None:
    app_.command()(fetch)
    app_.command()(batch)
    app_.command()(search)
    app_.command()(cache_cmd)


def fetch(
    identifier: str = typer.Argument(help="DOI or URL of the paper to fetch."),
    output: str = typer.Option("", "--output", "-o", help="Output directory for PDFs."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    text_only: bool = typer.Option(False, "--text-only", "-t", help="Output only plain text (minimal tokens)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch a single paper by DOI or URL."""
    from ..fetcher import PaperFetcher

    _setup_logging(verbose)
    config = load_config()
    _ensure_email(config)
    if output:
        config["output_dir"] = output
    fetcher = PaperFetcher(config)
    try:
        console.print(f"[bold]Fetching:[/bold] {identifier}")
        result = fetcher.fetch_with_result(identifier, use_cache=not no_cache)
        paper = result.paper
        if result.status != "success":
            console.print(f"[yellow]Status: {result.status} ({result.reason or result.quality})[/yellow]")
            if result.next_action:
                console.print(f"[yellow]Next: {result.next_action.message}[/yellow]")
                if result.next_action.command:
                    console.print(f"[dim]{result.next_action.command}[/dim]")
        if text_only:
            console.print(result.to_text())
        elif format == "markdown":
            console.print(result.to_markdown())
        elif format == "text":
            console.print(result.to_text())
        else:
            console.print(result.to_json())
        if paper.pdf_path:
            console.print(f"\n[dim]PDF saved to: {paper.pdf_path}[/dim]")
        console.print(f"[dim]Source: {paper.source}[/dim]")
    finally:
        fetcher.close()


def batch(
    file: Path = typer.Argument(help="File containing DOIs (one per line)."),
    output: str = typer.Option("", "--output", "-o", help="Output directory."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch multiple papers from a file of DOIs."""
    from ..fetcher import PaperFetcher

    _setup_logging(verbose)
    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)
    dois = _read_dois(file)
    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)
    console.print(f"[bold]Found {len(dois)} DOIs to fetch.[/bold]")
    config = load_config()
    if output:
        config["output_dir"] = output
    fetcher = PaperFetcher(config)
    results_dir = Path(config.get("output_dir", ""))
    results_dir.mkdir(parents=True, exist_ok=True)
    succeeded = 0
    failed = 0
    try:
        for i, doi in enumerate(dois, 1):
            console.print(f"\n[bold][{i}/{len(dois)}][/bold] Fetching: {doi}")
            try:
                paper = fetcher.fetch(doi)
                if paper.full_text:
                    succeeded += 1
                    safe_name = doi.replace("/", "_").replace(":", "_")
                    if format == "markdown":
                        out_file = results_dir / f"{safe_name}.md"
                        out_file.write_text(paper.to_markdown(), encoding="utf-8")
                    elif format == "text":
                        out_file = results_dir / f"{safe_name}.txt"
                        out_file.write_text(paper.to_text(), encoding="utf-8")
                    else:
                        out_file = results_dir / f"{safe_name}.json"
                        out_file.write_text(paper.to_json(), encoding="utf-8")
                    console.print(f"  [green]OK[/green] → {out_file.name}")
                else:
                    failed += 1
                    console.print("  [yellow]No full text extracted[/yellow]")
            except Exception as e:
                failed += 1
                console.print(f"  [red]Error: {e}[/red]")
        console.print(f"\n[bold]Done:[/bold] {succeeded} succeeded, {failed} failed out of {len(dois)}.")
    finally:
        fetcher.close()


def search(
    query: str = typer.Argument(help="Search query."),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results."),
    year: str = typer.Option("", "--year", "-y", help="Year range, e.g., '2020-2024' or '2020-'."),
    do_fetch: bool = typer.Option(False, "--fetch", help="Also fetch full text for results with DOIs."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Search for papers via Semantic Scholar."""
    from rich.table import Table as RichTable
    from ..fetcher import PaperFetcher
    from ..sources import semantic_scholar

    _setup_logging(verbose)
    console.print(f"[bold]Searching:[/bold] {query}")
    results = semantic_scholar.search(query, limit=limit, year_range=year or None)
    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)
    table = RichTable(title=f"Search Results ({len(results)})")
    table.add_column("#", style="dim", width=3)
    table.add_column("Year", width=5)
    table.add_column("Title", max_width=60)
    table.add_column("Authors", max_width=30)
    table.add_column("DOI", max_width=25)
    table.add_column("Cites", width=5, justify="right")
    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        table.add_row(
            str(i), str(r.year or ""), r.title[:60], authors_str[:30],
            r.doi[:25] if r.doi else r.arxiv_id[:25] if r.arxiv_id else "",
            str(r.citation_count),
        )
    console.print(table)
    if do_fetch:
        fetchable = [r for r in results if r.doi or r.arxiv_id]
        if fetchable:
            console.print(f"\n[bold]Fetching {len(fetchable)} papers...[/bold]")
            config = load_config()
            fetcher = PaperFetcher(config)
            try:
                for r in fetchable:
                    identifier = r.doi or f"arxiv:{r.arxiv_id}"
                    console.print(f"  Fetching: {identifier}")
                    try:
                        paper = fetcher.fetch(identifier)
                        status = "[green]OK[/green]" if paper.full_text else "[yellow]No text[/yellow]"
                        console.print(f"    {status}")
                    except Exception as e:
                        console.print(f"    [red]Error: {e}[/red]")
            finally:
                fetcher.close()


def cache_cmd(
    action: str = typer.Argument(help="Action: 'clear' to remove cached results."),
):
    """Manage the paper cache."""
    from ..fetcher import PaperFetcher

    if action == "clear":
        config = load_config()
        fetcher = PaperFetcher(config)
        fetcher.clear_cache()
        console.print("[green]Cache cleared.[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use 'clear'.[/red]")
        raise typer.Exit(1)
