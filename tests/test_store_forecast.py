import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from pypdf import PdfReader

from commission_cashflow_ledger.core.forecast import alerts, cashflow, occurrences
from commission_cashflow_ledger.core.reports import markdown, report, report_data
from commission_cashflow_ledger.core.store import Ledger


def test_payment_allocation_overdue_and_overpayment_rejected(ledger):
    ledger.add_deal("D-1", "CLIENT-A", "2026-01-01", "1000")
    ledger.add_plan("D-1", "2026-09-01", proportion="0.4")
    ledger.add_plan("D-1", "2026-10-20", value="600")
    ledger.add_receipt("D-1", "2026-09-10", "500", idempotency_key="receipt-1")
    assert ledger.add_receipt("D-1", "2026-09-10", "500", idempotency_key="receipt-1")[
        "deduplicated"
    ]
    detail = ledger.show_deal("D-1")
    assert [p["remaining_cents"] for p in detail["plans"]] == [0, 50000]
    assert not any(p["overdue"] for p in detail["plans"])
    with pytest.raises(ValueError, match="overpayments"):
        ledger.add_receipt("D-1", "2026-09-10", "500.01")
    with pytest.raises(ValueError):
        ledger.add_receipt("D-1", "2026-09-10", "499", idempotency_key="receipt-1")
    with pytest.raises(ValueError):
        ledger.add_plan("D-1", "2026-10-20", value="1")
    with pytest.raises(ValueError):
        ledger.reverse_receipt("receipt-1", "2026-09-12")
    ledger.reverse_receipt("receipt-1", "2026-09-12", "200", confirm=True)
    detail = ledger.show_deal("D-1")
    assert detail["plans"][0]["overdue"] and detail["plans"][0]["remaining_cents"] == 10000
    with pytest.raises(ValueError):
        ledger.reverse_receipt("receipt-1", "2026-09-12", "301", confirm=True)
    with pytest.raises(ValueError):
        ledger.reverse_receipt("receipt-1", "2026-09-09", "1", confirm=True)
    alert = alerts(ledger)
    assert len(alert["overdue"]) == len(alert["upcoming_30_days"]) == 1


def test_invalid_future_amount_and_status(ledger):
    with pytest.raises(ValueError):
        ledger.add_deal("FUTURE", "A", "2027-01-01", "100")
    ledger.add_deal("D-1", "A", "2026-01-01", "100")
    with pytest.raises(ValueError):
        ledger.add_receipt("D-1", "2027-01-01", "10")
    with pytest.raises(ValueError):
        ledger.add_cash("2027-01-01", "income", "10")
    ledger.set_deal("D-1", status="cancelled")
    with pytest.raises(ValueError):
        ledger.add_receipt("D-1", "2026-01-01", "10")
    with pytest.raises(ValueError):
        ledger.add_cash("2026-01-01", "income", "10", category="commission")


def test_concurrent_receipts_enforce_contract_total(ledger):
    ledger.add_deal("D-1", "A", "2026-01-01", "100")

    def collect(i):
        try:
            ledger.add_receipt("D-1", "2026-01-02", "30", idempotency_key=str(i))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(collect, range(6)))
    assert sum(results) == 3
    assert ledger.show_deal("D-1")["received_cents"] == 9000


@pytest.mark.parametrize("format", ["json", "csv"])
def test_import_export_roundtrip_dedupe_backup(demo, tmp_path, format):
    file = tmp_path / ("export." + format)
    assert demo.export(file, format)["count"] == 13
    if format == "csv":
        assert file.read_bytes().startswith(b"\xef\xbb\xbf")
    restored = Ledger(tmp_path / "restored")
    assert restored.import_file(file)["inserted"] == 13
    assert restored.import_file(file) == {"inserted": 0, "skipped": 13}
    assert restored.show_deal("D-2026-002")["received_cents"] == 60000000
    backup = demo.backup()
    with zipfile.ZipFile(backup["path"]) as archive:
        assert set(archive.namelist()) == {"ledger.db", "rules.yaml", "settings.json"}
        archive.extract("ledger.db", tmp_path / "backup")
    with sqlite3.connect(tmp_path / "backup/ledger.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2
    with pytest.raises(ValueError):
        demo.backup(demo.path)
    with pytest.raises(ValueError):
        demo.export(demo.rules_path)


def test_atomic_import_and_code_reference_retry(ledger, tmp_path):
    records = [
        {
            "record_type": "deals",
            "code": "D-1",
            "client_code": "A",
            "signed_date": "2026-01-01",
            "amount": "100",
        },
        {"record_type": "receipts", "deal_id": "D-1", "date": "2026-01-02", "amount": "50"},
    ]
    path = tmp_path / "records.json"
    path.write_text(json.dumps(records))
    assert ledger.import_file(path)["inserted"] == 2
    assert ledger.import_file(path)["skipped"] == 2
    records = [
        {
            "record_type": "deals",
            "code": "D-2",
            "client_code": "A",
            "signed_date": "2026-01-01",
            "amount": "100",
        },
        {"record_type": "receipts", "deal_id": "D-2", "date": "2026-01-02", "amount": "101"},
    ]
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError):
        ledger.import_file(path)
    assert len(ledger.rows("deals")) == 1


