"""CLI entry point for the Job Hunt Agent.

Usage:
    python -m src.main hunt        # Run the full pipeline
    python -m src.main setup       # Open browser for portal logins
    python -m src.main status      # Show last run stats
    python -m src.main models      # Show configured LLM models
"""

import asyncio
import subprocess
import sys

from src.core.config import load_config, get_enabled_portals
from src.core.db import Database
from src.core.llm import LLMClient


def cmd_hunt():
    """Run the full sourcing pipeline."""
    from src.orchestrator import run_pipeline
    print("Starting job hunt pipeline...\n")
    summary = asyncio.run(run_pipeline())
    print(f"\n{'='*50}")
    print(f"Pipeline complete!")
    print(f"  Jobs found:  {summary.get('jobs_found', 0)}")
    print(f"  Jobs parsed: {summary.get('jobs_parsed', 0)}")
    print(f"  Duration:    {summary.get('duration_s', 0)}s")
    print(f"  LLM cost:    ${summary.get('llm_cost', 0):.4f}")
    print(f"  Errors:      {summary.get('errors', 0)}")
    print(f"{'='*50}")


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

    print(f"Total jobs in DB:  {len(all_jobs)}")
    print(f"  New:             {len(new_jobs)}")
    print(f"  Parsed:          {len(parsed)}")
    print(f"  With score:      {sum(1 for j in all_jobs if j.get('match_score'))}")
    print(f"\nEnabled portals:   {', '.join(get_enabled_portals(config))}")

    db.close()


def cmd_models():
    """Show which LLM model each agent will use."""
    config = load_config()
    llm = LLMClient(config)
    summary = llm.get_model_config_summary()
    print(f"Default model:  {summary['default_model']}")
    print(f"Global fallback: {summary['global_fallback']}\n")
    print(f"{'Agent':<20} {'Primary Model':<50} {'Fallback'}")
    print(f"{'─'*20} {'─'*50} {'─'*40}")
    for agent, info in summary["agents"].items():
        print(f"{agent:<20} {info['primary']:<50} {info['fallback']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()
    commands = {
        "hunt": cmd_hunt,
        "setup": cmd_setup,
        "status": cmd_status,
        "models": cmd_models,
    }

    if command in commands:
        commands[command]()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
