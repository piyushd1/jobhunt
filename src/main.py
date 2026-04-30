"""CLI entry point for the Job Hunt Agent.

Quick start (new user):
    python -m src init                          # Interactive setup wizard
    python -m src setup                         # Log into portals (one-time)
    python -m src hunt                          # Run the pipeline

All commands:
    python -m src init                          # Wizard: parse resume, write config.local.yaml
    python -m src hunt                          # Run the full pipeline (incremental)
    python -m src hunt fresh                    # Nuke DB + fresh run from scratch
    python -m src reset scores                  # Reset match scores only (re-score)
    python -m src reset all                     # Nuke entire DB (full fresh start)
    python -m src setup                         # Open browser for portal logins
    python -m src status                        # Show job counts in DB
    python -m src models                        # Show configured LLM models per agent
    python -m src metrics                       # Show agent performance from last run
    python -m src metrics all                   # Show all historical runs
    python -m src config                        # Show current search config
    python -m src config exp 7 13               # Set experience filter (7-13 years)
    python -m src config exp off                # Disable experience filter
    python -m src blacklist show                # Show all blacklist entries
    python -m src blacklist add company Acme    # Block a company
    python -m src blacklist add keyword intern  # Block a title keyword
    python -m src blacklist remove <id>         # Remove a blacklist entry
    python -m src eval_matches                  # Run LLM to evaluate match scoring logic
"""

import asyncio
import json
import os
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


