"""Official FastMCP stdio adapter over the shared ledger core."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import make_ledger
from .core.forecast import alerts, cashflow
from .core.store import Ledger


def create_server(ledger: Ledger | None = None) -> FastMCP:
    store = ledger or make_ledger()
    server = FastMCP("commission-cashflow-ledger")

    @server.tool()
    def add_receipt(
        deal_code: str, date: str, amount: str, note: str = "", idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Write one actual customer collection in major currency units; use an idempotency key for retries. This does not record personal cash income."""
        return store.add_receipt(deal_code, date, amount, note, idempotency_key)

    @server.tool()
    def get_commission_summary(period: str | None = None) -> dict[str, Any]:
        """Read current-rule commission accruals for YYYY-MM or all periods. Does not persist recalculation or imply payment."""
        return store.calculate(period, persist=False)

    @server.tool()
    def get_cashflow_forecast(
        months: int = 6,
        scenario: str = "base",
        start: str | None = None,
        as_of: str | None = None,
        opening_balance: str | None = None,
    ) -> dict[str, Any]:
        """Read monthly actual cash and projected cash. Scenarios are optimistic/base/conservative. Opening balance applies before start month."""
        return cashflow(store, months, scenario, start, as_of, opening_balance)

    @server.tool()
    def list_alerts(
        months: int = 6, scenario: str = "base", as_of: str | None = None
    ) -> dict[str, Any]:
        """Read upcoming 30-day collections, overdue installments, and negative projected balances."""
        return alerts(store, months, scenario, as_of)

    @server.resource("ledger://rules")
    def rules() -> str:
        """Read the local current YAML commission rules."""
        return store.rules_path.read_text(encoding="utf-8")

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
