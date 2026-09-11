"""Shared pipeline helpers: repo-root resolution, config loading, paths.

All configured paths are relative to the repository root; :func:`resolve`
turns them into absolute paths. Secrets are never read here — API keys come
from environment variables only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

_CONFIG_PATH = REPO_ROOT / "config.yaml"

_ET = "America/New_York"


def load_config(path: str | Path | None = None) -> dict:
    """Load and lightly validate config.yaml. Returns the config dict."""
    cfg_path = Path(path) if path else _CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"config at {cfg_path} did not parse to a mapping")
    for section in ("broker", "universe", "risk", "strategy1", "council", "paths"):
        if section not in cfg:
            raise ValueError(f"config at {cfg_path} is missing [{section}]")
    return cfg


def resolve(*parts: str) -> Path:
    """Join *parts* onto the repo root and return an absolute Path."""
    return REPO_ROOT.joinpath(*parts)


def configured_path(cfg: dict, key: str) -> Path:
    """Return the absolute path for cfg['paths'][key], creating it."""
    rel = cfg["paths"][key]
    path = resolve(rel)
    path.mkdir(parents=True, exist_ok=True)
    return path


def today_et(date_str: str | None = None) -> str:
    """Return *date_str* if given, else today's date in America/New_York.

    Falls back to UTC if the tz database is unavailable.
    """
    if date_str:
        datetime.strptime(date_str, "%Y-%m-%d")  # validate format
        return date_str
    try:
        tz = ZoneInfo(_ET)
    except ZoneInfoNotFoundError:
        log.warning("tzdata for %s unavailable; using UTC", _ET)
        tz = None
    return datetime.now(tz).date().isoformat()
