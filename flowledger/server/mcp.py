"""JSON-RPC 2.0 Model Context Protocol (MCP) Server for FlowLedger."""

from __future__ import annotations

import json
import sys
from typing import Any

from flowledger.aegis_bridge import create_aegis_financial_leaf
from flowledger.cli import run_synthetic_simulation
from flowledger.compliance import SanctionsScreener
from flowledger.ledger.merkle import ledger_merkle_root
from flowledger.ledger.models import Currency
from flowledger.payments.fx import FxEngine
from flowledger.payments.orchestrator import PaymentOrchestrator
from flowledger.payments.split import EscrowManager, SplitRecipient, SplitRule
from flowledger.providers import mpesa
from flowledger.reconciliation import iso20022

MCP_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "flowledger_create_payment",
        "description": "Create an idempotent mobile money payment intent (MTN MoMo or Airtel Money) with integer minor units.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string"},
                "amount": {"type": "integer", "description": "Amount in integer minor units (> 0)"},
                "currency": {"type": "string", "default": "UGX"},
                "provider": {"type": "string", "default": "mtn_momo"},
                "merchant_id": {"type": "string", "default": "merch_kampala_solar"},
                "customer_msisdn": {"type": "string", "default": "+256772123456"},
                "description": {"type": "string", "default": "Mobile money payment"},
            },
            "required": ["idempotency_key", "amount"],
        },
    },
    {
        "name": "flowledger_get_account_balance",
        "description": "Query immutable double-entry ledger account balances and zero-sum trial balance across currencies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Optional account ID to query"},
                "currency": {"type": "string", "description": "Optional currency filter (UGX, KES, NGN, USD)"},
            },
        },
    },
    {
        "name": "flowledger_verify_ledger_invariants",
        "description": "Verify mathematical zero-sum trial balances and cryptographic SHA-256 hash-chain integrity from GENESIS_HASH.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flowledger_run_reconciliation",
        "description": "Execute automated 3-way reconciliation across Payment Orders, Ledger Postings, and Provider Statements.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 50, "description": "Number of transactions to evaluate"},
            },
        },
    },
    {
        "name": "flowledger_create_escrow_hold",
        "description": "Create a marketplace two-phase escrow deposit with multi-party split rules (Funa Gigs pattern).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payment_id": {"type": "string"},
                "amount": {"type": "integer", "description": "Amount in integer minor units"},
                "currency": {"type": "string", "default": "UGX"},
                "provider_id": {"type": "string", "default": "worker_plumber_01"},
                "provider_bps": {"type": "integer", "default": 8800, "description": "Provider share in basis points (8800 = 88%)"},
                "platform_bps": {"type": "integer", "default": 1000, "description": "Platform share in basis points (1000 = 10%)"},
                "gateway_bps": {"type": "integer", "default": 200, "description": "Gateway share in basis points (200 = 2%)"},
            },
            "required": ["payment_id", "amount"],
        },
    },
    {
        "name": "flowledger_release_escrow",
        "description": "Release held escrow funds to marketplace service provider and platform commission accounts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "escrow_id": {"type": "string", "description": "Escrow hold ID to release"},
            },
            "required": ["escrow_id"],
        },
    },
    {
        "name": "flowledger_export_aegis_g6",
        "description": "Export canonical Proof-Carrying Agent Execution (PCAE) financial attestation leaf with RFC 6962 SHA-256 digest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "default": "flowledger_mcp_audit"},
            },
        },
    },
    {
        "name": "flowledger_build_mpesa_stk_push",
        "description": "Build a Safaricom Daraja M-Pesa Express (STK Push) request body from KES minor units. Offline: does not call Safaricom.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shortcode": {"type": "string"},
                "passkey": {"type": "string"},
                "timestamp": {"type": "string", "description": "YYYYMMDDHHMMSS"},
                "amount_minor": {"type": "integer", "description": "KES cents; must be a multiple of 100"},
                "msisdn": {"type": "string"},
                "callback_url": {"type": "string"},
                "account_reference": {"type": "string"},
            },
            "required": ["shortcode", "passkey", "timestamp", "amount_minor", "msisdn", "callback_url", "account_reference"],
        },
    },
    {
        "name": "flowledger_fx_quote",
        "description": "Create a rate-locked cross-border FX quote and optionally execute it as two balanced single-currency journals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "source_amount": {"type": "integer"},
                "rate_micro": {"type": "integer", "description": "target minor units per 1,000,000 source minor units"},
                "spread_bps": {"type": "integer", "default": 100},
                "execute": {"type": "boolean", "default": False},
            },
            "required": ["source", "target", "source_amount", "rate_micro"],
        },
    },
    {
        "name": "flowledger_generate_pacs008",
        "description": "Generate an ISO 20022 pacs.008 credit transfer XML message (pragmatic subset, not certified).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "end_to_end_id": {"type": "string"},
                "amount_minor": {"type": "integer"},
                "currency": {"type": "string"},
                "debtor_name": {"type": "string"},
                "debtor_agent_bic": {"type": "string"},
                "creditor_name": {"type": "string"},
                "creditor_agent_bic": {"type": "string"},
                "remittance_info": {"type": "string", "default": ""},
            },
            "required": ["message_id", "end_to_end_id", "amount_minor", "currency", "debtor_name",
                         "debtor_agent_bic", "creditor_name", "creditor_agent_bic"],
        },
    },
    {
        "name": "flowledger_screen_name",
        "description": "Fuzzy-screen a name against the loaded sanctions/PEP watchlist (bundled list is synthetic). Returns risk signals only.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "flowledger_merkle_root",
        "description": "Compute the RFC 6962 Merkle root over all ledger entry hashes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class FlowLedgerMCPServer:
    """JSON-RPC 2.0 MCP Server exposing FlowLedger double-entry and reconciliation capabilities."""

    def __init__(self, orchestrator: PaymentOrchestrator | None = None) -> None:
        self.orchestrator = orchestrator or PaymentOrchestrator()
        self.escrow = EscrowManager(self.orchestrator.ledger)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Process a single JSON-RPC 2.0 request dictionary and return a response."""
        req_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or "method" not in request:
            return self._error(req_id, -32600, "Invalid Request: must be JSON-RPC 2.0 with 'method'")

        method = request["method"]
        params = request.get("params") or {}

        try:
            if method == "initialize":
                return self._ok(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "flowledger-mcp", "version": "2.1.0"},
                        "capabilities": {"tools": {"listChanged": False}},
                    },
                )
            if method == "ping":
                return self._ok(req_id, {"status": "pong"})
            if method == "tools/list":
                return self._ok(req_id, {"tools": MCP_TOOLS_SPEC})
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                result_payload = self._dispatch_tool(tool_name, arguments)
                return self._ok(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result_payload, sort_keys=True),
                            }
                        ],
                        "structuredContent": result_payload,
                        "isError": False,
                    },
                )

            # Direct method invocation for ergonomic RPC clients
            if method in {
                "flowledger_create_payment",
                "flowledger_get_account_balance",
                "flowledger_verify_ledger_invariants",
                "flowledger_run_reconciliation",
                "flowledger_create_escrow_hold",
                "flowledger_release_escrow",
                "flowledger_export_aegis_g6",
            }:
                return self._ok(req_id, self._dispatch_tool(method, params))

            return self._error(req_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # noqa: BLE001 - JSON-RPC boundary translates tool errors
            return self._error(req_id, -32000, str(exc))

    def _dispatch_tool(self, tool_name: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "flowledger_create_payment":
            intent, replayed = self.orchestrator.create_payment_intent(
                idempotency_key=str(args["idempotency_key"]),
                merchant_id=str(args.get("merchant_id", "merch_kampala_solar")),
                customer_msisdn=str(args.get("customer_msisdn", "+256772123456")),
                amount=int(args["amount"]),
                currency=Currency(args.get("currency", "UGX")),
                provider=str(args.get("provider", "mtn_momo")),
                description=str(args.get("description", "Mobile money payment")),
            )
            resp_dict = intent.to_dict()
            resp_dict["is_replayed"] = replayed
            return resp_dict

        if tool_name == "flowledger_get_account_balance":
            acc_id = args.get("account_id")
            if acc_id:
                acc = self.orchestrator.ledger.get_account(acc_id)
                return acc.to_dict() if acc else {"error": f"Account '{acc_id}' not found"}

            curr_filter = args.get("currency")
            accounts = [
                a.to_dict()
                for a in self.orchestrator.ledger.list_accounts()
                if not curr_filter or a.currency.value == curr_filter
            ]
            inv_dict = self.orchestrator.ledger.verify_invariants().to_dict()
            trial_balances = inv_dict.get("trial_balance_by_currency", {})
            if curr_filter:
                trial_balances = {k: v for k, v in trial_balances.items() if k == curr_filter}
            return {"accounts": accounts, "trial_balances": trial_balances}

        if tool_name == "flowledger_verify_ledger_invariants":
            report = self.orchestrator.ledger.verify_invariants()
            return report.to_dict()

        if tool_name == "flowledger_run_reconciliation":
            count = int(args.get("count", 50))
            sim = run_synthetic_simulation(total_transactions=count)
            return dict(sim["reconciliation"].to_dict())

        if tool_name == "flowledger_create_escrow_hold":
            payment_id = str(args["payment_id"])
            amount = int(args["amount"])
            currency = Currency(args.get("currency", "UGX"))
            provider_id = str(args.get("provider_id", "worker_plumber_01"))
            p_bps = int(args.get("provider_bps", 8800))
            plt_bps = int(args.get("platform_bps", 1000))
            gtw_bps = int(args.get("gateway_bps", 200))

            rule = SplitRule(
                rule_id=f"rule_{payment_id}",
                recipients=[
                    SplitRecipient(
                        recipient_id=provider_id,
                        role="PROVIDER",
                        account_id=f"liability:merchant:{provider_id}",
                        basis_points=p_bps,
                    ),
                    SplitRecipient(
                        recipient_id="platform_treasury",
                        role="PLATFORM",
                        account_id="revenue:commission:platform",
                        basis_points=plt_bps,
                    ),
                    SplitRecipient(
                        recipient_id="gateway_processor",
                        role="GATEWAY",
                        account_id="expense:provider_fee:clearing",
                        basis_points=gtw_bps,
                    ),
                ],
            )
            hold = self.escrow.create_escrow_hold(
                payment_id=payment_id,
                amount=amount,
                currency=currency,
                split_rule=rule,
            )
            return hold.to_dict()

        if tool_name == "flowledger_release_escrow":
            escrow_id = str(args["escrow_id"])
            hold = self.escrow.release_escrow(escrow_id)
            return hold.to_dict()

        if tool_name == "flowledger_export_aegis_g6":
            task_id = str(args.get("task_id", "flowledger_mcp_audit"))
            inv_report = self.orchestrator.ledger.verify_invariants()
            sim = run_synthetic_simulation(total_transactions=20)
            leaf = create_aegis_financial_leaf(
                invariant_report=inv_report,
                recon_report=sim["reconciliation"],
                task_id=task_id,
            )
            return leaf

        if tool_name == "flowledger_build_mpesa_stk_push":
            return mpesa.build_stk_push(
                shortcode=str(args["shortcode"]), passkey=str(args["passkey"]),
                timestamp=str(args["timestamp"]), amount_minor=args["amount_minor"],
                msisdn=str(args["msisdn"]), callback_url=str(args["callback_url"]),
                account_reference=str(args["account_reference"]),
            )

        if tool_name == "flowledger_fx_quote":
            fx = FxEngine(self.orchestrator.ledger)
            q = fx.quote(args["source"], args["target"], args["source_amount"],
                         rate_micro=args["rate_micro"], spread_bps=args.get("spread_bps", 100))
            out = {k: (v.value if hasattr(v, "value") else v) for k, v in q.__dict__.items()}
            if args.get("execute"):
                out["entry_ids"] = list(fx.execute(q.quote_id))
            return out

        if tool_name == "flowledger_generate_pacs008":
            msg = iso20022.Pacs008(
                message_id=str(args["message_id"]), end_to_end_id=str(args["end_to_end_id"]),
                amount_minor=args["amount_minor"], currency=Currency(args["currency"]),
                debtor_name=str(args["debtor_name"]), debtor_agent_bic=str(args["debtor_agent_bic"]),
                creditor_name=str(args["creditor_name"]), creditor_agent_bic=str(args["creditor_agent_bic"]),
                remittance_info=str(args.get("remittance_info", "")),
            )
            return {"xml": iso20022.generate_pacs008(msg)}

        if tool_name == "flowledger_screen_name":
            hits = SanctionsScreener().screen(str(args["name"]))
            return {"match": bool(hits), "hits": [
                {"name": h.entry.name, "list": h.entry.list_name, "category": h.entry.category, "score": h.score}
                for h in hits]}

        if tool_name == "flowledger_merkle_root":
            entries = self.orchestrator.ledger.list_entries()
            return {"entries": len(entries),
                    "merkle_root": ledger_merkle_root([e.entry_hash for e in entries])}

        raise ValueError(f"Unknown tool: {tool_name}")

    @staticmethod
    def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve_stdio(self) -> None:  # pragma: no cover
        """Run JSON-RPC 2.0 loop over standard input/output."""
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                response = self.handle_request(payload)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error: invalid JSON")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