def test_three_scenarios_and_actual_cash_separation(ledger):
    ledger.add_deal("D-1", "A", "2026-01-01", "100000")
    ledger.add_plan("D-1", "2026-10-25", value="100000")
    for scenario, index in (("optimistic", 0), ("base", 1), ("conservative", 3)):
        result = cashflow(ledger, scenario=scenario)
        assert [r["expected_commission_cents"] for r in result["rows"]] == [
            300000 if i == index else 0 for i in range(6)
        ]
        assert all(r["actual_income_cents"] == 0 for r in result["rows"])
    ledger.add_receipt("D-1", "2026-10-01", "100000")
    assert cashflow(ledger)["rows"][0]["actual_income_cents"] == 0
    assert cashflow(ledger)["rows"][0]["expected_commission_cents"] == 300000
    ledger.add_cash("2026-10-02", "income", "1000", category="commission", deal_code="D-1")
    row = cashflow(ledger)["rows"][0]
    assert row["actual_income_cents"] == 100000 and row["expected_commission_cents"] == 200000
    assert row["closing_balance_cents"] == 300000


def test_advance_payout_and_reversal_are_not_double_counted(ledger):
    ledger.add_deal("D-1", "A", "2026-01-01", "100000")
    ledger.add_plan("D-1", "2026-11-01", value="100000")
    ledger.add_cash("2026-10-01", "income", "1000", category="commission", deal_code="D-1")
    result = cashflow(ledger, scenario="optimistic")
    assert result["warnings"] and result["rows"][1]["expected_commission_cents"] == 200000
    receipt = ledger.add_receipt("D-1", "2026-10-01", "100000")
    ledger.reverse_receipt(receipt["id"], "2026-10-02", confirm=True)
    result = cashflow(ledger, scenario="optimistic")
    assert result["rows"][0]["actual_income_cents"] == 100000
    assert result["rows"][1]["expected_commission_cents"] == 200000


def test_month_end_recurring_offset_and_negative_alert(ledger):
    item = ledger.add_recurring("Rent", "expense", "1000", "monthly", "2026-01-31")
    assert occurrences(item, date(2026, 1, 1), date(2026, 3, 31)) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
    ]
    quarter = {**item, "freq": "quarterly"}
    assert occurrences(quarter, date(2026, 1, 1), date(2026, 7, 31)) == [
        date(2026, 1, 31),
        date(2026, 4, 30),
        date(2026, 7, 31),
    ]
    ledger.add_cash("2026-10-01", "expense", "400", recurring_id=item["id"])
    row = cashflow(ledger)["rows"][0]
    assert row["actual_expense_cents"] == 40000 and row["scheduled_expense_cents"] == 60000
    assert row["closing_balance_cents"] == -100000
    assert len(alerts(ledger)["negative_balance_months"]) == 6
    historical = cashflow(ledger, start="2026-09")["rows"][0]
    assert historical["scheduled_expense_cents"] == 0


def test_manual_fx_requirements(tmp_path):
    ledger = Ledger(tmp_path / "fx")
    ledger.add_cash("2026-10-01", "income", "100", currency="USD")
    with pytest.raises(ValueError, match="missing manual"):
        cashflow(ledger)
    ledger.fx_rates["USD"] = 7
    with pytest.raises(ValueError, match="fx_as_of"):
        cashflow(ledger)
    ledger.fx_as_of = "2026-10-01"
    assert cashflow(ledger)["rows"][0]["actual_income_cents"] == 70000


def test_sample_report_exact_expected_numbers_and_pdf(demo, tmp_path):
    result = report_data(demo, "2026-09", "2026-09-30")
    assert result["customer_collections_cents"] == 65000000
    assert result["gross_commission_cents"] == 2050000  # 50k*3% + 500k*3% + 100k*4%
    assert result["withholding_estimate_cents"] == 205000
    assert result["net_accrual_cents"] == 1845000
    assert result["actual_commission_paid_cents"] == 100000
    assert result["actual_income_cents"] == 600000
    assert result["actual_expense_cents"] == 200000
    assert result["actual_net_cents"] == 400000
    assert result["forecast"]["rows"][0]["closing_balance_cents"] == 2245000
    assert "20500.00 CNY" in markdown(result)
    output = report(demo, "2026-09", pdf=True, out_dir=tmp_path / "reports", as_of="2026-09-30")
    assert len(output["charts"]) == 3
    document = PdfReader(output["pdf"])
    assert len(document.pages) == 4
    assert "20500.00" in document.pages[0].extract_text()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"months": 0},
        {"months": 121},
        {"scenario": "wrong"},
        {"as_of": "2027-01-01"},
        {"start": "2027-01"},
    ],
)
def test_forecast_validation(ledger, kwargs):
    with pytest.raises(ValueError):
        cashflow(ledger, **kwargs)


def test_once_end_dates_payout_delay_and_unplanned_warning(ledger):
    once = ledger.add_recurring("One-time allowance", "income", "100", "once", "2026-10-20")
    limited = ledger.add_recurring(
        "Limited rent", "expense", "50", "monthly", "2026-10-15", "2026-11-15"
    )
    assert occurrences(once, date(2026, 10, 1), date(2027, 1, 1)) == [date(2026, 10, 20)]
    assert occurrences(limited, date(2026, 10, 1), date(2027, 1, 1)) == [
        date(2026, 10, 15),
        date(2026, 11, 15),
    ]
    ledger.add_deal("D-1", "A", "2026-01-01", "1000")
    ledger.add_plan("D-1", "2026-10-31", value="500")
    ledger.rules_path.write_text("version: 1\ndefault:\n  rate: '0.03'\n  payout_delay_months: 2\n")
    result = cashflow(ledger, scenario="optimistic")
    assert result["rows"][0]["scheduled_income_cents"] == 10000
    assert result["rows"][1]["scheduled_income_cents"] == 0
    assert result["rows"][2]["expected_commission_cents"] == 1500
    assert result["projected_events"][-1]["date"] == "2026-11-15"
    assert any("50000 CNY cents have no remaining collection plan" in w for w in result["warnings"])
    assert any(
        e["date"] == "2026-12-31"
        for e in result["projected_events"]
        if e["kind"] == "expected_commission_cents"
    )
