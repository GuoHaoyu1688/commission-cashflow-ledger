"""Local monthly Markdown, PNG and optional PDF reports."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .forecast import cashflow
from .money import add_months, amount, month_date
from .store import Ledger


def report_data(ledger: Ledger, month: str, as_of: str | None = None) -> dict[str, Any]:
    first = month_date(month)
    cutoff = date.fromisoformat(as_of) if as_of else ledger.today()
    if cutoff > ledger.today():
        raise ValueError("as_of cannot be in the future")
    commission = ledger.calculate(month, persist=False, as_of=cutoff.isoformat())
    gross = sum(
        ledger.convert(r["gross_cents"], code)
        for code, r in commission["totals_by_currency"].items()
    )
    tax = sum(
        ledger.convert(r["withholding_cents"], code)
        for code, r in commission["totals_by_currency"].items()
    )
    net = sum(
        ledger.convert(r["net_cents"], code) for code, r in commission["totals_by_currency"].items()
    )
    cash = [
        r
        for r in ledger.rows("cash_entries")
        if r["date"].startswith(month) and r["date"] <= cutoff.isoformat()
    ]
    income = sum(
        ledger.convert(r["amount_cents"], r["currency"]) for r in cash if r["kind"] == "income"
    )
    expense = sum(
        ledger.convert(r["amount_cents"], r["currency"]) for r in cash if r["kind"] == "expense"
    )
    payouts = sum(
        ledger.convert(r["amount_cents"], r["currency"])
        for r in cash
        if r["category"] == "commission"
    )
    currencies = {d["id"]: d["currency"] for d in ledger.rows("deals")}
    receipts = [r for r in ledger.rows("receipts") if r["date"] <= cutoff.isoformat()]
    collections = sum(
        ledger.convert(r["amount_cents"], currencies[r["deal_id"]])
        for r in receipts
        if r["date"].startswith(month)
    )
    cumulative_collections = sum(
        ledger.convert(r["amount_cents"], currencies[r["deal_id"]])
        for r in receipts
        if r["date"] < add_months(first, 1).isoformat()
    )
    forecast = cashflow(
        ledger, start=min(month, cutoff.strftime("%Y-%m")), as_of=cutoff.isoformat()
    )
    return {
        "month": month,
        "as_of": cutoff.isoformat(),
        "currency": ledger.base_currency,
        "gross_commission_cents": gross,
        "withholding_estimate_cents": tax,
        "net_accrual_cents": net,
        "actual_income_cents": income,
        "actual_expense_cents": expense,
        "actual_net_cents": income - expense,
        "actual_commission_paid_cents": payouts,
        "customer_collections_cents": collections,
        "cumulative_customer_collections_cents": cumulative_collections,
        "commission": commission,
        "forecast": forecast,
    }


def markdown(data: dict[str, Any]) -> str:
    currency = data["currency"]
    labels = [
        ("Customer collections", "customer_collections_cents"),
        (
            "Cumulative customer collections through month end",
            "cumulative_customer_collections_cents",
        ),
        ("Gross accrued commission", "gross_commission_cents"),
        ("Estimated flat withholding", "withholding_estimate_cents"),
        ("Net accrued commission", "net_accrual_cents"),
        ("Actual commission payouts", "actual_commission_paid_cents"),
        ("Actual personal income", "actual_income_cents"),
        ("Actual personal expenses", "actual_expense_cents"),
        ("Actual personal net cash", "actual_net_cents"),
    ]
    lines = [
        f"# Commission and cash-flow report: {data['month']}",
        "",
        f"As of {data['as_of']}. Reporting currency: {currency}. Amounts are based on local ledger records.",
        "",
        "## Recorded activity and commission accruals",
        "",
        "| Measure | Amount |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {label} | {amount(data[key])} {currency} |" for label, key in labels)
    lines += [
        "",
        "Customer collections belong to the deal. Accrued commissions are calculated entitlements; only cash entries establish actual personal income.",
        "",
        f"Rule snapshot SHA-256: `{data['commission']['rule_id']}`. Figures use the current rule file and may differ from previously saved calculations.",
        "",
        "## Six-month base scenario",
        "",
        "| Month | Actual net | Expected commission | Projected net | Closing balance |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in data["forecast"]["rows"]:
        lines.append(
            f"| {row['month']} | {amount(row['actual_net_cents'])} | {amount(row['expected_commission_cents'])} | {amount(row['projected_net_cents'])} | {amount(row['closing_balance_cents'])} |"
        )
    lines += ["", "## Assumptions", ""] + ["- " + text for text in data["forecast"]["assumptions"]]
    if data["forecast"]["warnings"]:
        lines += ["", "## Data gaps", ""] + ["- " + text for text in data["forecast"]["warnings"]]
    lines += [
        "",
        "This is a planning estimate, not a payroll statement or tax advice. No bank or company systems are connected.",
        "",
    ]
    return "\n".join(lines)


def report(
    ledger: Ledger,
    month: str,
    pdf: bool = False,
    out_dir: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    data = report_data(ledger, month, as_of)
    folder = Path(out_dir).expanduser().resolve() if out_dir else ledger.data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{month}.md"
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    # Keep floating point confined to chart coordinates; all reported arithmetic uses integer cents.
    labels = [add_months(month_date(month), i - 5).strftime("%Y-%m") for i in range(6)]
    all_events = ledger.calculate(persist=False, as_of=data["as_of"])["entries"]
    monthly = [
        sum(
            ledger.convert(e["net_cents"], e["currency"])
            for e in all_events
            if e["date"].startswith(label)
        )
        / 100
        for label in labels
    ]
    currencies = {d["id"]: d["currency"] for d in ledger.rows("deals")}
    receipts = ledger.rows("receipts")
    cumulative_receipts = [
        sum(
            ledger.convert(r["amount_cents"], currencies[r["deal_id"]])
            for r in receipts
            if r["date"]
            <= min(
                data["as_of"], (add_months(month_date(label), 1) - timedelta(days=1)).isoformat()
            )
        )
        / 100
        for label in labels
    ]
    figures = []
    chart_paths = []
    series = [
        ("monthly-commission", "Monthly net commission accrual", labels, monthly, "bar"),
        (
            "cumulative-collections",
            "Cumulative customer collections",
            labels,
            cumulative_receipts,
            "line",
        ),
        (
            "cashflow",
            "Projected personal cash balance (base)",
            [r["month"] for r in data["forecast"]["rows"]],
            [r["closing_balance_cents"] / 100 for r in data["forecast"]["rows"]],
            "line",
        ),
    ]
    try:
        for slug, title, x, y, kind in series:
            fig, ax = plt.subplots(figsize=(9, 4.2), layout="constrained")
            if kind == "bar":
                ax.bar(x, y, color="#176c5a")
            else:
                ax.plot(x, y, color="#176c5a", marker="o")
            ax.set(title=title, ylabel=data["currency"])
            ax.axhline(0, color="#94a3b8", linewidth=0.8)
            ax.grid(axis="y", alpha=0.2)
            ax.tick_params(axis="x", rotation=25)
            chart_path = folder / f"{month}-{slug}.png"
            fig.savefig(chart_path, dpi=150)
            chart_paths.append(str(chart_path))
            figures.append(fig)
        body = markdown(data)
        body += (
            "\n## Charts\n\n"
            + "\n\n".join(f"![{series[i][1]}]({Path(p).name})" for i, p in enumerate(chart_paths))
            + "\n"
        )
        path.write_text(body, encoding="utf-8")
        pdf_path = None
        if pdf:
            pdf_path = folder / f"{month}.pdf"
            with PdfPages(
                pdf_path,
                metadata={
                    "Title": f"Commission ledger {month}",
                    "Author": "Commission Cashflow Ledger",
                },
            ) as pages:
                summary, ax = plt.subplots(figsize=(8.27, 11.69))
                ax.axis("off")
                summary_lines = [
                    f"Commission and cash-flow report: {month}",
                    f"As of {data['as_of']} | {data['currency']}",
                    "",
                ]
                for label, key in [
                    ("Customer collections", "customer_collections_cents"),
                    ("Gross commission accrual", "gross_commission_cents"),
                    ("Estimated withholding", "withholding_estimate_cents"),
                    ("Net commission accrual", "net_accrual_cents"),
                    ("Actual commission payouts", "actual_commission_paid_cents"),
                    ("Actual personal income", "actual_income_cents"),
                    ("Actual personal expenses", "actual_expense_cents"),
                    ("Actual personal net cash", "actual_net_cents"),
                ]:
                    summary_lines.append(f"{label}: {amount(data[key])}")
                summary_lines += [
                    "",
                    "Opening balance applies before the first forecast month.",
                    "Actual personal cash is recorded separately from deal receipts.",
                    "Withholding is a flat estimate, not tax advice.",
                    "Forecasts are assumptions, not guaranteed payments.",
                    "See the companion Markdown for full assumptions and rule traces.",
                    "",
                    "Rule snapshot:",
                    data["commission"]["rule_id"],
                ]
                ax.text(0, 1, "\n".join(summary_lines), va="top", fontsize=10, linespacing=1.6)
                pages.savefig(summary)
                plt.close(summary)
                for fig in figures:
                    pages.savefig(fig)
        return {
            "markdown": str(path),
            "pdf": str(pdf_path) if pdf_path else None,
            "charts": chart_paths,
            "summary": {k: v for k, v in data.items() if k not in {"commission", "forecast"}},
        }
    finally:
        for fig in figures:
            plt.close(fig)
