# Headless agent operations

This local SQLite ledger records fictional or user-provided deals, customer collections, commission accruals, and separate actual personal cash. Forecasts support collection delays and recurring schedules. It needs no graphical session or runtime internet connection.

## Install on Linux

Python 3.11 or newer is required. Use the machine's installed Python 3.11/3.12; no OCR or PDF system tools are required.

```bash
git clone https://github.com/GuoHaoyu1688/commission-cashflow-ledger.git
cd commission-cashflow-ledger
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export LEDGER_DATA_DIR=/workspace/data/commission-cashflow-ledger
ledger init --json
```

The path above is an example deployment location, not hardcoded. `LEDGER_DATA_DIR` or `--data-dir` controls storage. `LEDGER_CONFIG` or `--config` selects a YAML settings file. The database is `<data-dir>/ledger.db`; rules default to `<data-dir>/rules.yaml`; reports and dated backups use `<data-dir>/reports/` and `<data-dir>/backups/`. Config-relative paths resolve next to the YAML. Default timezone is Asia/Shanghai. Base currency defaults to CNY. Manual FX requires both positive rates and `fx_as_of`.

## Invocation contract

Commands never prompt. Global `--json`, `--quiet`, `--progress`, `--version`, `--config`, and `--data-dir` work before or after subcommands. JSON writes exactly one object to stdout; progress goes to stderr. `--quiet` suppresses success output unless `--json` is also passed. MCP uses stdout exclusively for protocol traffic. Exit status is 0 success, 1 I/O or database error, 2 argument/validation/conflict, 3 missing record.

Actual empty-ledger queries:

```bash
ledger deal list --json
# {"ok": true, "data": [], "warnings": []}
ledger commission history --json
# {"ok": true, "data": [], "warnings": []}
ledger deal show MISSING --json
# {"ok": false, "error": {"code": "NOT_FOUND", "message": "deal not found"}}
```

The last command exits 3. Numeric output keys ending in `_cents` contain integers. Amount/rate inputs are decimal strings; never perform accounting arithmetic with binary floats. A `receipt` is company/customer money; it is not personal income. An accrual is not proof of payout. Querying summaries with MCP is read-only; `commission calc` persists a fresh reconciliation unless `--dry-run` is passed.

## Command map

| Intent | Command |
| --- | --- |
| Initialize | `ledger init --json` |
| Add contract | `ledger deal add --code D-2026-003 --client-code CLIENT-C --signed-date 2026-09-01 --amount 100000 --currency CNY --status signed --tag fictional --json` |
| List contracts | `ledger deal list --json` |
| Collection timeline | `ledger deal show D-2026-003 --as-of 2026-09-30 --json` |
| Change status/rate | `ledger deal update D-2026-003 --status active --override-rate 0.04 --json`; `--clear-override` restores YAML selection |
| Plan installment | `ledger plan add --deal D-2026-003 --due-date 2026-11-01 --proportion 0.5 --json`; alternatively `--amount 50000` |
| Record collection | `ledger receipt add --deal D-2026-003 --date 2026-09-10 --amount 10000 --note 'Fictional collection' --idempotency-key receipt-fictional-1 --json` |
| Reverse collection | `ledger receipt reverse RECEIPT_ID --date 2026-09-11 --amount 1000 --yes --json`; omit amount for remaining full reversal |
| Calculate commissions | `ledger commission calc --month 2026-09 --json`; add `--dry-run` to preview differences |
| Inspect recalculations | `ledger commission history --json` |
| Create recurring schedule | `ledger recurring add --name 'Fictional rent' --kind expense --amount 2000 --freq monthly --start 2026-09-05 --currency CNY --json`; optional `--end`; also quarterly/once |
| Inspect schedules | `ledger recurring list --json` |
| Record actual personal cash | `ledger cash add --date 2026-09-25 --kind income --amount 1000 --currency CNY --category commission --deal D-2026-003 --json` |
| Link scheduled cash | `ledger cash add --date 2026-09-05 --kind expense --amount 2000 --category rent --recurring-id SCHEDULE_ID --json` |
| Inspect actual cash | `ledger cash list --json` |
| Forecast | `ledger cashflow --months 6 --scenario conservative --as-of 2026-09-30 --start 2026-09 --opening-balance 1000 --json` |
| Upcoming/late/negative alerts | `ledger alerts --as-of 2026-09-30 --json` |
| Generate report | `ledger report --month 2026-09 --as-of 2026-09-30 --pdf --json`; optional `--out-dir` |
| Import | `ledger import records.json --json`; CSV also supported, with optional `--table receipts` |
| Export | `ledger export --format csv --table receipts --out receipts.csv --json`; omit table for all primary tables; `--utf8` omits BOM |
| Backup | `ledger backup --json`; optional `--out /safe/path/ledger_2026-09-30.zip` |
| Web/API | `ledger serve --host 127.0.0.1 --port 8767` |
| MCP | `ledger mcp` |

