"""CLI entry point for the Job Hunt Agent.

Usage:
    python -m src hunt                          # Run the full pipeline
    python -m src setup                         # Open browser for portal logins
    python -m src status                        # Show job counts in DB
    python -m src models                        # Show configured LLM models per agent
    python -m src metrics                       # Show agent performance from last run
    python -m src metrics all                   # Show all historical runs
    python -m src blacklist add company Acme    # Block a company
    python -m src blacklist add keyword intern  # Block a title keyword
    python -m src blacklist show                # Show all blacklist entries
    python -m src blacklist remove <id>         # Remove a blacklist entry
"""

import asyncio
import json
import subprocess
import sys

from rich.console import Console
from rich.table import Table

from src.core.config import load_config, get_enabled_portals
from src.core.db import Database
from src.core.llm import LLMClient

console = Console()


def _patch_asyncio_ssl_cleanup():
    """Suppress Python 3.9 'Fatal error on SSL transport' during event loop shutdown.

    This is a known issue: asyncio tries to write to already-closed SSL sockets
    during teardown. The error is cosmetic — everything completed successfully.
    We patch the event loop class to silently ignore these specific errors.
    """
    import asyncio.selector_events

    _original_del = getattr(asyncio.selector_events._SelectorSocketTransport, '__del__', None)

    # Patch proactor event loop on Windows, selector on Unix
    original_class = asyncio.SelectorEventLoop

    _orig_run = asyncio.run

    def _patched_run(coro, **kwargs):
        loop = asyncio.new_event_loop()

        # Install a custom exception handler that ignores SSL cleanup errors
        def _quiet_exception_handler(loop, context):
            msg = context.get("message", "")
            exc = context.get("exception")
            if "SSL" in msg or (exc and "Bad file descriptor" in str(exc)):
                return  # Suppress
            # For all other errors, use default handler
            loop.default_exception_handler(context)

        loop.set_exception_handler(_quiet_exception_handler)

        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    asyncio.run = _patched_run


def cmd_hunt():
    """Run the full sourcing pipeline."""
    from src.orchestrator import run_pipeline

    # Fix Python 3.9 SSL/asyncio cleanup errors on exit.
    # asyncio's event loop prints scary "Fatal error on SSL transport"
    # during teardown — this is cosmetic but alarming. We patch the
    # event loop's exception handler to suppress it.
    _patch_asyncio_ssl_cleanup()

    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user.[/]")
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


def cmd_setup():
    """Run the browser setup script."""
    subprocess.run([sys.executable, "setup_browser.py"])


def cmd_status():
    """Show status of the last run and job counts."""
    config = load_config()
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))

    all_jobs = db.get_jobs()
    new_jobs = [j for j in all_jobs if j["status"] == "new"]
    parsed = [j for j in all_jobs if j["parse_status"] == "parsed"]
    failed = [j for j in all_jobs if j["parse_status"] == "failed"]

    # Job stats
    table = Table(title="Job Database", show_header=False, border_style="blue")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Total jobs", str(len(all_jobs)))
    table.add_row("New (unparsed)", str(len([j for j in all_jobs if j["parse_status"] == "pending"])))
    table.add_row("Parsed", str(len(parsed)))
    table.add_row("Parse failed", str(len(failed)))
    table.add_row("With match score", str(sum(1 for j in all_jobs if j.get("match_score"))))
    table.add_row("Enabled portals", ", ".join(get_enabled_portals(config)))
    console.print(table)

    # Source breakdown
    sources: dict[str, int] = {}
    for j in all_jobs:
        for src in (j.get("source") or "unknown").split("; "):
            sources[src] = sources.get(src, 0) + 1
    if sources:
        src_table = Table(title="Jobs by Source", show_header=True, border_style="dim")
        src_table.add_column("Portal", style="cyan")
        src_table.add_column("Count", style="white", justify="right")
        for portal, count in sorted(sources.items(), key=lambda x: -x[1]):
            src_table.add_row(portal, str(count))
        console.print(src_table)

    db.close()


def cmd_models():
    """Show which LLM model each agent will use."""
    config = load_config()
    llm = LLMClient(config)
    summary = llm.get_model_config_summary()

    table = Table(title="LLM Model Configuration", border_style="blue")
    table.add_column("Agent", style="cyan", width=20)
    table.add_column("Primary Model", style="white", max_width=50)
    table.add_column("Fallback", style="dim", max_width=45)

    for agent, info in summary["agents"].items():
        table.add_row(agent, info["primary"], info["fallback"])

    console.print(table)
    console.print(f"  [dim]Default: {summary['default_model']}[/]")
    console.print(f"  [dim]Global fallback: {summary['global_fallback']}[/]")