def cmd_init():
    """Interactive wizard: parse resume, prompt 5 questions, write config.local.yaml.

    Generates the candidate-specific overlay (search keywords, locations,
    experience range, role priority, resume signals, disqualifiers, domain
    preferences, big-brand list, excluded title keywords) by combining the
    parsed resume with short user inputs and a single LLM call.
    """
    import shutil
    from datetime import datetime
    from pathlib import Path

    import yaml
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt

    from src.agents.config_deriver import ConfigDeriver
    from src.agents.resume_profiler import ResumeProfiler
    from src.core.embeddings import EmbeddingModel

    _patch_asyncio_ssl_cleanup()

    config = load_config()
    llm = LLMClient(config)

    console.print(Panel.fit(
        "[bold cyan]Job Hunt Agent — Setup Wizard[/]\n\n"
        "Will parse your resume, ask a few short questions, and write\n"
        "a candidate-specific [bold]config.local.yaml[/] (gitignored).",
        border_style="cyan",
    ))

    async def run_wizard():
        # ── Step 1: Resume path ─────────────────────────────────────
        resume_default = config.get("resume", {}).get("path", "./data/resume.pdf")
        resume_default_p = Path(resume_default)
        existing = resume_default_p.exists()
        prompt_msg = (
            f"Path to your resume PDF [{resume_default}]"
            if existing
            else f"Path to your resume PDF (will be copied to {resume_default})"
        )
        resume_input = Prompt.ask(prompt_msg, default=resume_default).strip()
        resume_path = Path(resume_input).expanduser().resolve()
        if not resume_path.exists():
            console.print(f"[red]Resume not found: {resume_path}[/]")
            return

        # Copy to data/resume.pdf if needed
        if str(resume_path) != str(resume_default_p.resolve()):
            resume_default_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(resume_path), str(resume_default_p))
            console.print(f"[green]Copied resume → {resume_default}[/]")

        # ── Backup existing config.local.yaml + cached profile ─────
        local_path = Path("config.local.yaml")
        cache_path = Path(config.get("resume", {}).get("profile_cache",
                                                        "./data/candidate_profile.json"))
        chroma_dir = Path(config.get("output", {}).get("chroma_dir", "./data/chroma"))
        archive_dir = Path(f"./data/_archive/{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        if local_path.exists() or cache_path.exists() or chroma_dir.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
            for src in (local_path, cache_path):
                if src.exists():
                    shutil.copy(str(src), str(archive_dir / src.name))
            if chroma_dir.exists():
                shutil.copytree(str(chroma_dir), str(archive_dir / "chroma"),
                                dirs_exist_ok=True)
            console.print(f"[dim]Backed up previous setup to {archive_dir}[/]")

        # Force fresh resume parse + chroma rebuild
        if cache_path.exists():
            cache_path.unlink()
        if chroma_dir.exists():
            shutil.rmtree(chroma_dir)

        # ── Step 2: Run resume profiler ─────────────────────────────
        console.print("\n[bold]Parsing resume...[/] (this can take ~60s with the LLM)")
        emb = EmbeddingModel(config)
        profiler = ResumeProfiler(config, llm, embedding_model=emb)
        result = await profiler.run()
        if not result.success:
            console.print(f"[red]Resume parse failed: {result.errors}[/]")
            return
        profile = result.data

        skills_count = len(profile.get("all_skills_canonical", []))
        years = profile.get("total_experience_years", "?")
        console.print(
            f"[green]Parsed:[/] [bold]{profile.get('name','(unknown)')}[/] — "
            f"{profile.get('current_title','')} ({years} yrs, {skills_count} skills)"
        )

        # ── Step 3: User inputs (with prefills from parsed profile) ─
        console.print("\n[bold]Job-hunt preferences[/] (press Enter to accept defaults)")

        # Target keywords — prefill from current_title + target_roles
        prefill_keywords = []
        if profile.get("current_title"):
            prefill_keywords.append(profile["current_title"])
        for tr in profile.get("target_roles", []):
            if tr not in prefill_keywords:
                prefill_keywords.append(tr)
        if not prefill_keywords:
            prefill_keywords = ["Product Manager"]
        kw_default = ", ".join(prefill_keywords[:6])
        kw_input = Prompt.ask("Target role keywords (comma-separated)", default=kw_default)
        target_keywords = [k.strip() for k in kw_input.split(",") if k.strip()]

        # Locations — prefill from preferred_locations
        loc_default = ", ".join(profile.get("preferred_locations") or ["Bangalore"])
        loc_input = Prompt.ask("Locations to search (comma-separated)", default=loc_default)
        locations = [l.strip() for l in loc_input.split(",") if l.strip()]

        # Remote OK
        remote_ok = Confirm.ask("Open to remote roles?", default=True)

        # Experience range — prefill ± 1 around parsed years
        try:
            yint = int(years) if isinstance(years, (int, float, str)) else 5
        except (TypeError, ValueError):
            yint = 5
        emin_default = max(0, yint - 1)
        emax_default = yint + 1
        emin = int(Prompt.ask("Minimum years of experience to consider",
                               default=str(emin_default)))
        emax = int(Prompt.ask("Maximum years of experience to consider",
                               default=str(emax_default)))

        # Sheet ID
        sheet_default = config.get("sheets", {}).get("sheet_id") or ""
        sheet_id = Prompt.ask(
            "Google Sheet ID (leave blank if you'll set GOOGLE_SHEET_ID in .env)",
            default=sheet_default,
        ).strip()

        user_inputs = {
            "target_keywords": target_keywords,
            "locations": locations,
            "remote_ok": remote_ok,
            "experience_min": emin,
            "experience_max": emax,
        }

        # ── Step 4: Auto-derive ─────────────────────────────────────
        console.print("\n[bold]Auto-deriving search config from resume...[/] "
                       "(single LLM call, ~30-90s)")
        deriver = ConfigDeriver(config, llm)
        derived = await deriver.derive(profile, user_inputs)

        # ── Step 5: Write config.local.yaml ─────────────────────────
        local_doc = {
            "search": {
                "keywords": target_keywords,
                "locations": locations,
                "remote_ok": remote_ok,
                "experience_min": emin,
                "experience_max": emax,
                "experience_buffer": 0,
                "role_priority": derived["role_priority"],
                "excluded_title_keywords": derived["excluded_title_keywords"],
            },
            "big_brand_companies": derived["big_brand_companies"],
            "matching": {
                "resume_signals": derived["resume_signals"],
                "disqualifiers": derived["disqualifiers"],
                "domain_preferences": derived["domain_preferences"],
            },
        }
        if sheet_id:
            local_doc["sheets"] = {"sheet_id": sheet_id}

        with open(local_path, "w") as f:
            f.write(f"# config.local.yaml — generated by `python -m src init` on "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write("# Candidate-specific overrides for config.yaml. Re-run init to regenerate.\n\n")
            yaml.safe_dump(local_doc, f, default_flow_style=False, sort_keys=False,
                            allow_unicode=True)
        console.print(f"\n[green]Wrote {local_path}[/]")
        console.print(
            f"  resume_signals:    {len(derived['resume_signals'])} entries\n"
            f"  disqualifiers:     {len(derived['disqualifiers'])} entries\n"
            f"  domain strong:     {len(derived['domain_preferences']['strong_fit'])} entries\n"
            f"  big-brand list:    {len(derived['big_brand_companies'])} companies\n"
            f"  excluded titles:   {len(derived['excluded_title_keywords'])} entries"
        )

        # ── Step 6: Next steps ──────────────────────────────────────
        env_path = Path(".env")
        env_ok = env_path.exists()
        next_steps = (
            "[bold]Next steps[/]\n\n"
            f"  {'✓' if env_ok else '☐'} .env file present"
            f" (fill in API keys + Google credentials if not done)\n"
            f"  ☐ Share the Google Sheet with your service account email (Editor access)\n"
            f"  ☐ Run [cyan]python -m src setup[/] to log into job portals (one-time)\n"
            f"  ☐ Then [cyan]python -m src hunt[/] to start the pipeline"
        )
        console.print(Panel(next_steps, border_style="green"))

    try:
        asyncio.run(run_wizard())
    except KeyboardInterrupt:
        console.print("\n[yellow]Wizard cancelled.[/]")
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


def cmd_reset():
    """Reset job data for a fresh run."""
    config = load_config()
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))

    subcmd = sys.argv[2].lower() if len(sys.argv) > 2 else "scores"

    if subcmd == "scores":
        db.conn.execute(
            """UPDATE jobs SET
               match_score = NULL,
               skill_score = NULL,
               required_skill_score = NULL,
               preferred_skill_score = NULL,
               experience_score = NULL,
               location_score = NULL,
               domain_score = NULL,
               role_fit_score = NULL,
               matched_skills = NULL,
               missing_skills = NULL,
               match_summary = NULL,
               role_family = NULL,
               fit_bucket = NULL,
               penalty_reasons = NULL"""
        )
        db.conn.execute("DELETE FROM contacts")
        db.conn.execute("DELETE FROM drafts")
        db.conn.commit()
        count = db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        console.print(f"[green]Reset scores for {count} jobs. Contacts and drafts cleared.[/]")
        console.print("[dim]Next `hunt` will re-score all parsed jobs with current algorithm.[/]")

    elif subcmd == "all":
        db.conn.execute("DELETE FROM jobs")
        db.conn.execute("DELETE FROM contacts")
        db.conn.execute("DELETE FROM drafts")
        db.conn.execute("DELETE FROM runs")
        db.conn.execute("DELETE FROM agent_metrics")
        db.conn.execute("DELETE FROM cost_log")
        db.conn.commit()
        # Also clear chroma
        import shutil
        chroma_dir = config.get("output", {}).get("chroma_dir", "./data/chroma")
        if os.path.exists(chroma_dir):
            shutil.rmtree(chroma_dir)
        # Clear resume cache
        cache = config.get("resume", {}).get("profile_cache", "./data/candidate_profile.json")
        if os.path.exists(cache):
            os.remove(cache)
        console.print("[green]Full reset: all jobs, contacts, drafts, metrics, chroma, resume cache cleared.[/]")
        console.print("[dim]Next `hunt` starts completely fresh.[/]")

    else:
        console.print("[yellow]Usage: python -m src reset <scores|all>[/]")

    db.close()


