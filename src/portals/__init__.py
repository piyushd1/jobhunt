"""Portal adapter registry — maps portal names to adapter classes."""

from src.portals.linkedin import LinkedInAdapter
from src.portals.naukri import NaukriAdapter
from src.portals.foundit import FounditAdapter

# Add new adapters here as they're built
PORTAL_REGISTRY: dict[str, type] = {
    "linkedin": LinkedInAdapter,
    "naukri": NaukriAdapter,
    "foundit": FounditAdapter,
}


def get_adapter(name: str, config: dict):
    """Get a portal adapter instance by name."""
    cls = PORTAL_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown portal: {name}. Available: {list(PORTAL_REGISTRY.keys())}")
    return cls(config)
