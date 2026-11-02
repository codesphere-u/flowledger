"""Tests for 3-way reconciliation engine, CLI commands, and FastAPI endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from flowledger.cli import main as cli_main, run_synthetic_simulation
from flowledger.ledger.models import Currency
from flowledger.payments import PaymentOrchestrator
from flowledger.providers import FailureInjectionSimulator, FailureScenario
from flowledger.reconciliation import (
    ReconciliationStatus,
    ThreeWayReconciliationEngine,
)
from flowledger.server import create_app


def test_reconciliation_detects_all_six_classification_categories() -> None:
    orchestrator = PaymentOrchestrator()
    simulator = FailureInjectionSimulator(orchestrator)
    recon_engine = ThreeWayReconciliationEngine()

    test_matrix = [
        ("pay_match", FailureScenario.NORMAL, ReconciliationStatus.MATCHED),
        ("pay_fee_disc", FailureScenario.FEE_MISMATCH, ReconciliationStatus.FEE_DISCREPANCY),
        ("pay_miss_ledger", FailureScenario.MISSING_IN_LEDGER, ReconciliationStatus.MISSING_IN_LEDGER),
        ("pay_stale", FailureScenario.MISSING_AT_PROVIDER, ReconciliationStatus.STALE_UNSETTLED),
        ("pay_amt_mismatch", FailureScenario.AMOUNT_MISMATCH, ReconciliationStatus.AMOUNT_MISMATCH),
    ]

    for pid, scenario, _ in test_matrix:
        intent, _ = orchestrator.create_payment_intent(
            idempotency_key=f"idem_{pid}",
            payment_id=pid,
            merchant_id="merch_recon",
            customer_msisdn="+256772000111",
            amount=100_000,
            currency=Currency.UGX,
            provider="mtn_momo",
        )
        simulator.run_scenario(payment=intent, scenario=scenario)

    # Also create a payment that settled locally in ledger but is missing from provider statement
    intent_missing_prov, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_pay_miss_prov",
        payment_id="pay_miss_prov",
        merchant_id="merch_recon",
        customer_msisdn="+256772000999",
        amount=100_000,
        currency=Currency.UGX,
        provider="airtel_money",
    )
    orchestrator.transition_payment("pay_miss_prov", "AUTHORIZED")
    orchestrator.transition_payment("pay_miss_prov", "SETTLED")

    report = recon_engine.reconcile(
        payments=orchestrator.list_payments(),
        ledger=orchestrator.ledger,
        provider_statements=simulator.settlement_statements,
    )

    by_id = {item.payment_id: item for item in report.items}
    assert by_id["pay_match"].status == ReconciliationStatus.MATCHED
    assert by_id["pay_fee_disc"].status == ReconciliationStatus.FEE_DISCREPANCY
    assert by_id["pay_miss_ledger"].status == ReconciliationStatus.MISSING_IN_LEDGER
    assert by_id["pay_stale"].status == ReconciliationStatus.STALE_UNSETTLED
    assert by_id["pay_amt_mismatch"].status == ReconciliationStatus.AMOUNT_MISMATCH
    assert by_id["pay_miss_prov"].status == ReconciliationStatus.MISSING_AT_PROVIDER


def test_reconciliation_adjustment_proposals_are_balanced_double_entry_postings() -> None:
    sim = run_synthetic_simulation(total_transactions=50)
    recon = sim["reconciliation"]
    assert len(recon.adjustment_proposals) >= 3

    for proposal in recon.adjustment_proposals:
        debits = sum(
            p["amount"] for p in proposal.proposed_postings if p["direction"] == "DEBIT"
        )
        credits = sum(
            p["amount"] for p in proposal.proposed_postings if p["direction"] == "CREDIT"
        )
        assert debits == credits > 0


def test_settlement_statement_csv_export_and_parse_roundtrip() -> None:
    sim = run_synthetic_simulation(total_transactions=10)
    statements = sim["simulator"].settlement_statements
    csv_text = ThreeWayReconciliationEngine.export_settlement_csv(statements)
    parsed = ThreeWayReconciliationEngine.parse_settlement_csv(csv_text)

    assert len(parsed) == len(statements)
    assert parsed[0].payment_id == statements[0].payment_id
    assert parsed[0].gross_amount == statements[0].gross_amount
    assert parsed[0].provider_fee == statements[0].provider_fee


def test_50_transaction_simulation_invariants_and_counts() -> None:
    sim = run_synthetic_simulation(total_transactions=50)
    assert len(sim["orchestrator"].list_payments()) == 50
    assert sim["invariants"].is_valid is True
    assert sim["reconciliation"].total_evaluated == 50
    assert sim["reconciliation"].counts_by_status["MATCHED"] == 47
    assert sim["reconciliation"].counts_by_status["FEE_DISCREPANCY"] == 1
    assert sim["reconciliation"].counts_by_status["MISSING_IN_LEDGER"] == 1
    assert sim["reconciliation"].counts_by_status["STALE_UNSETTLED"] == 1


def test_cli_demo_and_verify_invariants_exit_zero(capsys: object) -> None:
    rc_demo = cli_main(["demo", "--count", "50"])
    assert rc_demo == 0

    rc_verify = cli_main(["verify-invariants", "--count", "50", "--json"])
    assert rc_verify == 0


def test_fastapi_health_and_dashboard_endpoints() -> None:
    app = create_app(bootstrap_demo=True)
    client = TestClient(app)

    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "FlowLedger Financial Operations Dashboard" in res_dash.text

    res_health = client.get("/health")
    assert res_health.status_code == 200
    body = res_health.json()
    assert body["status"] == "ok"
    assert body["invariants_valid"] is True


def test_fastapi_payment_creation_and_idempotency_conflict_409() -> None:
    app = create_app(bootstrap_demo=False)
    client = TestClient(app)

    payload = {
        "idempotency_key": "api_idem_001",
        "merchant_id": "merch_api",
        "customer_msisdn": "+256775551122",
        "amount": 45_000,
        "currency": "UGX",
        "provider": "mtn_momo",
    }
    r1 = client.post("/api/payments", json=payload)
    assert r1.status_code == 201
    assert r1.json()["replayed"] is False

    # Identical retry returns 200 with cached response
    r2 = client.post("/api/payments", json=payload)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["payment"]["payment_id"] == r1.json()["payment"]["payment_id"]

    # Mutated amount with same idempotency_key returns HTTP 409 Conflict
    mutated = dict(payload, amount=99_000)
    r3 = client.post("/api/payments", json=mutated)
    assert r3.status_code == 409


def test_fastapi_webhook_endpoint_verifies_hmac_and_rejects_forgery() -> None:
    app = create_app(bootstrap_demo=False)
    client = TestClient(app)

    r_create = client.post(
        "/api/payments",
        json={
            "idempotency_key": "wh_api_test",
            "merchant_id": "merch_wh",
            "customer_msisdn": "+256771234567",
            "amount": 50_000,
            "currency": "UGX",
            "provider": "mtn_momo",
        },
    )
    payment_dict = r_create.json()["payment"]

    # Forge an invalid webhook signature -> HTTP 401
    r_bad = client.post(
        "/api/webhooks/mtn_momo",
        content='{"event_id":"e1","payment_id":"' + payment_dict["payment_id"] + '","status":"AUTHORIZED"}',
        headers={"X-Signature": "sha256=invalid", "X-Timestamp": "1700000000"},
    )
    assert r_bad.status_code == 401


def test_fastapi_simulate_and_reconciliation_endpoints() -> None:
    app = create_app(bootstrap_demo=False)
    client = TestClient(app)

    r_sim = client.post(
        "/api/simulate",
        json={
            "scenario": "FEE_MISMATCH",
            "provider": "airtel_money",
            "amount": 80_000,
            "currency": "UGX",
        },
    )
    assert r_sim.status_code == 200
    assert r_sim.json()["invariants"]["is_valid"] is True

    r_recon = client.get("/api/reconciliation")
    assert r_recon.status_code == 200
    assert r_recon.json()["counts_by_status"]["FEE_DISCREPANCY"] == 1
