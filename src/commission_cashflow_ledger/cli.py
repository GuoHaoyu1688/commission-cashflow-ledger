"""Noninteractive CLI with a stable JSON envelope and exit codes."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

import yaml

from . import __version__
from .config import make_ledger
from .core.forecast import alerts, cashflow
from .core.reports import report
from .core.store import Ledger, NotFound


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def parser() -> Parser:
    root = Parser(prog="ledger", description="Local commission and personal cash-flow ledger")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize local database and default rules")
    deal = commands.add_parser("deal").add_subparsers(dest="action", required=True)
    add = deal.add_parser("add")
    for key in ("code", "client-code", "signed-date", "amount"):
        add.add_argument("--" + key, required=True)
    add.add_argument("--currency", default="CNY")
    add.add_argument("--status", default="signed")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--override-rate")
    deal.add_parser("list")
    show = deal.add_parser("show")
    show.add_argument("code")
    show.add_argument("--as-of")
    update = deal.add_parser("update")
    update.add_argument("code")
    update.add_argument("--status")
    update.add_argument("--override-rate")
    update.add_argument("--clear-override", action="store_true")
    plan = (
        commands.add_parser("plan").add_subparsers(dest="action", required=True).add_parser("add")
    )
    plan.add_argument("--deal", required=True)
    plan.add_argument("--due-date", required=True)
    group = plan.add_mutually_exclusive_group(required=True)
    group.add_argument("--amount")
    group.add_argument("--proportion")
    plan.add_argument("--note", default="")
    receipt = commands.add_parser("receipt").add_subparsers(dest="action", required=True)
    add = receipt.add_parser("add")
    for key in ("deal", "date", "amount"):
        add.add_argument("--" + key, required=True)
    add.add_argument("--note", default="")
    add.add_argument("--idempotency-key")
    reverse = receipt.add_parser("reverse")
    reverse.add_argument("id")
    reverse.add_argument("--date", required=True)
    reverse.add_argument("--amount")
    reverse.add_argument("--note", default="Receipt reversal")
    reverse.add_argument("--yes", action="store_true")
    commission = commands.add_parser("commission").add_subparsers(dest="action", required=True)
    calc = commission.add_parser("calc")
    calc.add_argument("--month")
    calc.add_argument("--dry-run", action="store_true")
    commission.add_parser("history")
    recurring = commands.add_parser("recurring").add_subparsers(dest="action", required=True)
    recurring.add_parser("list")
    add = recurring.add_parser("add")
    for key in ("name", "kind", "amount", "freq", "start"):
        add.add_argument("--" + key, required=True)
    add.add_argument("--end")
    add.add_argument("--currency", default="CNY")
    cash = commands.add_parser("cash").add_subparsers(dest="action", required=True)
    cash.add_parser("list")
    add = cash.add_parser("add")
    for key in ("date", "kind", "amount"):
        add.add_argument("--" + key, required=True)
    add.add_argument("--currency", default="CNY")
    add.add_argument("--category", default="other")
    add.add_argument("--note", default="")
    add.add_argument("--deal")
    add.add_argument("--recurring-id")
    for name in ("cashflow", "alerts"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--months", type=int, default=6)
        cmd.add_argument(
            "--scenario", choices=["optimistic", "base", "conservative"], default="base"
        )
        cmd.add_argument("--as-of")
        if name == "cashflow":
            cmd.add_argument("--start")
            cmd.add_argument("--opening-balance")
    rep = commands.add_parser("report")
    rep.add_argument("--month", required=True)
    rep.add_argument("--pdf", action="store_true")
    rep.add_argument("--out-dir")
    rep.add_argument("--as-of")
    imp = commands.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--table")
    exp = commands.add_parser("export")
    exp.add_argument("--format", choices=["json", "csv"], default="json")
    exp.add_argument("--table")
    exp.add_argument("--out", required=True)
    exp.add_argument("--utf8", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--out")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8767)
    commands.add_parser("mcp")
    return root


def execute(a: argparse.Namespace, ledger: Ledger) -> Any:
    if a.command == "init":
        return {
            "database": str(ledger.path),
            "rules": str(ledger.rules_path),
            "timezone": ledger.timezone,
        }
    if a.command == "deal":
        if a.action == "list":
            return ledger.rows("deals")
        if a.action == "show":
            return ledger.show_deal(a.code, a.as_of)
        if a.action == "update":
            return ledger.set_deal(a.code, a.status, a.override_rate, a.clear_override)
        return ledger.add_deal(
            a.code,
            a.client_code,
            a.signed_date,
            a.amount,
            a.currency,
            a.status,
            a.tag,
            a.override_rate,
        )
    if a.command == "plan":
        return ledger.add_plan(a.deal, a.due_date, a.amount, a.proportion, a.note)
    if a.command == "receipt":
        if a.action == "reverse":
            return ledger.reverse_receipt(a.id, a.date, a.amount, a.note, a.yes)
        return ledger.add_receipt(a.deal, a.date, a.amount, a.note, a.idempotency_key)
    if a.command == "commission":
        if a.action == "calc":
            return ledger.calculate(a.month, persist=not a.dry_run)
        with ledger.connect() as conn:
            return [
                {**dict(row), "diffs": json.loads(row["diffs_json"])}
                for row in conn.execute("SELECT * FROM calculation_runs ORDER BY at,id")
            ]
    if a.command == "recurring":
        if a.action == "list":
            return ledger.rows("recurring")
        return ledger.add_recurring(a.name, a.kind, a.amount, a.freq, a.start, a.end, a.currency)
    if a.command == "cash":
        if a.action == "list":
            return ledger.rows("cash_entries")
        return ledger.add_cash(
            a.date, a.kind, a.amount, a.currency, a.category, a.note, a.deal, a.recurring_id
        )
    if a.command == "cashflow":
        return cashflow(ledger, a.months, a.scenario, a.start, a.as_of, a.opening_balance)
    if a.command == "alerts":
        return alerts(ledger, a.months, a.scenario, a.as_of)
    if a.command == "report":
        return report(ledger, a.month, a.pdf, a.out_dir, a.as_of)
    if a.command == "import":
        return ledger.import_file(a.path, a.table)
    if a.command == "export":
        return ledger.export(a.out, a.format, a.table, not a.utf8)
    if a.command == "backup":
        return ledger.backup(a.out)
    raise ValueError("unknown command")


def failure(code: str, message: str, status: int) -> int:
    print(
        json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False)
    )
    return status


def main(argv: list[str] | None = None) -> int:
    try:
        common = Parser(add_help=False, allow_abbrev=False)
        common.add_argument("--data-dir")
        common.add_argument("--config")
        for flag in ("json", "quiet", "version", "progress"):
            common.add_argument("--" + flag, action="store_true")
        options, remaining = common.parse_known_args(sys.argv[1:] if argv is None else argv)
        if options.version:
            print(__version__)
            return 0
        if not remaining or remaining == ["--help"]:
            parser().print_help()
            print("\nGlobal: --data-dir DIR --config YAML --json --quiet --progress --version")
            return 0
        args = parser().parse_args(remaining)
        ledger = make_ledger(options.data_dir, options.config)
        if args.command == "serve":
            import uvicorn

            from .web.app import create_app

            uvicorn.run(create_app(ledger), host=args.host, port=args.port)
            return 0
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(ledger).run(transport="stdio")
            return 0
        if options.progress:
            print(f"Running {args.command}...", file=sys.stderr)
        result = execute(args, ledger)
        if options.json or not options.quiet:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "data": result,
                        "warnings": result.get("warnings", []) if isinstance(result, dict) else [],
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except NotFound as exc:
        return failure("NOT_FOUND", str(exc), 3)
    except (
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
        ZoneInfoNotFoundError,
        sqlite3.IntegrityError,
    ) as exc:
        return failure("INVALID_ARGUMENT", str(exc), 2)
    except (OSError, sqlite3.Error) as exc:
        return failure("IO_ERROR", str(exc), 1)


if __name__ == "__main__":
    raise SystemExit(main())
