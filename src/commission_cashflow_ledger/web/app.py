"""Loopback-only JSON API and static HTML dashboard."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..core.forecast import alerts, cashflow
from ..core.money import amount
from ..core.reports import markdown, report_data
from ..core.store import Ledger, NotFound


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deal_code: str
    date: str
    amount: str
    note: str = ""
    idempotency_key: str | None = None


def envelope(data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "warnings": data.get("warnings", []) if isinstance(data, dict) else [],
    }


def create_app(ledger: Ledger) -> FastAPI:
    app = FastAPI(title="Commission Cashflow Ledger", docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next: Any) -> Any:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": {
                            "code": "FORBIDDEN",
                            "message": "cross-origin writes are blocked",
                        },
                    },
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": {"code": "NOT_FOUND", "message": str(exc)}}, status_code=404
        )

    async def invalid(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": str(exc)}},
            status_code=400,
        )

    app.add_exception_handler(ValueError, invalid)
    app.add_exception_handler(sqlite3.IntegrityError, invalid)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return envelope(
            {
                "today": ledger.today().isoformat(),
                "currency": ledger.base_currency,
                "timezone": ledger.timezone,
                "opening_balance": amount(ledger.opening_balance),
            }
        )

    @app.get("/api/deals")
    def deals() -> dict[str, Any]:
        return envelope([ledger.show_deal(d["id"]) for d in ledger.rows("deals")])

    @app.get("/api/deals/{code}")
    def detail(code: str) -> dict[str, Any]:
        return envelope(ledger.show_deal(code))

    @app.post("/api/receipts")
    def receipt(body: Receipt) -> dict[str, Any]:
        return envelope(
            ledger.add_receipt(
                body.deal_code, body.date, body.amount, body.note, body.idempotency_key
            )
        )

    @app.get("/api/commissions")
    def commissions(month: str | None = None) -> dict[str, Any]:
        return envelope(ledger.calculate(month, persist=False))

    @app.get("/api/cashflow")
    def forecast(
        months: int = 6,
        scenario: str = "base",
        start: str | None = None,
        as_of: str | None = None,
        opening_balance: str | None = None,
    ) -> dict[str, Any]:
        return envelope(cashflow(ledger, months, scenario, start, as_of, opening_balance))

    @app.get("/api/alerts")
    def warnings(as_of: str | None = None) -> dict[str, Any]:
        return envelope(alerts(ledger, as_of=as_of))

    @app.get("/api/report/{month}")
    def preview(month: str, as_of: str | None = None) -> dict[str, Any]:
        data = report_data(ledger, month, as_of)
        return envelope(
            {
                "markdown": markdown(data),
                "summary": {k: v for k, v in data.items() if k not in {"forecast", "commission"}},
            }
        )

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
