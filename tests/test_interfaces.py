import asyncio
import json
import os
import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from commission_cashflow_ledger.cli import main
from commission_cashflow_ledger.config import make_ledger
from commission_cashflow_ledger.mcp_server import create_server
from commission_cashflow_ledger.web.app import create_app


def cli(capsys, tmp_path, *args):
    status = main(["--data-dir", str(tmp_path / "cli"), *args, "--json"])
    output = capsys.readouterr()
    return status, json.loads(output.out)


def test_cli_all_commands(capsys, tmp_path):
    assert cli(capsys, tmp_path, "init")[0] == 0
    assert (
        cli(
            capsys,
            tmp_path,
            "deal",
            "add",
            "--code",
            "D-1",
            "--client-code",
            "A",
            "--signed-date",
            "2026-01-01",
            "--amount",
            "1000",
        )[0]
        == 0
    )
    assert cli(capsys, tmp_path, "deal", "list")[1]["data"][0]["code"] == "D-1"
    assert cli(capsys, tmp_path, "deal", "update", "D-1", "--override-rate", "0.04")[0] == 0
    assert (
        cli(
            capsys,
            tmp_path,
            "plan",
            "add",
            "--deal",
            "D-1",
            "--due-date",
            "2026-10-01",
            "--proportion",
            "1",
        )[0]
        == 0
    )
    receipt = cli(
        capsys,
        tmp_path,
        "receipt",
        "add",
        "--deal",
        "D-1",
        "--date",
        "2026-09-01",
        "--amount",
        "1000",
    )[1]["data"]
    assert cli(capsys, tmp_path, "deal", "show", "D-1")[1]["data"]["received_cents"] == 100000
    assert (
        cli(capsys, tmp_path, "commission", "calc", "--month", "2026-09")[1]["data"][
            "totals_by_currency"
        ]["CNY"]["net_cents"]
        == 4000
    )
    assert cli(capsys, tmp_path, "commission", "history")[1]["data"]
    assert (
        cli(
            capsys,
            tmp_path,
            "recurring",
            "add",
            "--name",
            "Rent",
            "--kind",
            "expense",
            "--amount",
            "10",
            "--freq",
            "monthly",
            "--start",
            "2026-10-01",
        )[0]
        == 0
    )
    assert len(cli(capsys, tmp_path, "recurring", "list")[1]["data"]) == 1
    assert (
        cli(
            capsys,
            tmp_path,
            "cash",
            "add",
            "--date",
            "2026-09-01",
            "--kind",
            "income",
            "--amount",
            "20",
            "--category",
            "commission",
            "--deal",
            "D-1",
        )[0]
        == 0
    )
    assert len(cli(capsys, tmp_path, "cash", "list")[1]["data"]) == 1
    assert len(cli(capsys, tmp_path, "cashflow")[1]["data"]["rows"]) == 6
    assert cli(capsys, tmp_path, "alerts")[0] == 0
    assert (
        cli(capsys, tmp_path, "receipt", "reverse", receipt["id"], "--date", "2026-09-02", "--yes")[
            0
        ]
        == 0
    )
    assert (
        cli(capsys, tmp_path, "report", "--month", "2026-09")[1]["data"]["summary"][
            "customer_collections_cents"
        ]
        == 0
    )
    assert cli(capsys, tmp_path, "backup")[1]["data"]["bytes"] > 0
    path = tmp_path / "export.csv"
    assert cli(capsys, tmp_path, "export", "--format", "csv", "--out", str(path))[0] == 0
    assert cli(capsys, tmp_path, "import", str(path))[1]["data"]["inserted"] == 0


@pytest.mark.parametrize(
    "args,status",
    [
        (["deal", "show", "unknown"], 3),
        (["unknown"], 2),
        (["cashflow", "--months", "0"], 2),
        (["import", "/nonexistent/ledger.csv"], 1),
        (["receipt", "reverse", "unknown", "--date", "2026-01-01"], 2),
    ],
)
def test_cli_error_envelope(capsys, tmp_path, args, status):
    code, result = cli(capsys, tmp_path, *args)
    assert code == status and not result["ok"] and result["error"]["message"]


