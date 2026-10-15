"""Deterministic scenarios, keeping recorded cash separate from estimates."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .money import add_months, cents, iso_date, month_date
from .rules import cumulative, load_rules, select_rule
from .store import Ledger

DELAYS = {"optimistic": 0, "base": 1, "conservative": 3}


def occurrences(item: dict[str, Any], start: date, end: date) -> list[date]:
    """Anchor every occurrence to its original day, including month ends."""
    anchor = date.fromisoformat(item["start"])
    until = min(end, date.fromisoformat(item["end"])) if item["end"] else end
    if item["freq"] == "once":
        return [anchor] if start <= anchor <= until else []
    step = 1 if item["freq"] == "monthly" else 3
    distance = (start.year - anchor.year) * 12 + start.month - anchor.month
    index = max(0, distance // step)
    result = []
    while True:
        point = add_months(anchor, index * step)
        if point > until:
            break
        if point >= start:
            result.append(point)
        index += 1
    return result


def cashflow(
    ledger: Ledger,
    months: int = 6,
    scenario: str = "base",
    start: str | None = None,
    as_of: str | None = None,
    opening_balance: str | None = None,
) -> dict[str, Any]:
    if type(months) is not int or not 1 <= months <= 120:
        raise ValueError("months must be an integer from 1 to 120")
    if scenario not in DELAYS:
        raise ValueError("scenario must be optimistic, base, or conservative")
    cutoff = date.fromisoformat(iso_date(as_of)) if as_of else ledger.today()
    if cutoff > ledger.today():
        raise ValueError("as_of cannot be in the future")
    first = month_date(start) if start else cutoff.replace(day=1)
    if first > cutoff.replace(day=1):
        raise ValueError(
            "start must be at or before the as_of month so opening balance is unambiguous"
        )
    end = add_months(first, months) - timedelta(days=1)
    opening = (
        cents(opening_balance, True) if opening_balance is not None else ledger.opening_balance
    )
    rows = {
        add_months(first, i).strftime("%Y-%m"): {
            "month": add_months(first, i).strftime("%Y-%m"),
            "actual_income_cents": 0,
            "actual_expense_cents": 0,
            "scheduled_income_cents": 0,
            "scheduled_expense_cents": 0,
            "expected_commission_cents": 0,
            "customer_collections_cents": 0,
        }
        for i in range(months)
    }
    projected_events: list[dict[str, Any]] = []
    warnings = []

    def post(on: date, field: str, total: int, currency: str, source: str) -> None:
        month = on.strftime("%Y-%m")
        converted = ledger.convert(total, currency)
        if month in rows:
            rows[month][field] += converted
            if field.startswith("scheduled") or field == "expected_commission_cents":
                projected_events.append(
                    {
                        "date": on.isoformat(),
                        "kind": field,
                        "amount_cents": converted,
                        "currency": ledger.base_currency,
                        "source": source,
                    }
                )

    cash = [r for r in ledger.rows("cash_entries") if r["date"] <= cutoff.isoformat()]
    payouts: dict[str, int] = defaultdict(int)
    matched_recurring: dict[tuple[str, str], int] = defaultdict(int)
    for item in cash:
        post(
            date.fromisoformat(item["date"]),
            "actual_" + item["kind"] + "_cents",
            item["amount_cents"],
            item["currency"],
            item["id"],
        )
        if item["category"] == "commission":
            payouts[item["deal_id"]] += item["amount_cents"]
        if item["recurring_id"]:
            matched_recurring[(item["recurring_id"], item["date"][:7])] += item["amount_cents"]
    _, _, rules = load_rules(ledger.rules_path)
    earned: dict[str, int] = defaultdict(int)
    latest: dict[str, date] = {}
    for event in ledger.calculate(persist=False, as_of=cutoff.isoformat())["entries"]:
        earned[event["deal_id"]] += event["net_cents"]
        latest[event["deal_id"]] = max(
            latest.get(event["deal_id"], date.min), date.fromisoformat(event["date"])
        )
    for deal in ledger.rows("deals"):
        detail = ledger.show_deal(deal["id"], cutoff.isoformat())
        for receipt in detail["receipts"]:
            post(
                date.fromisoformat(receipt["date"]),
                "customer_collections_cents",
                receipt["amount_cents"],
                deal["currency"],
                receipt["id"],
            )
        rule = select_rule(rules, deal["code"], deal["override_rate"])
        unpaid = earned[deal["id"]] - payouts[deal["id"]]
        if unpaid < 0:
            warnings.append(
                f"{deal['code']}: recorded commission payouts exceed current net accrual by {-unpaid} {deal['currency']} cents; no automatic cash clawback"
            )
        if unpaid > 0:
            on = max(cutoff, add_months(latest[deal["id"]], rule["payout_delay_months"]))
            post(
                on,
                "expected_commission_cents",
                unpaid,
                deal["currency"],
                "unpaid accrual:" + deal["code"],
            )
        if deal["status"] in {"negotiating", "cancelled"}:
            continue
        if rule["basis"] == "receipts":
            basis = detail["received_cents"]
            previous = cumulative(basis, rule)[2]
            advance = max(0, -unpaid)
            for plan in detail["plans"]:
                if not plan["remaining_cents"]:
                    continue
                basis += plan["remaining_cents"]
                target = cumulative(basis, rule)[2]
                collection_day = max(
                    cutoff, add_months(date.fromisoformat(plan["due_date"]), DELAYS[scenario])
                )
                payout_day = add_months(collection_day, rule["payout_delay_months"])
                delta = target - previous
                offset = min(advance, max(0, delta))
                advance -= offset
                post(
                    payout_day,
                    "expected_commission_cents",
                    delta - offset,
                    deal["currency"],
                    "plan:" + plan["id"],
                )
                previous = target
        unplanned = detail["outstanding_cents"] - sum(p["remaining_cents"] for p in detail["plans"])
        if unplanned > 0:
            warnings.append(
                f"{deal['code']}: {unplanned} {deal['currency']} cents have no remaining collection plan; excluded from collection-based forecast"
            )
    for item in ledger.rows("recurring"):
        for on in occurrences(item, first, end):
            # Historical months contain recorded cash only. Unpaid current-month schedules remain estimates.
            if on.strftime("%Y-%m") < cutoff.strftime("%Y-%m"):
                continue
            remaining = max(
                0, item["amount_cents"] - matched_recurring[(item["id"], on.strftime("%Y-%m"))]
            )
            if remaining:
                post(
                    max(on, cutoff),
                    "scheduled_" + item["kind"] + "_cents",
                    remaining,
                    item["currency"],
                    "recurring:" + item["id"],
                )
    balance = opening
    for row in rows.values():
        row["actual_net_cents"] = row["actual_income_cents"] - row["actual_expense_cents"]
        row["projected_income_cents"] = (
            row["actual_income_cents"]
            + row["scheduled_income_cents"]
            + row["expected_commission_cents"]
        )
        row["projected_expense_cents"] = (
            row["actual_expense_cents"] + row["scheduled_expense_cents"]
        )
        row["projected_net_cents"] = row["projected_income_cents"] - row["projected_expense_cents"]
        balance += row["projected_net_cents"]
        row["closing_balance_cents"] = balance
    return {
        "as_of": cutoff.isoformat(),
        "start": first.strftime("%Y-%m"),
        "months": months,
        "scenario": scenario,
        "currency": ledger.base_currency,
        "opening_balance_cents": opening,
        "rows": list(rows.values()),
        "projected_events": projected_events,
        "warnings": warnings,
        "fx": {
            "as_of": ledger.fx_as_of,
            "base_currency": ledger.base_currency,
            "rates": {k: str(v) for k, v in ledger.fx_rates.items()},
        },
        "assumptions": [
            "Opening balance is the balance immediately before the first displayed month.",
            "Actual personal cash comes only from cash_entries; customer receipts and accruals are not cash income.",
            "Unpaid net accrual is expected at the later of as_of or its last accrual date plus payout delay; advance payouts offset subsequent planned commission before additional expected cash.",
            "Collection plans use FIFO allocation and scenario delays of 0/1/3 months; overdue projections roll forward to as_of.",
            "Payout delays are whole months. Current-month unpaid recurring estimates roll forward to as_of; earlier months show recorded cash only.",
            "Link recurring cash in the occurrence month to offset its estimate; cross-month settlements require a manual schedule adjustment.",
            "Withholding is a configurable flat estimate, not a tax calculation. Manual FX is fixed throughout the forecast.",
        ],
    }


def alerts(
    ledger: Ledger, months: int = 6, scenario: str = "base", as_of: str | None = None
) -> dict[str, Any]:
    cutoff = date.fromisoformat(iso_date(as_of)) if as_of else ledger.today()
    upcoming, overdue = [], []
    for deal in ledger.rows("deals"):
        if deal["status"] in {"negotiating", "cancelled"}:
            continue
        for plan in ledger.show_deal(deal["id"], cutoff.isoformat())["plans"]:
            item = {**plan, "deal_code": deal["code"], "currency": deal["currency"]}
            if plan["remaining_cents"] and plan["due_date"] < cutoff.isoformat():
                overdue.append(item)
            elif (
                plan["remaining_cents"]
                and plan["due_date"] <= (cutoff + timedelta(days=30)).isoformat()
            ):
                upcoming.append(item)
    forecast = cashflow(ledger, months, scenario, as_of=as_of)
    return {
        "as_of": cutoff.isoformat(),
        "upcoming_30_days": upcoming,
        "overdue": overdue,
        "negative_balance_months": [
            row for row in forecast["rows"] if row["closing_balance_cents"] < 0
        ],
        "warnings": forecast["warnings"],
        "currency": ledger.base_currency,
    }
