import sys
from unittest.mock import MagicMock

# Mock dependencies that might be missing in the environment
sys.modules["yaml"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

from src.core.config import get_enabled_portals

def test_get_enabled_portals_mixed():
    config = {
        "portals": {
            "linkedin": {"enabled": True},
            "indeed": {"enabled": False},
            "wellfound": {"enabled": True}
        }
    }
    assert get_enabled_portals(config) == ["linkedin", "wellfound"]

def test_get_enabled_portals_all_disabled():
    config = {
        "portals": {
            "linkedin": {"enabled": False},
            "indeed": {"enabled": False}
        }
    }
    assert get_enabled_portals(config) == []

def test_get_enabled_portals_all_enabled():
    config = {
        "portals": {
            "linkedin": {"enabled": True},
            "indeed": {"enabled": True}
        }
    }
    # Dictionaries are ordered in Python 3.7+
    assert get_enabled_portals(config) == ["linkedin", "indeed"]

def test_get_enabled_portals_missing_portals_key():
    config = {"other": "stuff"}
    assert get_enabled_portals(config) == []

def test_get_enabled_portals_empty_portals():
    config = {"portals": {}}
    assert get_enabled_portals(config) == []

def test_get_enabled_portals_missing_enabled_key():
    config = {
        "portals": {
            "linkedin": {},
            "indeed": {"enabled": True}
        }
    }
    assert get_enabled_portals(config) == ["indeed"]

def test_get_enabled_portals_truthy_values():
    config = {
        "portals": {
            "linkedin": {"enabled": 1},
            "indeed": {"enabled": 0},
            "wellfound": {"enabled": "yes"}
        }
    }
    assert get_enabled_portals(config) == ["linkedin", "wellfound"]
