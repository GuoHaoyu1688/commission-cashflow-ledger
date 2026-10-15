"""SQLite deals, receipts, cash movements and immutable calculation snapshots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .money import amount, cents, decimal, iso_date, rate, rounded
from .rules import cumulative, load_rules, select_rule

STATUSES = {"negotiating", "signed", "active", "settled", "cancelled"}
TABLES = ("deals", "payment_plans", "receipts", "recurring", "cash_entries")
SCHEMA = """
CREATE TABLE IF NOT EXISTS deals(id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL,
 client_code TEXT NOT NULL, signed_date TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 currency TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, override_rate TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment_plans(id TEXT PRIMARY KEY, deal_id TEXT NOT NULL REFERENCES deals(id),
 due_date TEXT NOT NULL, amount_cents INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, deal_id TEXT NOT NULL REFERENCES deals(id),
 date TEXT NOT NULL, amount_cents INTEGER NOT NULL, note TEXT NOT NULL, reverses TEXT REFERENCES receipts(id),
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recurring(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
 amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, freq TEXT NOT NULL, start TEXT NOT NULL,
 end TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cash_entries(id TEXT PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL,
 amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, category TEXT NOT NULL, note TEXT NOT NULL,
 deal_id TEXT REFERENCES deals(id), recurring_id TEXT REFERENCES recurring(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, yaml TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commissions(id TEXT PRIMARY KEY, deal_id TEXT NOT NULL REFERENCES deals(id),
 receipt_id TEXT REFERENCES receipts(id), rule_id TEXT NOT NULL REFERENCES rules(id), date TEXT NOT NULL,
 basis_cents INTEGER NOT NULL, rate TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 withholding_cents INTEGER NOT NULL, net_cents INTEGER NOT NULL, currency TEXT NOT NULL, trace_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calculation_runs(id TEXT PRIMARY KEY, rule_id TEXT NOT NULL,
 at TEXT NOT NULL, diffs_json TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class NotFound(LookupError):
    pass


class Ledger:
    def __init__(
        self,
        data_dir: str | Path = "data",
        rules_path: str | Path | None = None,
        timezone: str = "Asia/Shanghai",
        base_currency: str = "CNY",
        fx_rates: dict[str, Any] | None = None,
        fx_as_of: str | None = None,
        opening_balance: str = "0",
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "ledger.db"
        self.rules_path = (
            Path(rules_path).expanduser().resolve() if rules_path else self.data_dir / "rules.yaml"
        )
        if not self.rules_path.exists():
            self.rules_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(__file__).parents[1] / "default_rules.yaml", self.rules_path)
        self.timezone = timezone
        ZoneInfo(timezone)
        self.base_currency = self.currency(base_currency)
        self.fx_rates = {self.base_currency: decimal("1")}
        for code, value in (fx_rates or {}).items():
            factor = decimal(value)
            if factor <= 0:
                raise ValueError("manual FX rates must be positive")
            self.fx_rates[self.currency(code)] = factor
        if self.fx_rates[self.base_currency] != 1:
            raise ValueError("base currency FX rate must equal 1")
        self.fx_as_of = iso_date(fx_as_of) if fx_as_of else None
        self.opening_balance = cents(opening_balance, True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @staticmethod
    def currency(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be an uppercase three-letter code")
        return value

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()

    def convert(self, value: int, currency: str) -> int:
        if currency not in self.fx_rates:
            raise ValueError(f"missing manual FX rate for {currency} to {self.base_currency}")
        if currency != self.base_currency and not self.fx_as_of:
            raise ValueError(
                "manual foreign-exchange conversion requires a configured fx_as_of date"
            )
        return rounded(decimal(value) * self.fx_rates[currency])

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        if "tags_json" in result:
            result["tags"] = json.loads(result.pop("tags_json"))
        if "trace_json" in result:
            result["trace"] = json.loads(result.pop("trace_json"))
        return result

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError("unknown export table")
        with self.connect() as conn:
            return [
                self._decode(row)
                for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY created_at,id')
            ]

    def _deal(self, conn: sqlite3.Connection, code: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM deals WHERE code=? OR id=?", (code, code)).fetchone()
        if row is None:
            raise NotFound("deal not found")
        return self._decode(row)

    def _insert(self, conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Validate every imported and interactive record through the same path."""
        row = dict(data)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", now())
        if not isinstance(row["id"], str) or not row["id"]:
            raise ValueError("id must be a non-empty string")
        if (
            type(row.get("amount_cents")) is not int
            or abs(row["amount_cents"]) > 9_000_000_000_000_000
        ):
            raise ValueError("amount_cents must be an integer within the supported range")
        if table == "deals":
            for key in ("code", "client_code"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError(f"{key} must be a non-empty string")
            row["signed_date"] = iso_date(row["signed_date"])
            row["currency"] = self.currency(row.get("currency", self.base_currency))
            row.setdefault("status", "signed")
            if row["status"] != "negotiating" and row["signed_date"] > self.today().isoformat():
                raise ValueError("signed contracts cannot have a future signature date")
            if row["status"] not in STATUSES:
                raise ValueError("unsupported deal status")
            tags = row.pop("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise ValueError("tags must be an array of strings")
            row["tags_json"] = json.dumps(tags, ensure_ascii=False)
            row["override_rate"] = (
                str(rate(row["override_rate"])) if row.get("override_rate") is not None else None
            )
        elif table == "payment_plans":
            deal = self._deal(conn, row["deal_id"])
            row["deal_id"] = deal["id"]
            row["due_date"] = iso_date(row["due_date"])
            row.setdefault("note", "")
            existing = conn.execute(
                "SELECT coalesce(sum(amount_cents),0) FROM payment_plans WHERE deal_id=?",
                (deal["id"],),
            ).fetchone()[0]
            if existing + row["amount_cents"] > deal["amount_cents"]:
                raise ValueError("payment plan total exceeds contract amount")
        elif table == "receipts":
            deal = self._deal(conn, row["deal_id"])
            row["deal_id"] = deal["id"]
            row["date"] = iso_date(row["date"])
            if row["date"] > self.today().isoformat():
                raise ValueError("actual receipt dates cannot be in the future; use a payment plan")
            row.setdefault("note", "")
            row.setdefault("reverses", None)
            if row["amount_cents"] < 0:
                original = conn.execute(
                    "SELECT * FROM receipts WHERE id=?", (row["reverses"],)
                ).fetchone()
                if (
                    not original
                    or original["deal_id"] != deal["id"]
                    or original["amount_cents"] <= 0
                ):
                    raise ValueError(
                        "negative receipts must reverse a positive receipt of the same deal"
                    )
                already = conn.execute(
                    "SELECT coalesce(sum(amount_cents),0) FROM receipts WHERE reverses=?",
                    (original["id"],),
                ).fetchone()[0]
                if (
                    -row["amount_cents"] > original["amount_cents"] + already
                    or row["date"] < original["date"]
                ):
                    raise ValueError("reversal exceeds remaining original receipt or predates it")
            elif row["reverses"] is not None:
                raise ValueError("a reversal must have a negative amount")
            elif deal["status"] in {"negotiating", "cancelled"}:
                raise ValueError("cannot collect a negotiating or cancelled deal")
            records = [
                dict(item)
                for item in conn.execute("SELECT * FROM receipts WHERE deal_id=?", (deal["id"],))
            ]
            records.append(row)
            balance = 0
            for entry in sorted(
                records, key=lambda item: (item["date"], item["created_at"], item["id"])
            ):
                balance += entry["amount_cents"]
                if not 0 <= balance <= deal["amount_cents"]:
                    raise ValueError(
                        "receipt sequence would underflow or exceed contract amount; overpayments are rejected"
                    )
        elif table == "recurring":
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                raise ValueError("recurring name is required")
            if row.get("kind") not in {"income", "expense"} or row.get("freq") not in {
                "monthly",
                "quarterly",
                "once",
            }:
                raise ValueError("recurring requires income/expense and monthly/quarterly/once")
            row["start"] = iso_date(row["start"])
            row["end"] = iso_date(row["end"]) if row.get("end") else None
            if row["end"] and row["end"] < row["start"]:
                raise ValueError("recurring end cannot precede start")
            row["currency"] = self.currency(row.get("currency", self.base_currency))
        elif table == "cash_entries":
            row["date"] = iso_date(row["date"])
            if row["date"] > self.today().isoformat():
                raise ValueError("actual cash dates cannot be in the future; use recurring entries")
            if row.get("kind") not in {"income", "expense"}:
                raise ValueError("cash kind must be income or expense")
            row["currency"] = self.currency(row.get("currency", self.base_currency))
            row.setdefault("category", "other")
            row.setdefault("note", "")
            row.setdefault("deal_id", None)
            row.setdefault("recurring_id", None)
            if row["deal_id"]:
                deal = self._deal(conn, row["deal_id"])
                row["deal_id"] = deal["id"]
                if row["currency"] != deal["currency"]:
                    raise ValueError("commission payouts must use the deal currency")
            if row["category"] == "commission" and (not row["deal_id"] or row["kind"] != "income"):
                raise ValueError("commission cash income requires a linked deal")
            if row["recurring_id"]:
                recurring = conn.execute(
                    "SELECT * FROM recurring WHERE id=?", (row["recurring_id"],)
                ).fetchone()
                if (
                    not recurring
                    or recurring["kind"] != row["kind"]
                    or recurring["currency"] != row["currency"]
                ):
                    raise ValueError("cash recurring link must match kind and currency")
        else:
            raise ValueError("unsupported table")
        if (
            type(row.get("amount_cents")) is not int
            or abs(row["amount_cents"]) > 9_000_000_000_000_000
        ):
            raise ValueError("amount_cents must be an integer within the supported range")
        if row["amount_cents"] == 0 or (table != "receipts" and row["amount_cents"] < 0):
            raise ValueError(
                "amount must be positive (negative amounts only for receipt reversals)"
            )
        allowed = {field[1] for field in conn.execute(f'PRAGMA table_info("{table}")')}
        if set(row) - allowed:
            raise ValueError("unsupported fields: " + ", ".join(sorted(set(row) - allowed)))
        columns = ",".join('"' + key + '"' for key in row)
        conn.execute(
            f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in row)})',
            list(row.values()),
        )
        return self._decode(row)

    def add_deal(
        self,
        code: str,
        client_code: str,
        signed_date: str,
        value: str,
        currency: str = "CNY",
        status: str = "signed",
        tags: list[str] | None = None,
        override_rate: str | None = None,
    ) -> dict[str, Any]:
        with self.connect(write=True) as conn:
            return self._insert(
                conn,
                "deals",
                dict(
                    code=code,
                    client_code=client_code,
                    signed_date=signed_date,
                    amount_cents=cents(value),
                    currency=currency,
                    status=status,
                    tags=tags or [],
                    override_rate=override_rate,
                ),
            )

    def set_deal(
        self,
        code: str,
        status: str | None = None,
        override_rate: str | None = None,
        clear_override: bool = False,
    ) -> dict[str, Any]:
        with self.connect(write=True) as conn:
            deal = self._deal(conn, code)
            if status:
                if status not in STATUSES:
                    raise ValueError("unsupported status")
                if status != "negotiating" and deal["signed_date"] > self.today().isoformat():
                    raise ValueError("cannot activate a future-dated contract")
                conn.execute("UPDATE deals SET status=? WHERE id=?", (status, deal["id"]))
            if override_rate is not None or clear_override:
                conn.execute(
                    "UPDATE deals SET override_rate=? WHERE id=?",
                    (None if clear_override else str(rate(override_rate)), deal["id"]),
                )
            return self._deal(conn, code)

    def add_plan(
        self,
        deal_code: str,
        due_date: str,
        value: str | None = None,
        proportion: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if (value is None) == (proportion is None):
            raise ValueError("supply exactly one of amount or proportion")
        with self.connect(write=True) as conn:
            deal = self._deal(conn, deal_code)
            total = (
                cents(value)
                if value is not None
                else rounded(decimal(deal["amount_cents"]) * rate(proportion))
            )
            return self._insert(
                conn,
                "payment_plans",
                dict(deal_id=deal["id"], due_date=due_date, amount_cents=total, note=note),
            )

    def add_receipt(
        self,
        deal_code: str,
        on: str,
        value: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self.connect(write=True) as conn:
            deal = self._deal(conn, deal_code)
            data = dict(deal_id=deal["id"], date=on, amount_cents=cents(value), note=note)
            if idempotency_key:
                old = conn.execute(
                    "SELECT * FROM receipts WHERE id=?", (idempotency_key,)
                ).fetchone()
                if old:
                    if any(old[key] != val for key, val in data.items()):
                        raise ValueError("idempotency key already refers to different receipt data")
                    return {**self._decode(old), "deduplicated": True}
                data["id"] = idempotency_key
            return self._insert(conn, "receipts", data)

    def reverse_receipt(
        self,
        receipt_id: str,
        on: str,
        value: str | None = None,
        note: str = "Receipt reversal",
        confirm: bool = False,
    ) -> dict[str, Any]:
        if confirm is not True:
            raise ValueError("receipt reversal requires confirm=true or --yes")
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if not row:
                raise NotFound("receipt not found")
            reversed_total = conn.execute(
                "SELECT coalesce(sum(amount_cents),0) FROM receipts WHERE reverses=?", (receipt_id,)
            ).fetchone()[0]
            total = cents(value) if value is not None else row["amount_cents"] + reversed_total
            return self._insert(
                conn,
                "receipts",
                dict(
                    deal_id=row["deal_id"],
                    date=on,
                    amount_cents=-total,
                    note=note,
                    reverses=receipt_id,
                ),
            )

    def add_recurring(
        self,
        name: str,
        kind: str,
        value: str,
        freq: str,
        start: str,
        end: str | None = None,
        currency: str = "CNY",
    ) -> dict[str, Any]:
        with self.connect(write=True) as conn:
            return self._insert(
                conn,
                "recurring",
                dict(
                    name=name,
                    kind=kind,
                    amount_cents=cents(value),
                    freq=freq,
                    start=start,
                    end=end,
                    currency=currency,
                ),
            )

    def add_cash(
        self,
        on: str,
        kind: str,
        value: str,
        currency: str = "CNY",
        category: str = "other",
        note: str = "",
        deal_code: str | None = None,
        recurring_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connect(write=True) as conn:
            return self._insert(
                conn,
                "cash_entries",
                dict(
                    date=on,
                    kind=kind,
                    amount_cents=cents(value),
                    currency=currency,
                    category=category,
                    note=note,
                    deal_id=deal_code,
                    recurring_id=recurring_id,
                ),
            )

    def show_deal(self, code: str, as_of: str | None = None) -> dict[str, Any]:
        cutoff = iso_date(as_of) if as_of else self.today().isoformat()
        with self.connect() as conn:
            deal = self._deal(conn, code)
            plans = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM payment_plans WHERE deal_id=? ORDER BY due_date,id",
                    (deal["id"],),
                )
            ]
            receipts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM receipts WHERE deal_id=? AND date<=? ORDER BY date,created_at,id",
                    (deal["id"], cutoff),
                )
            ]
        received = sum(row["amount_cents"] for row in receipts)
        allocated = received
        for plan in plans:
            used = min(plan["amount_cents"], max(0, allocated))
            allocated -= used
            plan["received_cents"] = used
            plan["remaining_cents"] = plan["amount_cents"] - used
            plan["overdue"] = plan["remaining_cents"] > 0 and plan["due_date"] < cutoff
        return {
            **deal,
            "plans": plans,
            "receipts": receipts,
            "received_cents": received,
            "outstanding_cents": deal["amount_cents"] - received,
            "as_of": cutoff,
            "allocation": "FIFO to earliest planned due dates",
        }

    def calculate(
        self, month: str | None = None, persist: bool = True, as_of: str | None = None
    ) -> dict[str, Any]:
        from .money import month_date

        if month:
            month_date(month)
        rule_id, snapshot, rules = load_rules(self.rules_path)
        with self.connect(write=persist) as conn:
            deals = [
                self._decode(row)
                for row in conn.execute("SELECT * FROM deals ORDER BY signed_date,id")
            ]
            events = []
            for deal in deals:
                rule = select_rule(rules, deal["code"], deal["override_rate"])
                if rule["basis"] == "contract":
                    sources = (
                        [
                            {
                                "id": "contract:" + deal["id"],
                                "date": deal["signed_date"],
                                "amount_cents": deal["amount_cents"],
                            }
                        ]
                        if deal["status"] not in {"negotiating", "cancelled"}
                        else []
                    )
                else:
                    sources = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT * FROM receipts WHERE deal_id=? ORDER BY date,created_at,id",
                            (deal["id"],),
                        )
                    ]
                basis = 0
                previous = (0, 0, 0)
                for source in sources:
                    if as_of and source["date"] > as_of:
                        continue
                    basis += source["amount_cents"]
                    target = cumulative(basis, rule)
                    gross, withholding, net = [target[i] - previous[i] for i in range(3)]
                    previous = target
                    event = {
                        "id": source["id"],
                        "deal_id": deal["id"],
                        "receipt_id": source["id"] if rule["basis"] == "receipts" else None,
                        "rule_id": rule_id,
                        "date": source["date"],
                        "basis_cents": source["amount_cents"],
                        "rate": str(decimal(gross) / decimal(source["amount_cents"]))
                        if source["amount_cents"]
                        else "0",
                        "amount_cents": gross,
                        "withholding_cents": withholding,
                        "net_cents": net,
                        "currency": deal["currency"],
                        "trace": {
                            "deal_code": deal["code"],
                            "basis": rule["basis"],
                            "cumulative_basis_cents": basis,
                            "cumulative_gross_cents": target[0],
                            "rule": rule,
                            "rounding": "cumulative ROUND_HALF_UP to cents; event is delta",
                        },
                    }
                    events.append(event)
            previous_rows = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM commissions")
            }
            current = {row["id"]: row for row in events}
            differences = []
            for key in sorted(set(previous_rows) | set(current)):
                old, new = previous_rows.get(key), current.get(key)
                old_net, new_net = old["net_cents"] if old else 0, new["net_cents"] if new else 0
                if (
                    old is None
                    or new is None
                    or any(
                        old[field] != new[field]
                        for field in ("rule_id", "amount_cents", "withholding_cents", "net_cents")
                    )
                ):
                    differences.append(
                        {
                            "id": key,
                            "date": (new or old)["date"],
                            "currency": (new or old)["currency"],
                            "previous_net_cents": old_net,
                            "new_net_cents": new_net,
                            "delta_net_cents": new_net - old_net,
                            "previous_rule_id": old["rule_id"] if old else None,
                            "new_rule_id": new["rule_id"] if new else None,
                        }
                    )
            if persist:
                if as_of:
                    raise ValueError(
                        "as_of calculations are read-only; cannot overwrite full historical results"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO rules VALUES (?,?,?)", (rule_id, snapshot, now())
                )
                conn.execute("DELETE FROM commissions")
                for row in events:
                    serialized = {k: v for k, v in row.items() if k != "trace"}
                    serialized["trace_json"] = json.dumps(row["trace"], ensure_ascii=False)
                    conn.execute(
                        f"INSERT INTO commissions ({','.join(serialized)}) VALUES ({','.join('?' for _ in serialized)})",
                        list(serialized.values()),
                    )
                if differences:
                    conn.execute(
                        "INSERT INTO calculation_runs VALUES (?,?,?,?)",
                        (str(uuid.uuid4()), rule_id, now(), json.dumps(differences)),
                    )
        selected = [row for row in events if not month or row["date"].startswith(month)]
        totals: dict[str, dict[str, int]] = {}
        for row in selected:
            group = totals.setdefault(
                row["currency"], {"gross_cents": 0, "withholding_cents": 0, "net_cents": 0}
            )
            for target, source in (
                ("gross_cents", "amount_cents"),
                ("withholding_cents", "withholding_cents"),
                ("net_cents", "net_cents"),
            ):
                group[target] += row[source]
        return {
            "period": month or "all",
            "rule_id": rule_id,
            "persisted": persist,
            "totals_by_currency": totals,
            "entries": selected,
            "differences": [
                row for row in differences if not month or row["date"].startswith(month)
            ],
            "all_period_difference_count": len(differences),
            "classification": "accrued commission; not proof of personal cash payment",
        }

    def export(
        self, out: str | Path, format: str = "json", table: str | None = None, bom: bool = True
    ) -> dict[str, Any]:
        if table is not None and table not in TABLES:
            raise ValueError("unsupported table")
        selected = [table] if table else list(TABLES)
        records = [{"record_type": name, **row} for name in selected for row in self.rows(name)]
        path = Path(out).expanduser().resolve()
        if (
            path == self.path
            or str(path).startswith(str(self.path) + "-")
            or path == self.rules_path
        ):
            raise ValueError("export cannot overwrite active database or rules")
        path.parent.mkdir(parents=True, exist_ok=True)
        if format == "json":
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        elif format == "csv":
            keys = (
                ["record_type", "id"]
                + sorted(set().union(*(row.keys() for row in records)) - {"record_type", "id"})
                if records
                else ["record_type", "id"]
            )
            with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=keys)
                writer.writeheader()
                for row in records:
                    writer.writerow(
                        {k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()}
                    )
        else:
            raise ValueError("format must be json or csv")
        return {"path": str(path), "count": len(records), "format": format}

    def import_file(self, path: str | Path, table: str | None = None) -> dict[str, int]:
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            records = json.loads(text)
        elif path.suffix.lower() == ".csv":
            records = list(csv.DictReader(io.StringIO(text)))
            for row in records:
                for key in list(row):
                    if row[key] == "":
                        del row[key]
                if "tags" in row:
                    row["tags"] = json.loads(row["tags"])
                if "amount_cents" in row:
                    row["amount_cents"] = int(row["amount_cents"])
        else:
            raise ValueError("import supports .json and .csv")
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("import must contain a list of records")
        normalized = []
        for item in records:
            row = dict(item)
            kind = row.pop("record_type", table)
            if kind not in TABLES:
                raise ValueError("each row requires a valid record_type or --table")
            if "amount" in row:
                total = cents(row.pop("amount"), kind == "receipts")
                if "amount_cents" in row and total != row["amount_cents"]:
                    raise ValueError("amount and amount_cents disagree")
                row["amount_cents"] = total
            row.setdefault(
                "id",
                hashlib.sha256(
                    json.dumps([kind, row], sort_keys=True, default=str).encode()
                ).hexdigest()[:32],
            )
            normalized.append((kind, row))
        order = {name: i for i, name in enumerate(TABLES)}
        normalized.sort(
            key=lambda item: (
                order[item[0]],
                item[1].get("date", ""),
                item[1].get("amount_cents", 0) < 0,
            )
        )
        inserted = skipped = 0
        with self.connect(write=True) as conn:
            for kind, row in normalized:
                if row.get("deal_id"):
                    row["deal_id"] = self._deal(conn, row["deal_id"])["id"]
                if row.get("override_rate") is not None:
                    row["override_rate"] = str(rate(row["override_rate"]))
                existing = conn.execute(
                    f'SELECT * FROM "{kind}" WHERE id=?', (row["id"],)
                ).fetchone()
                if existing:
                    decoded = self._decode(existing)
                    if any(
                        decoded.get(key) != value
                        for key, value in row.items()
                        if key != "created_at"
                    ):
                        raise ValueError("import ID conflicts with different record content")
                    skipped += 1
                    continue
                self._insert(conn, kind, row)
                inserted += 1
        return {"inserted": inserted, "skipped": skipped}

    def backup(self, out: str | Path | None = None) -> dict[str, Any]:
        stamp = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d_%H%M%S_%f")
        path = (
            Path(out).expanduser().resolve()
            if out
            else self.data_dir / "backups" / f"ledger_{stamp}.zip"
        )
        if (
            path == self.path
            or path == self.rules_path
            or str(path).startswith(str(self.path) + "-")
        ):
            raise ValueError("backup cannot overwrite active files")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder) / "ledger.db"
            with self.connect() as source, sqlite3.connect(temporary) as target:
                source.backup(target)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temporary, "ledger.db")
                archive.write(self.rules_path, "rules.yaml")
                archive.writestr(
                    "settings.json",
                    json.dumps(
                        {
                            "timezone": self.timezone,
                            "base_currency": self.base_currency,
                            "fx_rates": {k: str(v) for k, v in self.fx_rates.items()},
                            "fx_as_of": self.fx_as_of,
                            "opening_balance": amount(self.opening_balance),
                        }
                    ),
                )
        return {"path": str(path), "bytes": path.stat().st_size}
