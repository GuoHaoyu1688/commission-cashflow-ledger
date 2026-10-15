"""Versioned progressive per-deal commission rules and cumulative reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .money import cents, rate, rounded

ALLOWED = {"basis", "rate", "tiers", "cap", "floor", "withholding_rate", "payout_delay_months"}


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rule, dict) or set(rule) - ALLOWED:
        raise ValueError("rule contains unsupported keys")
    result = copy.deepcopy(rule)
    result.setdefault("basis", "receipts")
    result.setdefault("rate", "0.03")
    result.setdefault("withholding_rate", "0")
    result.setdefault("payout_delay_months", 0)
    if result["basis"] not in {"receipts", "contract"}:
        raise ValueError("basis must be receipts or contract")
    for name in ("rate", "withholding_rate"):
        result[name] = str(rate(result[name]))
    if (
        type(result["payout_delay_months"]) is not int
        or not 0 <= result["payout_delay_months"] <= 120
    ):
        raise ValueError("payout_delay_months must be an integer from 0 to 120")
    result["floor_cents"] = cents(result.pop("floor", "0"))
    cap = result.pop("cap", None)
    result["cap_cents"] = cents(cap) if cap is not None else None
    if result["cap_cents"] is not None and result["floor_cents"] > result["cap_cents"]:
        raise ValueError("floor cannot exceed cap")
    if "tiers" in result:
        tiers = result["tiers"]
        if not isinstance(tiers, list) or not tiers:
            raise ValueError("tiers must be a non-empty list")
        previous = 0
        normalized = []
        for index, tier in enumerate(tiers):
            if not isinstance(tier, dict) or set(tier) != {"up_to", "rate"}:
                raise ValueError("each tier needs exactly up_to and rate")
            upper = cents(tier["up_to"]) if tier["up_to"] is not None else None
            if upper is None and index != len(tiers) - 1:
                raise ValueError("an unlimited tier must be last")
            if upper is not None and upper <= previous:
                raise ValueError("tier thresholds must increase")
            normalized.append({"up_to_cents": upper, "rate": str(rate(tier["rate"]))})
            previous = upper or previous
        if normalized[-1]["up_to_cents"] is not None:
            raise ValueError("the final tier must have up_to: null")
        result["tiers"] = normalized
    return result


def load_rules(path: str | Path) -> tuple[str, str, dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or set(raw) - {"version", "default", "overrides"}
    ):
        raise ValueError("rules require version: 1, default and optional overrides")
    defaults = raw.get("default", {})
    result = {"default": validate_rule(defaults), "overrides": {}}
    overrides = raw.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("overrides must map deal codes to rules")
    for code, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError("each deal override must be a mapping")
        merged = {**defaults, **override}
        if "rate" in override and "tiers" not in override:
            merged.pop("tiers", None)
        result["overrides"][str(code)] = validate_rule(merged)
    canonical = json.dumps(raw, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest(), text, result


def select_rule(
    rules: dict[str, Any], code: str, override_rate: str | None = None
) -> dict[str, Any]:
    result = copy.deepcopy(rules["overrides"].get(code, rules["default"]))
    if override_rate is not None:
        result["rate"] = str(rate(override_rate))
        result.pop("tiers", None)
    return result


def cumulative(basis_cents: int, rule: dict[str, Any]) -> tuple[int, int, int]:
    """Return cumulative gross, estimated withholding and net in cents.

    A zero basis earns zero, including after a full reversal. Positive bases receive
    the deal lifetime floor/cap. Incremental events are differences of these totals.
    """
    if basis_cents < 0:
        raise ValueError("cumulative commission basis cannot be negative")
    if basis_cents == 0:
        return 0, 0, 0
    total = Decimal(0)
    if rule.get("tiers"):
        lower = 0
        for tier in rule["tiers"]:
            upper = (
                basis_cents
                if tier["up_to_cents"] is None
                else min(basis_cents, tier["up_to_cents"])
            )
            width = max(0, upper - lower)
            total += Decimal(width) * Decimal(tier["rate"])
            lower = upper
            if upper >= basis_cents:
                break
    else:
        total = Decimal(basis_cents) * Decimal(rule["rate"])
    gross = max(rounded(total), rule["floor_cents"])
    if rule["cap_cents"] is not None:
        gross = min(gross, rule["cap_cents"])
    tax = rounded(Decimal(gross) * Decimal(rule["withholding_rate"]))
    return gross, tax, gross - tax
