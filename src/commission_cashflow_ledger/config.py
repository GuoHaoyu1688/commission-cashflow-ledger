"""Validated local settings shared by all entry points."""

import os
from pathlib import Path
from typing import Any

import yaml

from .core.store import Ledger


def make_ledger(data_dir: str | None = None, config: str | None = None) -> Ledger:
    settings: dict[str, Any] = {}
    config_path = config or os.getenv("LEDGER_CONFIG")
    if config_path:
        path = Path(config_path).expanduser().resolve()
        settings = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        allowed = {
            "data_dir",
            "rules_path",
            "timezone",
            "base_currency",
            "fx_rates",
            "fx_as_of",
            "opening_balance",
        }
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("config contains unsupported keys or is not a mapping")
        for key in ("data_dir", "rules_path"):
            if settings.get(key) and not Path(settings[key]).expanduser().is_absolute():
                settings[key] = str(path.parent / settings[key])
    settings["data_dir"] = (
        data_dir or os.getenv("LEDGER_DATA_DIR") or settings.get("data_dir", "data")
    )
    return Ledger(**settings)
