"""Configuration loader — merges config.yaml with .env secrets."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> dict[str, Any]:
    """Load config.yaml and overlay environment variables from .env."""
    load_dotenv(env_path)

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_file) as f:
        config = yaml.safe_load(f)

    # Overlay env vars into config
    config.setdefault("llm", {})
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