def cmd_hunt():
    """Run the full sourcing pipeline."""
    from src.orchestrator import run_pipeline

    # Check for "fresh" flag
    if len(sys.argv) > 2 and sys.argv[2].lower() == "fresh":
        console.print("[yellow]Fresh run requested — resetting everything...[/]")
        sys.argv = [sys.argv[0], "reset", "all"]
        cmd_reset()
        sys.argv = [sys.argv[0], "hunt"]

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

    # Lead gen eval metrics
    contacts_rows = db.conn.execute(
        "SELECT c.confidence, COUNT(*) as cnt FROM contacts c "
        "JOIN jobs j ON c.job_id = j.id GROUP BY c.confidence"
    ).fetchall()
    total_contacts = db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    total_drafts = db.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    shortlisted = db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE match_score >= ?",
        (config.get("matching", {}).get("shortlist_threshold", 70),)
    ).fetchone()[0]
    with_contacts = db.conn.execute(
        "SELECT COUNT(DISTINCT job_id) FROM contacts"
    ).fetchone()[0]

    if shortlisted > 0 or total_contacts > 0:
        lead_table = Table(title="Lead Gen Eval", border_style="magenta")
        lead_table.add_column("Metric", style="cyan")
        lead_table.add_column("Value", style="white", justify="right")

        lead_table.add_row("Shortlisted jobs (≥ threshold)", str(shortlisted))
        lead_table.add_row("Jobs with contacts found", f"{with_contacts} ({with_contacts*100//max(shortlisted,1)}%)")
        lead_table.add_row("Total contacts", str(total_contacts))
        lead_table.add_row("Avg contacts per job", f"{total_contacts/max(with_contacts,1):.1f}" if with_contacts else "0")
        lead_table.add_row("Total drafts written", str(total_drafts))
        for row in contacts_rows:
            lead_table.add_row(f"  Confidence: {row['confidence']}", str(row["cnt"]))

        console.print(lead_table)

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


