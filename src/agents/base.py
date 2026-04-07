"""Base agent interface — all pipeline agents implement this contract."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentResult:
    """Standard result from an agent run."""
    data: Any = None                    # Output data (list of jobs, contacts, etc.)
    count: int = 0                      # Number of items produced
    errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class BaseAgent(ABC):
    """Base class for all pipeline agents."""

    name: str = "base"

    def __init__(self, config: dict, **deps):
        self.config = config
        self.deps = deps

    @abstractmethod
    async def run(self, input_data: Any = None) -> AgentResult:
        """Execute the agent's work. Returns AgentResult."""
        ...
