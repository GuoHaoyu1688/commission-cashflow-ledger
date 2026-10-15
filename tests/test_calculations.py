import json
from decimal import Decimal

import pytest
import yaml

from commission_cashflow_ledger.core.money import amount, cents, decimal, rate
from commission_cashflow_ledger.core.rules import cumulative, load_rules, validate_rule


def configure(ledger, default=None, overrides=None):
    ledger.rules_path.write_text(
        yaml.safe_dump(
            {"version": 1, "default": default or {"rate": "0.03"}, "overrides": overrides or {}}
        )
    )


def deal(ledger, value="1000000", override=None):
    return ledger.add_deal("D-1", "CLIENT-A", "2026-01-01", value, override_rate=override)


def values(ledger):
    return [r["amount_cents"] for r in ledger.calculate()["entries"]]


def test_manual_flat_three_percent(ledger):
    deal(ledger, "100000")
    ledger.add_receipt("D-1", "2026-01-02", "100000")
    assert values(ledger) == [300000]  # 100,000 * 3% = 3,000.00


def test_manual_progressive_tiers_incremental(ledger):
    configure(
        ledger, {"tiers": [{"up_to": "500000", "rate": "0.03"}, {"up_to": None, "rate": "0.04"}]}
    )
    deal(ledger)
    ledger.add_receipt("D-1", "2026-01-02", "400000")
    ledger.add_receipt("D-1", "2026-02-02", "300000")
    assert values(ledger) == [1200000, 1100000]  # 400k*3%; remaining 100k*3%+200k*4%
    assert sum(values(ledger)) == 2300000


def test_manual_override_replaces_tiers(ledger):
    configure(
        ledger, {"tiers": [{"up_to": "500000", "rate": "0.03"}, {"up_to": None, "rate": "0.04"}]}
    )
    deal(ledger, "700000", "0.05")
    ledger.add_receipt("D-1", "2026-01-02", "700000")
    assert values(ledger) == [3500000]
    ledger.set_deal("D-1", clear_override=True)
    assert values(ledger) == [2300000]


def test_manual_lifetime_cap(ledger):
    configure(ledger, {"rate": "0.03", "cap": "5000"})
    deal(ledger, "300000")
    for on in ("2026-01-02", "2026-02-02", "2026-03-02"):
        ledger.add_receipt("D-1", on, "100000")
    assert values(ledger) == [300000, 200000, 0]


def test_manual_lifetime_floor_and_full_reversal(ledger):
    configure(ledger, {"rate": "0.03", "floor": "500"})
    deal(ledger)
    first = ledger.add_receipt("D-1", "2026-01-02", "5000")
    second = ledger.add_receipt("D-1", "2026-02-02", "5000")
    third = ledger.add_receipt("D-1", "2026-03-02", "20000")
    assert values(ledger) == [50000, 0, 40000]
    for row in (third, second, first):
        ledger.reverse_receipt(row["id"], "2026-04-01", confirm=True)
    assert sum(values(ledger)) == 0  # Floor vanishes when the lifetime basis returns to zero.
    assert values(ledger)[3:] == [-40000, 0, -50000]


def test_manual_withholding_and_rounding_reconciliation(ledger):
    configure(ledger, {"rate": "0.03", "withholding_rate": "0.10"})
    deal(ledger, "1")
    ledger.add_receipt("D-1", "2026-01-02", "0.50")
    ledger.add_receipt("D-1", "2026-01-03", "0.50")
    result = ledger.calculate()
    assert [r["amount_cents"] for r in result["entries"]] == [2, 1]
    assert result["totals_by_currency"]["CNY"] == {
        "gross_cents": 3,
        "withholding_cents": 0,
        "net_cents": 3,
    }
    assert cumulative(10000000, validate_rule({"rate": "0.03", "withholding_rate": "0.10"})) == (
        300000,
        30000,
        270000,
    )


def test_manual_contract_basis_and_yaml_override(ledger):
    configure(ledger, {"basis": "contract", "rate": "0.03"}, {"D-1": {"rate": "0.02"}})
    deal(ledger, "100000")
    ledger.add_receipt("D-1", "2026-02-01", "5000")
    result = ledger.calculate()
    assert [r["amount_cents"] for r in result["entries"]] == [200000]
    assert result["entries"][0]["receipt_id"] is None
    assert result["entries"][0]["date"] == "2026-01-01"
    ledger.set_deal("D-1", status="cancelled")
    assert ledger.calculate()["entries"] == []


def test_manual_tier_reversal_recalculates_downward(ledger):
    configure(
        ledger,
        {
            "tiers": [{"up_to": "500000", "rate": "0.03"}, {"up_to": None, "rate": "0.04"}],
            "withholding_rate": "0.10",
        },
    )
    deal(ledger)
    receipt = ledger.add_receipt("D-1", "2026-01-02", "700000")
    ledger.reverse_receipt(receipt["id"], "2026-02-01", "200000", confirm=True)
    result = ledger.calculate()
    assert [r["amount_cents"] for r in result["entries"]] == [2300000, -800000]
    assert [r["net_cents"] for r in result["entries"]] == [2070000, -720000]
    assert result["totals_by_currency"]["CNY"]["net_cents"] == 1350000


def test_rule_edits_history_diffs_and_idempotence(ledger):
    deal(ledger)
    receipt = ledger.add_receipt("D-1", "2026-01-02", "100000")
    first = ledger.calculate()
    assert ledger.calculate()["differences"] == []
    configure(ledger, {"rate": "0.04"})
    preview = ledger.calculate(persist=False)
    assert preview["differences"][0]["delta_net_cents"] == 100000
    with ledger.connect() as conn:
        assert conn.execute("SELECT amount_cents FROM commissions").fetchone()[0] == 300000
    result = ledger.calculate("2026-01")
    assert result["rule_id"] != first["rule_id"]
    assert result["differences"][0]["id"] == receipt["id"]
    with ledger.connect() as conn:
        assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 2
        saved = conn.execute("SELECT diffs_json FROM calculation_runs ORDER BY at DESC").fetchone()[
            0
        ]
        assert json.loads(saved)[0]["previous_net_cents"] == 300000
    assert ledger.calculate()["differences"] == []
    with pytest.raises(ValueError):
        ledger.calculate(as_of="2026-01-01")


@pytest.mark.parametrize(
    "rule",
    [
        {"rate": "NaN"},
        {"rate": "1.1"},
        {"basis": "unknown"},
        {"floor": "100", "cap": "50"},
        {"tiers": [{"up_to": "100", "rate": "0.03"}]},
        {"tiers": [{"up_to": None, "rate": "0.03"}, {"up_to": None, "rate": "0.04"}]},
        {"payout_delay_months": -1},
        {"unknown": 3},
    ],
)
def test_invalid_rules_fail_closed(rule):
    with pytest.raises(ValueError):
        validate_rule(rule)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0.001", "-0.01", True, "90000000000000.01"])
def test_money_rejects_invalid_precision_range_or_sign(value):
    with pytest.raises(ValueError):
        cents(value)


def test_decimal_exactness_and_rule_file_validation(ledger):
    assert decimal("0.1") + decimal("0.2") == Decimal("0.3")
    assert amount(-101) == "-1.01"
    assert rate("0.03") == Decimal("0.03")
    ledger.rules_path.write_text("version: 2\ndefault: {}")
    with pytest.raises(ValueError):
        load_rules(ledger.rules_path)
