"""Configuration and secrets.

Config is a YAML file in the repo; secrets are environment variables and never
touch the repo. `.env` is read for local runs only — GitHub Actions injects the
same names from repository secrets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
DEFAULT_SOURCES_PATH = CONFIG_DIR / "sources.yaml"
VOICE_CARD_PATH = CONFIG_DIR / "voice_card.md"


class ConfigError(Exception):
    """Raised when required configuration or a secret is missing."""


def load_dotenv(path: Optional[Path] = None) -> None:
    """Load KEY=VALUE lines from a local .env. Existing env vars win."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class Config:
    """Dotted-path read-only view over the YAML config."""

    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, None)
        if value is None:
            raise ConfigError(f"missing required config key: {dotted}")
        return value

    def __contains__(self, dotted: str) -> bool:
        return self.get(dotted, _MISSING) is not _MISSING

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    # --- frequently used values, named so call sites read cleanly ---------

    @property
    def dry_run(self) -> bool:
        """Ship-safe default: True. The human turns this off deliberately."""
        return bool(self.get("publish.dry_run", True))

    @property
    def staleness_hours(self) -> int:
        return int(self.get("publish.staleness_hours", 48))

    @property
    def max_revisions(self) -> int:
        return int(self.get("drafting.max_revisions", 3))


_MISSING = object()


def load_config(path: Optional[Path] = None) -> Config:
    load_dotenv()
    path = Path(path or os.environ.get("LNP_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(data, path)


def load_sources(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Flatten sources.yaml into a list of feed dicts carrying their tier."""
    path = Path(path or os.environ.get("LNP_SOURCES") or DEFAULT_SOURCES_PATH)
    if not path.exists():
        raise ConfigError(f"sources file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds: List[Dict[str, Any]] = []
    for tier_key, tier in (data.get("tiers") or {}).items():
        tier_number = int(str(tier_key).replace("tier_", "").strip())
        weight = float(tier.get("weight", 1.0))
        for feed in tier.get("feeds") or []:
            entry = dict(feed)
            entry["tier"] = tier_number
            entry["weight"] = weight
            entry.setdefault("enabled", True)
            entry.setdefault("verify", False)
            entry.setdefault("ingest", "rss")
            feeds.append(entry)
    return feeds


# ---- secrets ----------------------------------------------------------


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    load_dotenv()
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise ConfigError(
            f"missing required secret {name}; set it in .env locally or as a "
            f"GitHub Actions secret in CI"
        )
    return value


def google_credentials_info() -> Dict[str, Any]:
    """Read GOOGLE_SA_JSON as either a filesystem path or a raw JSON blob.

    Local runs point at a downloaded key file; GitHub Actions pastes the whole
    JSON into a secret. Both must work with no other change.
    """
    raw = require_env("GOOGLE_SA_JSON")
    candidate = Path(raw).expanduser()
    try:
        if candidate.is_file():
            raw = candidate.read_text(encoding="utf-8")
    except OSError:
        pass  # A JSON blob makes an invalid path on some platforms; fall through.
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "GOOGLE_SA_JSON is neither a readable file path nor valid JSON"
        ) from exc
    if "client_email" not in info:
        raise ConfigError("GOOGLE_SA_JSON does not look like a service account key")
    return info
