"""Live progress display for pipeline runs using rich."""

import sys
from contextlib import contextmanager
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

console = Console()


class PipelineProgress:
    """Real-time progress tracker displayed in the terminal."""

    def __init__(self):
        self.stages: list[dict] = []
        self.current_stage: Optional[str] = None
        self.current_detail: str = ""
        self.live: Optional[Live] = None

    def start(self, run_id: str):
        """Start the live display."""
        self.run_id = run_id
        self.stages = []
        self.live = Live(self._render(), refresh_per_second=2, console=console)
        self.live.start()

    def stop(self):
        """Stop the live display."""
        if self.live:
            self.live.update(self._render())
            self.live.stop()
            self.live = None

    def start_stage(self, name: str, total: int = 0):
        """Mark a new pipeline stage as started."""
        self.current_stage = name
        self.current_detail = ""
        self.stages.append({
            "name": name,
            "status": "running",
            "total": total,
            "done": 0,
            "errors": 0,
            "detail": "",
        })
        self._refresh()

    def update_stage(self, done: int = 0, errors: int = 0, detail: str = ""):
        """Update the current stage's progress."""
        if self.stages:
            stage = self.stages[-1]
            stage["done"] = done
            stage["errors"] = errors
            if detail:
                stage["detail"] = detail
            self._refresh()

    def complete_stage(self, done: int = 0, errors: int = 0):
        """Mark the current stage as complete."""
        if self.stages:
            stage = self.stages[-1]
            stage["status"] = "done" if errors == 0 else "partial"
            stage["done"] = done
            stage["errors"] = errors
            self._refresh()

    def fail_stage(self, error: str = ""):
        """Mark the current stage as failed."""
        if self.stages:
            stage = self.stages[-1]
            stage["status"] = "failed"
            stage["detail"] = error
            self._refresh()

    def _refresh(self):
        if self.live:
            self.live.update(self._render())

    def _render(self):
        """Render the current progress as a rich panel."""
        table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
        table.add_column("Stage", width=20)
        table.add_column("Status", width=10)
        table.add_column("Progress", width=15)
        table.add_column("Detail", max_width=50)

        for stage in self.stages:
            # Status icon
            status_map = {
                "running": "[yellow]⏳ Running[/]",
                "done": "[green]✅ Done[/]",
                "partial": "[yellow]⚠️ Partial[/]",
                "failed": "[red]❌ Failed[/]",
            }
            status = status_map.get(stage["status"], stage["status"])

            # Progress bar
            if stage["total"] > 0:
                pct = stage["done"] / stage["total"] * 100
                bar_filled = int(pct / 5)
                bar = f"[green]{'█' * bar_filled}[/][dim]{'░' * (20 - bar_filled)}[/] {stage['done']}/{stage['total']}"
            elif stage["done"] > 0:
                bar = f"{stage['done']} items"
            else:
                bar = "..." if stage["status"] == "running" else ""

            # Error count
            detail = stage.get("detail", "")
            if stage["errors"] > 0:
                detail = f"[red]{stage['errors']} errors[/] {detail}"

            table.add_row(stage["name"], status, bar, detail)

        return Panel(table, title=f"[bold]Job Hunt Pipeline[/] [dim]run:{self.run_id}[/]",
                     border_style="blue", padding=(0, 1))


def print_summary(summary: dict):
    """Print a final run summary."""
    console.print()
    table = Table(title="Run Summary", show_header=False, border_style="green",
                  title_style="bold green")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Jobs found", str(summary.get("jobs_found", 0)))
    table.add_row("Jobs parsed", str(summary.get("jobs_parsed", 0)))
    table.add_row("Duration", f"{summary.get('duration_s', 0)}s")
    table.add_row("LLM calls", str(summary.get("llm_calls", 0)))
    table.add_row("LLM cost", f"${summary.get('llm_cost', 0):.4f}")
    table.add_row("Errors", str(summary.get("errors", 0)))

    console.print(table)
    console.print()
