"""Configuration loader — merges config.yaml with optional config.local.yaml + .env secrets.

Layering (each step overrides the previous):
  1. config.yaml         — generic template (committed; structural defaults)
  2. config.local.yaml   — per-user overlay written by `python -m src init` (gitignored)
  3. .env variables      — secrets and credentials

If config.local.yaml is absent, the base config.yaml is used as-is. Lists in
the overlay REPLACE base lists (they are not concatenated) so users have
clean control over keywords / signals / etc.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into `base`.

    Dict values are merged recursively. Any non-dict value in `overlay`
    replaces the value in `base` outright (this includes lists — they are
    REPLACED, not concatenated, so users have full control).
    """
    result = dict(base)
    for key, overlay_val in overlay.items():
        base_val = result.get(key)
        if isinstance(base_val, dict) and isinstance(overlay_val, dict):
            result[key] = _deep_merge(base_val, overlay_val)
        else:
            result[key] = overlay_val
    return result


def load_config(
    config_path: str = "config.yaml",
    local_config_path: str = "config.local.yaml",
    env_path: str = ".env",
) -> dict[str, Any]:
    """Load config.yaml, layer config.local.yaml on top, then overlay .env vars."""
    load_dotenv(env_path)

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_file) as f:
        config = yaml.safe_load(f) or {}

    # Layer in config.local.yaml if present (per-user overlay)
    local_file = Path(local_config_path)
    if local_file.exists():
        with open(local_file) as f:
            overlay = yaml.safe_load(f) or {}
        if overlay:
            config = _deep_merge(config, overlay)

    # Overlay env vars into config
    config.setdefault("llm", {})
    config["llm"]["openrouter_api_key"] = os.getenv("OPENROUTER_API_KEY", "")
    config["llm"]["groq_api_key"] = os.getenv("GROQ_API_KEY", "")
    config["llm"]["api_key"] = os.getenv("OPENAI_API_KEY", "")
    config["llm"]["anthropic_api_key"] = os.getenv("ANTHROPIC_API_KEY", "")
    config["llm"]["google_api_key"] = os.getenv("GOOGLE_API_KEY", "")

    config.setdefault("sheets", {})
    config["sheets"]["credentials_path"] = os.getenv(
        "GOOGLE_CREDENTIALS_PATH", "./credentials/google-service-account.json"
    )
    config["sheets"]["sheet_id"] = os.getenv("GOOGLE_SHEET_ID", "")

    config.setdefault("telegram", {})
    config["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config["telegram"]["allowed_user_ids"] = [
        int(uid.strip())
        for uid in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]

    config.setdefault("browser", {})
    config["browser"]["profile_dir"] = os.getenv(
        "BROWSER_PROFILE_DIR", config["browser"].get("profile_dir", "./data/browser_profile")
    )

    return config


def get_enabled_portals(config: dict) -> list[str]:
    """Return list of portal names that are enabled in config."""
    portals = config.get("portals", {})
    return [name for name, settings in portals.items() if settings.get("enabled", False)]