Imports use `record_type` and the fields described in README; amounts are either integer `amount_cents` or decimal string `amount`. Table names are `deals`, `payment_plans`, `receipts`, `recurring`, `cash_entries`. The included mixed JSON demonstrates every type. Import supplies related rows in dependency order automatically and rolls back the entire batch on an error. Exports contain primary records; backup preserves derived commissions and historical rules too.

## Idempotency and data safety

Use `--idempotency-key` for receipt retries. The same key with the same data returns the existing record with `deduplicated: true`; different content is rejected. Imports deduplicate IDs (missing IDs get content hashes) and reject changes to an existing ID. Ordinary `add` commands are not retry-safe. Re-running `commission calc` with unchanged records/rules produces an empty difference list; it does not add duplicate accruals. Recurring entries are planned estimates, never automatically posted cash.

Before rule changes, status changes, reversals, restores, or overwriting report/export paths, run `ledger backup --json` and retain the returned path. Reversals require `--yes` and preserve their original receipt. There are no hard-delete commands. Recalculate after a reversal to persist the corrected accruals. Cancellation suppresses contract-basis accruals and future plans but preserves customer receipts and actual cash. The database uses WAL, foreign keys, a busy timeout, and immediate write transactions to prevent concurrent overpayment races.

Do not replace a live database. Restore a ZIP to a fresh directory after stopping processes, supply a matching config for its settings/rules, then run `ledger deal list --json`. Backup settings contain decimal strings. CSV can contain spreadsheet-like user text: import identifier columns as text in spreadsheet software. The Web server is loopback-only and lacks user authentication; do not proxy it to an untrusted network.

## MCP client setup

```json
{
  "mcpServers": {
    "commission-ledger": {
      "command": "/workspace/apps/commission-cashflow-ledger/.venv/bin/ledger",
      "args": ["--data-dir", "/workspace/data/commission-cashflow-ledger", "mcp"],
      "env": {"LEDGER_CONFIG": "/workspace/data/commission-cashflow-ledger/settings.yaml"}
    }
  }
}
```

Omit `LEDGER_CONFIG` until that file exists. `python -m commission_cashflow_ledger.mcp_server` is an alternative with environment-based configuration.

| Tool/resource | Parameter schema and effect |
| --- | --- |
| `add_receipt` | Required strings `deal_code`, `date` (ISO date), `amount` (positive major units); optional string `note=""`, nullable string `idempotency_key=null`. Writes actual customer receipt, not personal cash. |
| `get_commission_summary` | Optional nullable string `period=null` (`YYYY-MM`); read-only current-rule accruals and differences. |
| `get_cashflow_forecast` | Integer `months=6` (1–120), string `scenario="base"` (optimistic/base/conservative), nullable strings `start`, `as_of`, `opening_balance`. Read-only. |
| `list_alerts` | Integer `months=6`, string `scenario="base"`, nullable string `as_of`; read-only upcoming, overdue and negative-balance months. |
| `ledger://rules` | Read-only YAML resource; no parameters. |

Tools return JSON objects directly as MCP structured content, with integer monetary values. Invalid inputs are MCP errors. Every tool and the resource are tested with the official in-memory client; a real stdio handshake is also tested.

## Task recipes

1. **“Record this customer payment.”** Find the deal with `deal list`/`deal show`, record `receipt add` with an external retry key, and run `commission calc --month MONTH --json`. Do not record personal income unless the user supplies a separate actual payout.
2. **“We changed the commission rate; what changes historically?”** Back up, edit the YAML, run `commission calc --dry-run --json`, inspect `differences`, and then run `commission calc --json` when the change is within the user's authorized request. Saved snapshot hashes identify both rules.
3. **“Can I cover the next six months?”** Verify the opening balance and its month, actual cash, schedules, and remaining collection plans. Run all three `cashflow --scenario ... --json` commands plus `alerts --json`. Separate estimates from recorded cash and identify missing plans/FX.
4. **“The customer payment was reversed.”** Back up, obtain the original receipt ID, use `receipt reverse ID --date DATE --yes --json`, and run `commission calc --json`. Inspect any excess-payout warning. Never auto-reverse actual personal cash.
5. **“Create a monthly report.”** Use `report --month MONTH --as-of DATE --pdf --json`. Verify the returned summary against `commission calc --month MONTH --dry-run --json` and the actual cash list. Share the generated Markdown, its three PNGs, and PDF together.

## Checks

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q --cov=commission_cashflow_ledger --cov-report=term-missing
```

No network access is needed for tests after dependencies are installed. Use temporary directories for tests; do not mutate user data. Manual cases cover flat rates, progressive tiers, overrides, lifetime caps/floors, cumulative rounding, withholding, contract basis, and receipt reversal. Sample report figures are independently asserted. See `docs/VALIDATION.md` for observed release checks and limitations.
