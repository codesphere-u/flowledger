"""Tests for the deterministic payment state machine, out-of-order webhooks, and idempotency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from flowledger.ledger.models import Currency
from flowledger.payments import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentOrchestrator,
    PaymentState,
    PaymentStateMachine,
)


def test_valid_standard_lifecycle_transitions() -> None:
    orchestrator = PaymentOrchestrator()
    intent, replayed = orchestrator.create_payment_intent(
        idempotency_key="idem_std_1",
        merchant_id="merch_1",
        customer_msisdn="+256771000111",
        amount=100_000,
        currency=Currency.UGX,
        provider="mtn_momo",
    )
    assert replayed is False
    assert intent.state == PaymentState.PENDING_PROVIDER

    r_auth = orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED, webhook_id="wh_1")
    assert r_auth.applied is True
    assert intent.state == PaymentState.AUTHORIZED

    r_settle = orchestrator.transition_payment(intent.payment_id, PaymentState.SETTLED, webhook_id="wh_2")
    assert r_settle.applied is True
    assert intent.state == PaymentState.SETTLED
    assert len(intent.journal_entry_ids) == 2
    assert orchestrator.ledger.verify_invariants().is_valid is True


def test_failed_to_settled_blocked_without_reconciliation_override() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_fail_1",
        merchant_id="merch_1",
        customer_msisdn="+256771000222",
        amount=50_000,
    )
    orchestrator.transition_payment(intent.payment_id, PaymentState.FAILED)
    assert intent.state == PaymentState.FAILED

    with pytest.raises(InvalidStateTransitionError, match="without explicit reconciliation exception override"):
        orchestrator.transition_payment(intent.payment_id, PaymentState.SETTLED)


def test_failed_to_settled_succeeds_with_explicit_reconciliation_override() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_fail_override",
        merchant_id="merch_1",
        customer_msisdn="+256771000333",
        amount=80_000,
    )
    orchestrator.transition_payment(intent.payment_id, PaymentState.FAILED)

    rec = orchestrator.transition_payment(
        intent.payment_id,
        PaymentState.SETTLED,
        allow_reconciliation_override=True,
    )
    assert rec.applied is True
    assert rec.reconciliation_override is True
    assert intent.state == PaymentState.SETTLED
    assert len(intent.journal_entry_ids) == 2
    assert orchestrator.ledger.verify_invariants().is_valid is True


def test_out_of_order_settled_before_authorized_webhook_handled_safely() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_ooo_1",
        merchant_id="merch_1",
        customer_msisdn="+256771000444",
        amount=60_000,
    )
    # 1. SETTLED arrives first while in PENDING_PROVIDER
    r1 = orchestrator.transition_payment(
        intent.payment_id, PaymentState.SETTLED, webhook_id="wh_settle_early"
    )
    assert r1.applied is True
    assert r1.implicit_intermediate_states == (PaymentState.AUTHORIZED,)
    assert intent.state == PaymentState.SETTLED
    assert len(intent.journal_entry_ids) == 2

    # 2. Delayed AUTHORIZED webhook arrives later -> safely absorbed as out_of_order_noop
    r2 = orchestrator.transition_payment(
        intent.payment_id, PaymentState.AUTHORIZED, webhook_id="wh_auth_late"
    )
    assert r2.applied is False
    assert r2.out_of_order_noop is True
    assert intent.state == PaymentState.SETTLED
    assert len(intent.journal_entry_ids) == 2  # No duplicate ledger postings!
    assert orchestrator.ledger.verify_invariants().is_valid is True


def test_duplicate_webhook_id_and_same_state_delivery_are_idempotent() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_dup_wh",
        merchant_id="merch_1",
        customer_msisdn="+256771000555",
        amount=40_000,
    )
    orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED, webhook_id="wh_auth_1")
    r_dup_id = orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED, webhook_id="wh_auth_1")
    r_dup_state = orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED, webhook_id="wh_auth_2")

    assert r_dup_id.applied is False and r_dup_id.idempotent_noop is True
    assert r_dup_state.applied is False and r_dup_state.idempotent_noop is True
    assert len(intent.journal_entry_ids) == 1


def test_dispute_and_reversal_transitions_preserve_zero_sum_ledger() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_dispute_1",
        merchant_id="merch_1",
        customer_msisdn="+256771000666",
        amount=120_000,
    )
    orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED)
    orchestrator.transition_payment(intent.payment_id, PaymentState.SETTLED)
    orchestrator.transition_payment(intent.payment_id, PaymentState.DISPUTED)
    assert intent.state == PaymentState.DISPUTED
    assert orchestrator.ledger.verify_invariants().is_valid is True


def test_reversal_from_settled_restores_zero_net_balances() -> None:
    orchestrator = PaymentOrchestrator()
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idem_rev_1",
        merchant_id="merch_1",
        customer_msisdn="+256771000777",
        amount=100_000,
        currency=Currency.UGX,
    )
    orchestrator.transition_payment(intent.payment_id, PaymentState.AUTHORIZED)
    orchestrator.transition_payment(intent.payment_id, PaymentState.SETTLED)
    orchestrator.transition_payment(intent.payment_id, PaymentState.REVERSED)

    assert intent.state == PaymentState.REVERSED
    assert orchestrator.ledger.get_account("asset:settlement:bank:ugx").balance == 0
    assert orchestrator.ledger.get_account("liability:merchant:payable:ugx").balance == 0
    assert orchestrator.ledger.get_account("revenue:platform:fees:ugx").balance == 0
    assert orchestrator.ledger.verify_invariants().is_valid is True


def test_illegal_reverse_transition_settled_to_failed_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        PaymentStateMachine.evaluate_transition(PaymentState.SETTLED, PaymentState.FAILED)


def test_idempotency_replay_returns_identical_payment_without_duplicate_side_effects() -> None:
    orchestrator = PaymentOrchestrator()
    p1, replayed1 = orchestrator.create_payment_intent(
        idempotency_key="key_replay_100",
        merchant_id="merch_alpha",
        customer_msisdn="+256701112233",
        amount=75_000,
        currency=Currency.UGX,
        provider="airtel_money",
    )
    p2, replayed2 = orchestrator.create_payment_intent(
        idempotency_key="key_replay_100",
        merchant_id="merch_alpha",
        customer_msisdn="+256701112233",
        amount=75_000,
        currency=Currency.UGX,
        provider="airtel_money",
    )
    assert replayed1 is False
    assert replayed2 is True
    assert p1.payment_id == p2.payment_id
    assert len(orchestrator.list_payments()) == 1


def test_idempotency_conflict_raises_on_mutated_amount_or_parameters() -> None:
    orchestrator = PaymentOrchestrator()
    orchestrator.create_payment_intent(
        idempotency_key="key_conflict_200",
        merchant_id="merch_alpha",
        customer_msisdn="+256701112233",
        amount=75_000,
        currency=Currency.UGX,
    )
    with pytest.raises(IdempotencyConflictError):
        orchestrator.create_payment_intent(
            idempotency_key="key_conflict_200",
            merchant_id="merch_alpha",
            customer_msisdn="+256701112233",
            amount=85_000,  # Mutated amount with same idempotency_key!
            currency=Currency.UGX,
        )


def test_empty_idempotency_key_rejected() -> None:
    orchestrator = PaymentOrchestrator()
    with pytest.raises(ValueError, match="idempotency_key"):
        orchestrator.create_payment_intent(
            idempotency_key="   ",
            merchant_id="merch_alpha",
            customer_msisdn="+256701112233",
            amount=50_000,
        )


def test_concurrent_idempotent_requests_create_exactly_one_payment() -> None:
    orchestrator = PaymentOrchestrator()

    def _worker() -> tuple[str, bool]:
        intent, replayed = orchestrator.create_payment_intent(
            idempotency_key="concurrent_key_999",
            merchant_id="merch_concurrent",
            customer_msisdn="+256779999999",
            amount=30_000,
        )
        return intent.payment_id, replayed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _worker(), range(16)))

    payment_ids = {pid for pid, _ in results}
    created_count = sum(1 for _, replayed in results if not replayed)
    replayed_count = sum(1 for _, replayed in results if replayed)

    assert len(payment_ids) == 1
    assert created_count == 1
    assert replayed_count == 15
