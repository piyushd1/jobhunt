"""Portal adapter registry — maps portal names to adapter classes."""

from src.portals.linkedin import LinkedInAdapter
from src.portals.linkedin_posts import LinkedInPostsAdapter
from src.portals.naukri import NaukriAdapter
from src.portals.foundit import FounditAdapter
from src.portals.indeed import IndeedAdapter
from src.portals.instahyre import InstahyreAdapter
from src.portals.hirist import HiristAdapter
from src.portals.wellfound import WellfoundAdapter

# Add new adapters here as they're built
PORTAL_REGISTRY: dict[str, type] = {
    "linkedin": LinkedInAdapter,
    "linkedin_posts": LinkedInPostsAdapter,
    "naukri": NaukriAdapter,
    "foundit": FounditAdapter,
    "indeed": IndeedAdapter,
    "instahyre": InstahyreAdapter,
    "hirist": HiristAdapter,
    "wellfound": WellfoundAdapter,
}


def get_adapter(name: str, config: dict):
    """Get a portal adapter instance by name."""
    cls = PORTAL_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown portal: {name}. Available: {list(PORTAL_REGISTRY.keys())}")
    return cls(config)
