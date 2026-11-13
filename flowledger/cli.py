"""Command-line interface and 50-transaction East African Mobile Money simulation for FlowLedger."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from flowledger.ledger.models import Currency
from flowledger.payments.orchestrator import PaymentOrchestrator
from flowledger.providers.sandbox import FailureInjectionSimulator, FailureScenario
from flowledger.reconciliation.engine import (
    ReconciliationReport,
    ThreeWayReconciliationEngine,
)


def run_synthetic_simulation(
    total_transactions: int = 50,
) -> dict[str, Any]:
    """Run a deterministic simulation of 50 East African mobile money transactions.

    Includes normal settlements, duplicate webhooks, out-of-order callbacks,
    provider timeout retries, fee discrepancy, missing-in-ledger, and stale/missing-at-provider cases.
    """
    orchestrator = PaymentOrchestrator()
    simulator = FailureInjectionSimulator(orchestrator)
    recon_engine = ThreeWayReconciliationEngine()

    merchants = [
        "merch_kampala_solar",
        "merch_entebbe_agro",
        "merch_nairobi_logistics",
        "merch_jinja_med",
        "merch_gulu_mart",
    ]

    # Deterministic scenario distribution for 50 transactions
    scenario_map: dict[int, FailureScenario] = {
        4: FailureScenario.DUPLICATE_WEBHOOK,
        11: FailureScenario.DUPLICATE_WEBHOOK,
        23: FailureScenario.DUPLICATE_WEBHOOK,
        37: FailureScenario.DUPLICATE_WEBHOOK,
        7: FailureScenario.OUT_OF_ORDER_WEBHOOK,
        19: FailureScenario.OUT_OF_ORDER_WEBHOOK,
        31: FailureScenario.OUT_OF_ORDER_WEBHOOK,
        15: FailureScenario.PROVIDER_TIMEOUT_RETRY,
        41: FailureScenario.PROVIDER_TIMEOUT_RETRY,
        28: FailureScenario.FEE_MISMATCH,
        44: FailureScenario.MISSING_IN_LEDGER,
        48: FailureScenario.MISSING_AT_PROVIDER,
    }

    for idx in range(1, total_transactions + 1):
        provider = "mtn_momo" if idx % 2 == 1 else "airtel_money"
        currency = Currency.UGX if idx <= 40 else Currency.KES
        base_unit = 5_000 if currency == Currency.UGX else 250
        amount = base_unit * ((idx % 9) + 2)
        merchant_id = merchants[(idx - 1) % len(merchants)]
        prefix = "+25677" if provider == "mtn_momo" else "+25670"
        if currency == Currency.KES:
            prefix = "+25472" if provider == "mtn_momo" else "+25473"
        msisdn = f"{prefix}{1000000 + idx:07d}"

        intent, _ = orchestrator.create_payment_intent(
            idempotency_key=f"idem_sim_{idx:04d}",
            payment_id=f"pay_sim_{idx:04d}",
            merchant_id=merchant_id,
            customer_msisdn=msisdn,
            amount=amount,
            currency=currency,
            provider=provider,
            description=f"Synthetic collection #{idx:02d} ({merchant_id})",
        )

        scenario = scenario_map.get(idx, FailureScenario.NORMAL)
        simulator.run_scenario(payment=intent, scenario=scenario)

        # For duplicate webhook scenarios, also exercise idempotent payment creation replay
        if scenario == FailureScenario.DUPLICATE_WEBHOOK:
            orchestrator.create_payment_intent(
                idempotency_key=f"idem_sim_{idx:04d}",
                payment_id=f"pay_sim_{idx:04d}",
                merchant_id=merchant_id,
                customer_msisdn=msisdn,
                amount=amount,
                currency=currency,
                provider=provider,
                description=f"Synthetic collection #{idx:02d} ({merchant_id})",
            )

    invariants = orchestrator.ledger.verify_invariants()
    reconciliation = recon_engine.reconcile(
        payments=orchestrator.list_payments(),
        ledger=orchestrator.ledger,
        provider_statements=simulator.settlement_statements,
    )

    return {
        "orchestrator": orchestrator,
        "simulator": simulator,
        "reconciliation_engine": recon_engine,
        "invariants": invariants,
        "reconciliation": reconciliation,
    }


def format_demo_report(sim_result: dict[str, Any]) -> str:
    """Format a rich ASCII audit report for `flowledger demo`."""
    invariants = sim_result["invariants"]
    recon: ReconciliationReport = sim_result["reconciliation"]
    orchestrator: PaymentOrchestrator = sim_result["orchestrator"]

    lines: list[str] = []
    lines.append("=" * 92)
    lines.append(" FLOWLEDGER v1.0 — AFRICAN PAYMENT ORCHESTRATION & DOUBLE-ENTRY RECONCILIATION AUDIT ")
    lines.append("=" * 92)
    lines.append(f"Total Synthetic Transactions Executed : {len(orchestrator.list_payments())}")
    lines.append(f"Immutable Double-Entry Journal Count  : {invariants.journal_count}")
    lines.append(f"Ledger Accounts Verified              : {invariants.account_count}")
    lines.append(f"Zero-Sum & Hash-Chain Invariant Check : {'PASS (100% VALID)' if invariants.is_valid else 'FAIL'}")
    lines.append(f"Cryptographic Head SHA-256 Hash       : {invariants.head_hash}")
    lines.append("-" * 92)
    lines.append("TRIAL BALANCE BY CURRENCY (INTEGER MINOR UNITS — ZERO FLOATING POINT):")
    lines.append(f"{'Currency':<10} | {'Total Debits':>18} | {'Total Credits':>18} | {'Net Imbalance':>15}")
    lines.append("-" * 92)
    for curr, stats in invariants.trial_balance_by_currency.items():
        lines.append(
            f"{curr:<10} | {stats['total_debits']:>18,d} | {stats['total_credits']:>18,d} | {stats['net_imbalance']:>15,d}"
        )

    lines.append("-" * 92)
    lines.append("3-WAY RECONCILIATION SUMMARY (ORDERS vs. LEDGER vs. PROVIDER STATEMENTS):")
    lines.append(f"{'Classification Status':<28} | {'Record Count':>14} | {'Share':>10}")
    lines.append("-" * 92)
    for status_key, count in recon.counts_by_status.items():
        pct = (count * 100.0 / recon.total_evaluated) if recon.total_evaluated else 0.0
        lines.append(f"{status_key:<28} | {count:>14d} | {pct:>9.1f}%")

    lines.append("-" * 92)
    lines.append("DETECTED RECONCILIATION EXCEPTIONS & ACTIONABLE ADJUSTMENT PROPOSALS:")
    lines.append(
        f"{'Payment ID':<14} | {'Provider':<13} | {'Status':<20} | {'Delta':>10} | {'Recommended Action'}"
    )
    lines.append("-" * 92)
    anomalies = [i for i in recon.items if i.status.value != "MATCHED"]
    for item in anomalies:
        action = (
            item.adjustment_proposal.recommended_action
            if item.adjustment_proposal
            else item.explanation
        )
        lines.append(
            f"{item.payment_id:<14} | {item.provider:<13} | {item.status.value:<20} | "
            f"{item.discrepancy_delta:>6,d} {item.currency.value:<3} | {action[:45]}"
        )
    lines.append("=" * 92)
    return "\n".join(lines)


def cmd_demo(args: argparse.Namespace) -> int:
    sim_result = run_synthetic_simulation(total_transactions=args.count)
    recon = sim_result["reconciliation"]

    if getattr(args, "export_csv", None):
        csv_content = ThreeWayReconciliationEngine.export_reconciliation_csv(recon)
        with open(args.export_csv, "w", encoding="utf-8") as f:
            f.write(csv_content)
        print(f"[CSV Export] Saved full 3-way reconciliation to: {args.export_csv}")

    if getattr(args, "export_exceptions_csv", None):
        exc_content = ThreeWayReconciliationEngine.export_exceptions_csv(recon)
        with open(args.export_exceptions_csv, "w", encoding="utf-8") as f:
            f.write(exc_content)
        print(f"[CSV Export] Saved reconciliation exceptions to: {args.export_exceptions_csv}")

    if getattr(args, "csv", False):
        print(ThreeWayReconciliationEngine.export_reconciliation_csv(recon))
    elif getattr(args, "exceptions_csv", False):
        print(ThreeWayReconciliationEngine.export_exceptions_csv(recon))
    elif args.json:
        print(
            json.dumps(
                {
                    "invariants": sim_result["invariants"].to_dict(),
                    "reconciliation": sim_result["reconciliation"].to_dict(),
                },
                indent=2,
            )
        )
    else:
        print(format_demo_report(sim_result))
    return 0 if sim_result["invariants"].is_valid else 1


def cmd_export_aegis(args: argparse.Namespace) -> int:
    from flowledger.aegis_bridge import create_aegis_financial_leaf

    sim_result = run_synthetic_simulation(total_transactions=args.count)
    leaf = create_aegis_financial_leaf(
        invariant_report=sim_result["invariants"],
        recon_report=sim_result["reconciliation"],
        task_id=args.task_id,
    )
    if args.json:
        print(json.dumps(leaf, indent=2))
    else:
        print("FLOWLEDGER NEXUS AEGIS ATTESTATION LEAF")
        print("-" * 72)
        print(f"Gate ID        : {leaf['payload']['gate_id']}")
        print(f"Passed         : {leaf['payload']['passed']} (Score: {leaf['payload']['score']})")
        print(f"Leaf SHA-256   : {leaf['leaf_hash']}")
        print(f"RFC 6962 Prefix: {leaf['rfc6962_prefix']}")
    return 0 if leaf["payload"]["passed"] else 1


def cmd_mcp(args: argparse.Namespace) -> int:  # pragma: no cover
    from flowledger.server.mcp import FlowLedgerMCPServer

    FlowLedgerMCPServer().serve_stdio()
    return 0


def cmd_verify_invariants(args: argparse.Namespace) -> int:
    sim_result = run_synthetic_simulation(total_transactions=args.count)
    report = sim_result["invariants"]
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print("FLOWLEDGER MATHEMATICAL & CRYPTOGRAPHIC INVARIANT VERIFICATION")
        print("-" * 72)
        print(f"Status             : {'VALID (PASS)' if report.is_valid else 'INVALID (FAIL)'}")
        print(f"Journal Entries    : {report.journal_count}")
        print(f"Accounts Checked   : {report.account_count}")
        print(f"Head Chain SHA-256 : {report.head_hash}")
        for curr, tb in report.trial_balance_by_currency.items():
            print(
                f"Currency [{curr}]     : debits={tb['total_debits']:,d} == credits={tb['total_credits']:,d} "
                f"(imbalance={tb['net_imbalance']})"
            )
        if report.errors:
            print("Errors:")
            for err in report.errors:
                print(f"  - {err}")
    return 0 if report.is_valid else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from flowledger.server.app import create_app

    app = create_app(bootstrap_demo=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowledger",
        description="FlowLedger — African Payment Orchestration & Double-Entry Reconciliation Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser(
        "demo",
        help="Run end-to-end 50-transaction mobile money simulation & 3-way reconciliation audit",
    )
    p_demo.add_argument("--count", type=int, default=50, help="Number of synthetic transactions")
    p_demo.add_argument("--json", action="store_true", help="Emit JSON output")
    p_demo.add_argument("--csv", action="store_true", help="Emit full reconciliation table as CSV")
    p_demo.add_argument("--exceptions-csv", action="store_true", help="Emit reconciliation exceptions as CSV")
    p_demo.add_argument("--export-csv", type=str, default=None, help="File path to save full reconciliation CSV")
    p_demo.add_argument(
        "--export-exceptions-csv",
        type=str,
        default=None,
        help="File path to save reconciliation exceptions CSV",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_aegis = sub.add_parser(
        "export-aegis",
        help="Export canonical Nexus Aegis G6 financial attestation leaf with RFC 6962 SHA-256 digest",
    )
    p_aegis.add_argument("--count", type=int, default=50, help="Number of synthetic transactions")
    p_aegis.add_argument("--task-id", type=str, default="flowledger_financial_audit", help="Attestation task ID")
    p_aegis.add_argument("--json", action="store_true", default=True, help="Emit JSON output")
    p_aegis.set_defaults(func=cmd_export_aegis)

    p_mcp = sub.add_parser(
        "mcp",
        help="Start Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio",
    )
    p_mcp.set_defaults(func=cmd_mcp)

    p_verify = sub.add_parser(
        "verify-invariants",
        help="Verify zero-sum double-entry accounting equation and SHA-256 hash chain",
    )
    p_verify.add_argument("--count", type=int, default=50, help="Number of transactions to verify")
    p_verify.add_argument("--json", action="store_true", help="Emit JSON output")
    p_verify.set_defaults(func=cmd_verify_invariants)

    p_serve = sub.add_parser(
        "serve",
        help="Start the FastAPI server and interactive Financial Operations Dashboard",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8095, help="Bind port (default: 8095)")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
