# Validation record

Local acceptance on 2026-10-15 used Python 3.12.13, macOS, and a project-specific virtual environment. Ubuntu Python 3.11/3.12 checks are configured in CI; this record does not claim a remote CI run or GitHub publication.

- `ruff check .`: passed.
- `ruff format --check .`: passed.
- `pytest -q --cov=commission_cashflow_ledger --cov-report=term-missing`: **51 passed, 95% coverage**. One upstream Starlette warning deprecates the installed httpx TestClient integration; no test failures.
- The exact three README quick-start commands were executed against `data/demo`: 13 records imported; gross September commission 2,050,000 cents, estimated withholding 205,000 cents, net accrual 1,845,000 cents; actual net personal cash 400,000 cents.
- The optional PDF has four pages. Text extraction confirmed the expected numbers; all four rendered pages were visually reviewed. Markdown references three existing PNG charts.
- An independent wheel build succeeded. The wheel contains default YAML rules and all HTML/CSS/JS assets, with no user database files.
- Official MCP in-memory tests cover all four tools, error handling, and `ledger://rules`. An actual subprocess stdio handshake and tool invocation also pass.
- SQLite tests validate concurrent overpayment protection, atomic rollback on invalid imports, repeat imports, JSON/CSV round trips, linked full/partial reversals, dated backup integrity, and exact rule snapshots/history.

## Browser checks

Headless Google Chrome via the available Playwright installation, desktop 1440×1000 and phone 390×844, with only fictional fixtures:

- Search customer/deal codes and open the collection timeline.
- Set the as-of date, first month, and opening balance; verify the sample 22,450.00 CNY projected closing balance.
- Switch collection-delay scenarios and verify updated forecast data.
- Preview the September report with 20,500.00 CNY gross commission.
- Submit a customer receipt and verify the timeline/collection total changes while personal cash remains separate.
- Reopen a deal at mobile size; verify no document-level horizontal overflow.
- Inspect full desktop/mobile screenshots and the mobile chart. No console errors, page errors, or external network requests were observed.

Screenshots: [desktop](dashboard.png), [mobile](mobile.png). The mobile tables scroll horizontally inside their panels. Browser-native date controls may use the operating system's numeric date order; month fields use explicit YYYY-MM text.

## Boundaries

The application supports local single-user planning. It does not authenticate remote users, connect to banks or payroll, retrieve FX, calculate jurisdiction-specific tax, or guarantee payment timing. Receipt allocation is FIFO by installment, tiers/caps/floors are lifetime per deal, unpaid commission timing uses the documented aggregate assumption, and recurring cash offsets match the occurrence month only. Amounts use two-decimal currencies. Recalculation intentionally restates history under the current rules while retaining old rule snapshots and differences. Backup restoration is a documented stop/extract/reconfigure procedure.
