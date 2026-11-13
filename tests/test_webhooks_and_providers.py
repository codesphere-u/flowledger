"""Tests for cryptographic HMAC-SHA256 webhook verification and sandbox provider adapters."""

from __future__ import annotations

import pytest

from flowledger.ledger.models import Currency
from flowledger.payments import PaymentOrchestrator
from flowledger.providers import (
    AirtelMoneySandboxAdapter,
    FailureInjectionSimulator,
    FailureScenario,
    MtnMomoSandboxAdapter,
    WebhookReplayError,
    WebhookSignatureError,
    WebhookVerifier,
    calculate_retry_backoff_ms,
)


def test_webhook_verifier_signs_and_verifies_valid_payload() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890", replay_window_seconds=300)
    payload = {"event_id": "evt_1", "payment_id": "pay_1", "status": "SETTLED", "amount": 50000}
    env = verifier.sign_payload(payload, provider="mtn_momo", timestamp=1_700_000_000)

    verified = verifier.verify(env.raw_body, env.headers, now_timestamp=1_700_000_120)
    assert verified == payload


def test_webhook_verifier_rejects_tampered_payload() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890")
    payload = {"event_id": "evt_1", "payment_id": "pay_1", "amount": 50000}
    env = verifier.sign_payload(payload, timestamp=1_700_000_000)

    tampered_body = env.raw_body.replace("50000", "90000")
    with pytest.raises(WebhookSignatureError, match="HMAC-SHA256"):
        verifier.verify(tampered_body, env.headers, now_timestamp=1_700_000_010)


def test_webhook_verifier_rejects_wrong_secret() -> None:
    signer = WebhookVerifier("whsec_correct_secret_12345")
    wrong_verifier = WebhookVerifier("whsec_wrong_secret_67890")
    env = signer.sign_payload({"event_id": "evt_1"}, timestamp=1_700_000_000)

    with pytest.raises(WebhookSignatureError):
        wrong_verifier.verify(env.raw_body, env.headers, now_timestamp=1_700_000_005)


def test_webhook_verifier_rejects_missing_headers() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890")
    with pytest.raises(WebhookSignatureError, match="Missing required 'X-Signature'"):
        verifier.verify('{"a":1}', {"X-Timestamp": "1700000000"})

    with pytest.raises(WebhookReplayError, match="Missing required 'X-Timestamp'"):
        verifier.verify('{"a":1}', {"X-Signature": "sha256=deadbeef"})


def test_webhook_verifier_rejects_stale_timestamp_replay_attack() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890", replay_window_seconds=300)
    env = verifier.sign_payload({"event_id": "evt_old"}, timestamp=1_700_000_000)

    with pytest.raises(WebhookReplayError, match="exceeds replay protection window"):
        verifier.verify(env.raw_body, env.headers, now_timestamp=1_700_000_600)


def test_webhook_verifier_rejects_future_timestamp_outside_window() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890", replay_window_seconds=300)
    env = verifier.sign_payload({"event_id": "evt_future"}, timestamp=1_700_001_000)

    with pytest.raises(WebhookReplayError, match="exceeds replay protection window"):
        verifier.verify(env.raw_body, env.headers, now_timestamp=1_700_000_000)


def test_webhook_verifier_nonce_cache_blocks_exact_replay() -> None:
    verifier = WebhookVerifier("whsec_test_secret_1234567890")
    env = verifier.sign_payload({"event_id": "evt_nonce"}, timestamp=1_700_000_000)

    verifier.verify(
        env.raw_body, env.headers, now_timestamp=1_700_000_010, enforce_nonce_cache=True
    )
    with pytest.raises(WebhookReplayError, match="Replay detected"):
        verifier.verify(
            env.raw_body, env.headers, now_timestamp=1_700_000_012, enforce_nonce_cache=True
        )


def test_mtn_and_airtel_sandbox_fee_structures() -> None:
    mtn = MtnMomoSandboxAdapter()
    airtel = AirtelMoneySandboxAdapter()

    # MTN is 1.20% (120 bps), Airtel is 1.50% (150 bps) in integer minor units
    assert mtn.compute_provider_fee(100_000) == 1_200
    assert airtel.compute_provider_fee(100_000) == 1_500


def test_exponential_backoff_calculator_monotonic_and_capped() -> None:
    b1 = calculate_retry_backoff_ms(1, base_delay_ms=200, max_delay_ms=2000)
    b2 = calculate_retry_backoff_ms(2, base_delay_ms=200, max_delay_ms=2000)
    b3 = calculate_retry_backoff_ms(3, base_delay_ms=200, max_delay_ms=2000)
    b10 = calculate_retry_backoff_ms(10, base_delay_ms=200, max_delay_ms=2000, deterministic_key="pay_1")

    assert b1 == 200
    assert b2 == 400
    assert b3 == 800
    assert b10 <= 2000


def test_failure_injection_simulator_scenarios_preserve_ledger_invariants() -> None:
    orchestrator = PaymentOrchestrator()
    simulator = FailureInjectionSimulator(orchestrator)

    scenarios = [
        FailureScenario.NORMAL,
        FailureScenario.DUPLICATE_WEBHOOK,
        FailureScenario.OUT_OF_ORDER_WEBHOOK,
        FailureScenario.PROVIDER_TIMEOUT_RETRY,
        FailureScenario.FEE_MISMATCH,
        FailureScenario.MISSING_IN_LEDGER,
        FailureScenario.MISSING_AT_PROVIDER,
        FailureScenario.AMOUNT_MISMATCH,
        FailureScenario.CUSTOMER_REVERSAL,
    ]

    for idx, scen in enumerate(scenarios, start=1):
        intent, _ = orchestrator.create_payment_intent(
            idempotency_key=f"scen_key_{idx}",
            payment_id=f"pay_scen_{idx}",
            merchant_id="merch_test",
            customer_msisdn=f"+25677000000{idx}",
            amount=50_000 * idx,
            currency=Currency.UGX,
            provider="mtn_momo" if idx % 2 == 1 else "airtel_money",
        )
        simulator.run_scenario(payment=intent, scenario=scen)

    inv = orchestrator.ledger.verify_invariants()
    assert inv.is_valid is True
    assert inv.journal_count > 0
