"""Run metrics collector — tracks per-agent performance."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class AgentMetrics:
    """Metrics for a single agent execution."""
    agent: str
    items_in: int = 0
    items_out: int = 0
    errors: list = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    @property
    def success_rate(self) -> float:
        if self.items_in == 0:
            return 1.0
        return self.items_out / self.items_in


class MetricsCollector:
    """Collects metrics across a pipeline run."""

    def __init__(self):
        self.agent_metrics: list[AgentMetrics] = []
        self.run_start: float = 0.0
        self.run_end: float = 0.0
        self.total_errors: int = 0

    def start_run(self) -> None:
        self.run_start = time.time()
        self.agent_metrics.clear()
        self.total_errors = 0

    def end_run(self) -> None:
        self.run_end = time.time()

    @contextmanager
    def track_agent(self, agent_name: str, items_in: int = 0):
        """Context manager to track an agent's execution."""
        metrics = AgentMetrics(agent=agent_name, items_in=items_in)
        metrics.start_time = time.time()
        try:
            yield metrics
        except Exception as e:
            metrics.errors.append(str(e))
            self.total_errors += 1
            raise
        finally:
            metrics.end_time = time.time()
            self.agent_metrics.append(metrics)
            logger.info(
                "agent_completed",
                agent=agent_name,
                items_in=metrics.items_in,
                items_out=metrics.items_out,
                success_rate=f"{metrics.success_rate:.1%}",
                duration_ms=f"{metrics.duration_ms:.0f}",
                errors=len(metrics.errors),
            )

    @property
    def total_duration_s(self) -> float:
        if self.run_start and self.run_end:
            return self.run_end - self.run_start
        return 0.0

    def summary(self) -> dict:
        """Return a summary dict for logging and run table."""
        return {
            "total_duration_s": round(self.total_duration_s, 1),
            "total_errors": self.total_errors,
            "agents": [
                {
                    "agent": m.agent,
                    "items_in": m.items_in,
                    "items_out": m.items_out,
                    "success_rate": round(m.success_rate, 3),
                    "duration_ms": round(m.duration_ms, 0),
                    "errors": m.errors,
                }
                for m in self.agent_metrics
            ],
        }