def cmd_config():
    """Show or update search configuration."""
    config = load_config()
    search = config.get("search", {})

    if len(sys.argv) < 3:
        # Show current config
        table = Table(title="Search Configuration", border_style="blue")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Keywords", ", ".join(search.get("keywords", [])))
        table.add_row("Locations", ", ".join(search.get("locations", search.get("location", ["India"]))))

        exp_min = search.get("experience_min", 0)
        exp_max = search.get("experience_max", 99)
        exp_buf = search.get("experience_buffer", 2)
        if exp_max < 99:
            table.add_row("Experience target", f"{exp_min}-{exp_max} years")
            table.add_row("Experience buffer", f"±{exp_buf} years (accepts {max(0,exp_min-exp_buf)}-{exp_max+exp_buf})")
        else:
            table.add_row("Experience filter", "OFF (all experience levels)")

        table.add_row("Max results/portal", str(search.get("max_results_per_portal", 25)))
        table.add_row("Enabled portals", ", ".join(get_enabled_portals(config)))
        console.print(table)
        return

    subcmd = sys.argv[2].lower()

    if subcmd == "exp":
        import yaml
        config_path = "config.yaml"
        with open(config_path) as f:
            raw = yaml.safe_load(f)

        if len(sys.argv) > 3 and sys.argv[3].lower() == "off":
            raw["search"]["experience_min"] = 0
            raw["search"]["experience_max"] = 99
            with open(config_path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
            console.print("[green]Experience filter disabled — all experience levels accepted.[/]")
        elif len(sys.argv) >= 5:
            exp_min = int(sys.argv[3])
            exp_max = int(sys.argv[4])
            raw["search"]["experience_min"] = exp_min
            raw["search"]["experience_max"] = exp_max
            buf = raw["search"].get("experience_buffer", 2)
            with open(config_path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
            console.print(f"[green]Experience filter set: {exp_min}-{exp_max} years (accepts {max(0,exp_min-buf)}-{exp_max+buf} with buffer)[/]")
        else:
            console.print("[yellow]Usage: python -m src config exp <min> <max>  or  python -m src config exp off[/]")
    else:
        console.print("[yellow]Usage: python -m src config [exp <min> <max>|exp off][/]")


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



def cmd_eval_matches():
    from src.eval.eval_matches import run_evaluator
    limit = 10
    if len(sys.argv) > 3 and sys.argv[2] == "--limit":
        try:
            limit = int(sys.argv[3])
        except ValueError:
            pass

    console.print(f"[bold cyan]Running Match Evaluator (limit: {limit})...[/]")
    report_path = asyncio.run(run_evaluator(limit=limit))
    if report_path:
        console.print(f"[bold green]Report generated: {report_path}[/]")
    else:
        console.print(f"[bold red]Failed to generate report.[/]")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()
    commands = {
        "init": cmd_init,
        "hunt": cmd_hunt,
        "reset": cmd_reset,
        "config": cmd_config,
        "blacklist": cmd_blacklist,
        "setup": cmd_setup,
        "status": cmd_status,
        "models": cmd_models,
        "metrics": cmd_metrics,
        "eval_matches": cmd_eval_matches,
    }

    if command in commands:
        commands[command]()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
