"""Loads agent_config.yaml. Read fresh on every call so edits apply without a restart."""

from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).resolve().parent / "agent_config.yaml"


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def bot_profile(cfg: dict, role: str) -> dict:
    """Select an explicit role profile; never silently fall back to the seller."""
    if role not in ('buyer', 'seller'):
        raise ValueError('role must be buyer or seller')
    return cfg['bot_profiles'][role]