def cmd_metrics():
    """Show agent performance metrics from recent runs."""
    config = load_config()
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))

    show_all = len(sys.argv) > 2 and sys.argv[2].lower() == "all"

    # Run history
    runs = db.conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
        (50 if show_all else 5,)
    ).fetchall()

    if not runs:
        console.print("[yellow]No runs found yet. Run `python -m src hunt` first.[/]")
        db.close()
        return

    run_table = Table(title="Run History", border_style="green")
    run_table.add_column("Run ID", style="cyan")
    run_table.add_column("Date", style="white")
    run_table.add_column("Jobs Found", justify="right")
    run_table.add_column("Parsed", justify="right")
    run_table.add_column("Errors", justify="right")
    run_table.add_column("Duration", justify="right")

    for run in runs:
        started = (run["started_at"] or "")[:19]
        completed = run["completed_at"] or ""
        run_table.add_row(
            run["id"],
            started,
            str(run["jobs_found"] or 0),
            str(run["jobs_parsed"] or 0),
            str(run["errors"] or 0),
            f"{_calc_duration(run['started_at'], run['completed_at'])}s" if completed else "...",
        )
    console.print(run_table)

    # Agent metrics from the latest run
    latest_run_id = runs[0]["id"]
    agent_rows = db.conn.execute(
        "SELECT * FROM agent_metrics WHERE run_id = ? ORDER BY created_at",
        (latest_run_id,)
    ).fetchall()

    if agent_rows:
        agent_table = Table(title=f"Agent Performance (run: {latest_run_id})", border_style="blue")
        agent_table.add_column("Agent", style="cyan")
        agent_table.add_column("Items In", justify="right")
        agent_table.add_column("Items Out", justify="right")
        agent_table.add_column("Success %", justify="right")
        agent_table.add_column("Duration", justify="right")
        agent_table.add_column("Errors", justify="right")

        for row in agent_rows:
            success_pct = f"{(row['success_rate'] or 0) * 100:.0f}%"
            duration = f"{row['avg_duration_ms'] or 0:.0f}ms"
            errors = json.loads(row["errors"]) if row["errors"] else []
            error_count = str(len(errors))

            # Color-code success rate
            if (row["success_rate"] or 0) >= 0.9:
                success_style = "green"
            elif (row["success_rate"] or 0) >= 0.7:
                success_style = "yellow"
            else:
                success_style = "red"

            agent_table.add_row(
                row["agent"],
                str(row["items_in"] or 0),
                str(row["items_out"] or 0),
                f"[{success_style}]{success_pct}[/]",
                duration,
                error_count,
            )
        console.print(agent_table)

    # Cost breakdown from latest run
    cost_rows = db.conn.execute(
        "SELECT agent, model, SUM(input_tokens) as inp, SUM(output_tokens) as outp, "
        "SUM(cost_usd) as cost FROM cost_log WHERE run_id = ? GROUP BY agent, model",
        (latest_run_id,)
    ).fetchall()

    if cost_rows:
        cost_table = Table(title=f"LLM Costs (run: {latest_run_id})", border_style="yellow")
        cost_table.add_column("Agent", style="cyan")
        cost_table.add_column("Model", style="dim")
        cost_table.add_column("Input Tokens", justify="right")
        cost_table.add_column("Output Tokens", justify="right")
        cost_table.add_column("Cost", justify="right", style="green")

        total_cost = 0.0
        for row in cost_rows:
            cost = row["cost"] or 0.0
            total_cost += cost
            cost_table.add_row(
                row["agent"],
                _short_model(row["model"] or ""),
                f"{row['inp'] or 0:,}",
                f"{row['outp'] or 0:,}",
                f"${cost:.4f}",
            )
        cost_table.add_row("", "", "", "[bold]Total[/]", f"[bold]${total_cost:.4f}[/]")
        console.print(cost_table)

    db.close()


def _calc_duration(started: str, completed: str) -> str:
    """Calculate duration between two ISO timestamps."""
    if not started or not completed:
        return "?"
    try:
        from datetime import datetime
        s = datetime.fromisoformat(started)
        c = datetime.fromisoformat(completed)
        return str(int((c - s).total_seconds()))
    except Exception:
        return "?"


def _short_model(model: str) -> str:
    """Shorten model name for display."""
    # "openrouter/deepseek/deepseek-chat-v3-0324" -> "deepseek-chat-v3-0324"
    parts = model.split("/")
    return parts[-1] if len(parts) > 1 else model


def cmd_blacklist():
    """Manage company and keyword blacklists."""
    config = load_config()
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))

    if len(sys.argv) < 3:
        console.print("[yellow]Usage: python -m src blacklist <show|add|remove>[/]")
        db.close()
        return

    subcmd = sys.argv[2].lower()

    if subcmd == "show":
        entries = db.get_blacklist()
        config_bl = config.get("blacklist", {})
        table = Table(title="Blacklist", border_style="red")
        table.add_column("ID", style="dim")
        table.add_column("Type", style="cyan")
        table.add_column("Value", style="white")
        table.add_column("Source", style="dim")

        for e in entries:
            table.add_row(e["id"][:8], e["type"], e["value"], "database")
        for c in config_bl.get("companies", []):
            table.add_row("—", "company", c, "config.yaml")
        for k in config_bl.get("title_keywords", []):
            table.add_row("—", "title_keyword", k, "config.yaml")

        console.print(table)

    elif subcmd == "add":
        if len(sys.argv) < 5:
            console.print("[yellow]Usage: python -m src blacklist add <company|keyword> <value>[/]")
            db.close()
            return
        bl_type = sys.argv[3].lower()
        value = " ".join(sys.argv[4:])
        if bl_type in ("company", "keyword", "title_keyword"):
            bl_type = "company" if bl_type == "company" else "title_keyword"
            import uuid
            bl_id = str(uuid.uuid4())[:8]
            db.add_to_blacklist(bl_id, bl_type, value)
            console.print(f"[green]Added to blacklist: {bl_type} = \"{value}\"[/]")
        else:
            console.print(f"[red]Unknown type: {bl_type}. Use 'company' or 'keyword'.[/]")

    elif subcmd == "remove":
        if len(sys.argv) < 4:
            console.print("[yellow]Usage: python -m src blacklist remove <id>[/]")
            db.close()
            return
        bl_id = sys.argv[3]
        db.conn.execute("DELETE FROM blacklist WHERE id LIKE ?", (f"{bl_id}%",))
        db.conn.commit()
        console.print(f"[green]Removed blacklist entry: {bl_id}[/]")

    db.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()
    commands = {
        "hunt": cmd_hunt,
        "blacklist": cmd_blacklist,
        "setup": cmd_setup,
        "status": cmd_status,
        "models": cmd_models,
        "metrics": cmd_metrics,
    }

    if command in commands:
        commands[command]()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