def test_global_flags_config_and_environment(tmp_path, capsys, monkeypatch):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    assert main(["--help"]) == 0
    assert "Global:" in capsys.readouterr().out
    config = tmp_path / "settings.yaml"
    config.write_text("data_dir: configured\nopening_balance: '500'\nbase_currency: CNY\n")
    store = make_ledger(config=str(config))
    assert store.data_dir == tmp_path / "configured"
    assert store.opening_balance == 50000
    monkeypatch.setenv("LEDGER_DATA_DIR", str(tmp_path / "env"))
    assert main(["init", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["init", "--json", "--progress"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["data"]["database"].endswith("env/ledger.db")
    assert "Running" in output.err
    config.write_text("unknown: true")
    with pytest.raises(ValueError):
        make_ledger(config=str(config))


def test_rest_dashboard_and_origin_guard(demo):
    client = TestClient(create_app(demo), base_url="http://127.0.0.1")
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert len(client.get("/api/deals").json()["data"]) == 2
    assert client.get("/api/status").json()["data"]["currency"] == "CNY"
    assert client.get("/api/deals/D-2026-001").json()["data"]["received_cents"] == 5000000
    assert client.get("/api/deals/unknown").status_code == 404
    receipt = {
        "deal_code": "D-2026-001",
        "date": "2026-10-01",
        "amount": "100",
        "idempotency_key": "web-receipt",
    }
    assert client.post("/api/receipts", json=receipt).status_code == 200
    assert client.post("/api/receipts", json=receipt).json()["data"]["deduplicated"]
    assert (
        client.get("/api/commissions?month=2026-09").json()["data"]["totals_by_currency"]["CNY"][
            "gross_cents"
        ]
        == 2050000
    )
    assert client.get("/api/cashflow").json()["data"]["months"] == 6
    assert client.get("/api/alerts").status_code == 200
    assert "20500.00" in client.get("/api/report/2026-09").json()["data"]["markdown"]
    assert client.get("/api/cashflow?scenario=wrong").status_code == 400
    assert (
        client.post(
            "/api/receipts", json=receipt, headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert client.get("/api/deals", headers={"Host": "untrusted.invalid"}).status_code == 400
    assert "/api/receipts" in client.get("/openapi.json").json()["paths"]


def payload(result):
    return result.structuredContent or json.loads(result.content[0].text)


def test_every_mcp_tool_and_resource(demo):
    async def scenario():
        async with create_connected_server_and_client_session(
            create_server(demo), read_timeout_seconds=timedelta(seconds=10)
        ) as client:
            await client.initialize()
            assert {t.name for t in (await client.list_tools()).tools} == {
                "add_receipt",
                "get_commission_summary",
                "get_cashflow_forecast",
                "list_alerts",
            }
            result = payload(
                await client.call_tool("get_commission_summary", {"period": "2026-09"})
            )
            assert result["totals_by_currency"]["CNY"]["net_cents"] == 1845000
            assert (
                payload(
                    await client.call_tool(
                        "add_receipt",
                        {
                            "deal_code": "D-2026-001",
                            "date": "2026-10-01",
                            "amount": "100",
                            "idempotency_key": "mcp-receipt",
                        },
                    )
                )["amount_cents"]
                == 10000
            )
            assert (
                payload(
                    await client.call_tool(
                        "get_cashflow_forecast", {"months": 3, "scenario": "conservative"}
                    )
                )["months"]
                == 3
            )
            assert "overdue" in payload(await client.call_tool("list_alerts", {}))
            assert (
                await client.call_tool(
                    "add_receipt", {"deal_code": "unknown", "date": "2026-10-01", "amount": "100"}
                )
            ).isError
            assert "version: 1" in (await client.read_resource("ledger://rules")).contents[0].text

    asyncio.run(scenario())


def test_real_stdio_handshake(tmp_path):
    async def scenario():
        options = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "commission_cashflow_ledger.cli",
                "--data-dir",
                str(tmp_path / "stdio"),
                "mcp",
            ],
            env=dict(os.environ),
        )
        async with stdio_client(options) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as client:
                assert (await client.initialize()).serverInfo.name == "commission-cashflow-ledger"
                assert (
                    payload(await client.call_tool("get_commission_summary", {}))[
                        "totals_by_currency"
                    ]
                    == {}
                )

    asyncio.run(scenario())
