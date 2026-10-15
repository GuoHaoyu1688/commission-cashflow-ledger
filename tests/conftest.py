from datetime import date
from pathlib import Path

import pytest

from commission_cashflow_ledger.core.store import Ledger


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(Ledger, "today", lambda self: date(2026, 10, 15))


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "data")


@pytest.fixture
def examples():
    return Path(__file__).parents[1] / "examples"


@pytest.fixture
def demo(tmp_path, examples):
    store = Ledger(tmp_path / "demo", rules_path=examples / "rules.yaml", opening_balance="1000")
    store.import_file(examples / "ledger.json")
    return store
