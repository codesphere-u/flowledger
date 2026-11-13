"""Unit tests for FlowLedger MCP Server (JSON-RPC 2.0) and Nexus Aegis G6 Bridge."""

from __future__ import annotations

import pytest

from flowledger.aegis_bridge import (
    canonical_json_bytes,
    create_aegis_financial_leaf,
    hash_leaf_bytes,
)
from flowledger.cli import main
from flowledger.server.mcp import FlowLedgerMCPServer


def test_aegis_canonical_json_and_rfc6962_hash() -> None:
    obj = {"z": 100, "a": "hello", "postings": [{"amount": 500, "ccy": "UGX"}]}
    raw = canonical_json_bytes(obj)
    assert raw == b'{"a":"hello","postings":[{"amount":500,"ccy":"UGX"}],"z":100}'

    digest = hash_leaf_bytes(raw)
    assert len(digest) == 64
    import hashlib

    assert digest == hashlib.sha256(b"\x00" + raw).hexdigest()


def test_aegis_financial_leaf_passed_and_violation() -> None:
    # Passed
    inv_data = {
        "journal_count": 50,
        "account_count": 12,
        "head_hash": "a" * 64,
        "is_valid": True,
        "trial_balance_by_currency": {"UGX": {"net_imbalance": 0}},
    }
    recon_data = {
        "total_evaluated": 50,
        "counts_by_status": {"MATCHED": 45, "FEE_DISCREPANCY": 5},
        "match_rate_pct": 90.0,
    }
    leaf = create_aegis_financial_leaf(invariant_report=inv_data, recon_report=recon_data)
    assert leaf["payload"]["passed"]
    assert leaf["payload"]["gate_id"] == "G_FINANCIAL_INVARIANTS"
    assert leaf["rfc6962_prefix"] == "0x00"
    assert len(leaf["leaf_hash"]) == 64

    # Violation (imbalance)
    inv_bad = {
        "journal_count": 50,
        "is_valid": False,
        "trial_balance_by_currency": {"UGX": {"net_imbalance": 5000}},
        "errors": ["Imbalance detected in UGX"],
    }
    bad_leaf = create_aegis_financial_leaf(invariant_report=inv_bad)
    assert not bad_leaf["payload"]["passed"]
    assert any("imbalance" in v.lower() for v in bad_leaf["payload"]["violations"])


def test_mcp_server_initialize_ping_and_tools_list() -> None:
    server = FlowLedgerMCPServer()

    init_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp["result"]["serverInfo"]["name"] == "flowledger-mcp"
    assert init_resp["result"]["serverInfo"]["version"] == "2.0.0"

    ping_resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert ping_resp["result"]["status"] == "pong"

    tools_resp = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert names == {
        "flowledger_build_mpesa_stk_push",
        "flowledger_fx_quote",
        "flowledger_generate_pacs008",
        "flowledger_screen_name",
        "flowledger_merkle_root",
        "flowledger_create_payment",
        "flowledger_get_account_balance",
        "flowledger_verify_ledger_invariants",
        "flowledger_run_reconciliation",
        "flowledger_create_escrow_hold",
        "flowledger_release_escrow",
        "flowledger_export_aegis_g6",
    }


def test_mcp_server_tools_call_execution() -> None:
    server = FlowLedgerMCPServer()

    # 1. create_payment
    p_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t1",
            "method": "tools/call",
            "params": {
                "name": "flowledger_create_payment",
                "arguments": {
                    "idempotency_key": "mcp_idemp_01",
                    "amount": 75000,
                    "currency": "UGX",
                    "provider": "mtn_momo",
                },
            },
        }
    )
    p_data = p_resp["result"]["structuredContent"]
    assert p_data["amount"] == 75000
    assert p_data["state"] in ("INITIATED", "PENDING_PROVIDER")

    # 2. get_account_balance
    bal_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t2",
            "method": "tools/call",
            "params": {"name": "flowledger_get_account_balance", "arguments": {}},
        }
    )
    assert "trial_balances" in bal_resp["result"]["structuredContent"]

    # 3. verify_ledger_invariants
    inv_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t3",
            "method": "tools/call",
            "params": {"name": "flowledger_verify_ledger_invariants", "arguments": {}},
        }
    )
    assert inv_resp["result"]["structuredContent"]["is_valid"]

    # 4. create_escrow_hold
    escrow_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t4",
            "method": "tools/call",
            "params": {
                "name": "flowledger_create_escrow_hold",
                "arguments": {
                    "payment_id": p_data["payment_id"],
                    "amount": 75000,
                    "currency": "UGX",
                    "provider_id": "mechanic_jinja",
                    "provider_bps": 8800,
                    "platform_bps": 1000,
                    "gateway_bps": 200,
                },
            },
        }
    )
    escrow_data = escrow_resp["result"]["structuredContent"]
    assert escrow_data["status"] == "HELD"
    e_id = escrow_data["escrow_id"]

    # 5. release_escrow
    rel_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t5",
            "method": "tools/call",
            "params": {
                "name": "flowledger_release_escrow",
                "arguments": {"escrow_id": e_id},
            },
        }
    )
    assert rel_resp["result"]["structuredContent"]["status"] == "RELEASED"

    # 6. export_aegis_g6
    aegis_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t6",
            "method": "tools/call",
            "params": {
                "name": "flowledger_export_aegis_g6",
                "arguments": {"task_id": "mcp_test_run"},
            },
        }
    )
    assert aegis_resp["result"]["structuredContent"]["payload"]["passed"]


def test_cli_export_aegis(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["export-aegis", "--count", "10", "--json"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "G_FINANCIAL_INVARIANTS" in out
    assert "leaf_hash" in out
