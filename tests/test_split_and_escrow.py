"""Unit tests for Marketplace Split Payments and Two-Phase Escrow Management."""

from __future__ import annotations

import pytest

from flowledger.ledger.engine import LedgerEngine, LedgerError
from flowledger.ledger.models import Currency
from flowledger.payments.split import (
    EscrowHold,
    EscrowManager,
    EscrowStatus,
    SplitRecipient,
    SplitRule,
)


def test_split_rule_basis_points_sum_conservation() -> None:
    rule = SplitRule(
        rule_id="marketplace_standard",
        recipients=[
            SplitRecipient("worker_1", "PROVIDER", "liability:merchant:worker_1", basis_points=8800),
            SplitRecipient("platform", "PLATFORM", "revenue:commission:platform", basis_points=1000),
            SplitRecipient("gateway", "GATEWAY", "expense:provider_fee:clearing", basis_points=200),
        ],
    )
    allocs = rule.compute_allocations(100_000)
    assert sum(amount for _, amount in allocs) == 100_000
    assert allocs[0][1] == 88_000
    assert allocs[1][1] == 10_000
    assert allocs[2][1] == 2_000


def test_split_rule_rejects_exceeded_bps() -> None:
    rule = SplitRule(
        rule_id="bad_bps",
        recipients=[
            SplitRecipient("p1", "PROVIDER", "acc1", basis_points=9500),
            SplitRecipient("p2", "PLATFORM", "acc2", basis_points=1000),  # 10500 > 10000
        ],
    )
    with pytest.raises(LedgerError, match="exceed 10,000"):
        rule.compute_allocations(50_000)


def test_split_rule_with_fixed_and_basis_points() -> None:
    rule = SplitRule(
        rule_id="fixed_plus_bps",
        recipients=[
            SplitRecipient("gateway", "GATEWAY", "acc_gw", fixed_amount=1500),
            SplitRecipient("worker", "PROVIDER", "acc_wk", basis_points=9000),
            SplitRecipient("platform", "PLATFORM", "acc_plt", basis_points=1000),
        ],
    )
    # Total 50,000. Fixed 1500 -> remaining 48,500.
    # worker: 90% of 50,000 = 45,000. platform gets remaining to sum to 50,000.
    allocs = rule.compute_allocations(50_000)
    assert sum(amt for _, amt in allocs) == 50_000


def test_escrow_hold_release_and_ledger_zero_sum() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)
    mgr = EscrowManager(ledger)

    rule = SplitRule(
        rule_id="funa_gigs_split",
        recipients=[
            SplitRecipient("plumber_01", "PROVIDER", "liability:merchant:plumber_01", basis_points=8500),
            SplitRecipient("funa_platform", "PLATFORM", "revenue:commission:platform", basis_points=1200),
            SplitRecipient("momo_gw", "GATEWAY", "expense:provider_fee:clearing", basis_points=300),
        ],
    )

    # 1. Create Escrow Hold
    hold = mgr.create_escrow_hold(
        payment_id="pay_gig_101",
        amount=200_000,
        currency=Currency.UGX,
        split_rule=rule,
    )
    assert hold.status == EscrowStatus.HELD
    assert hold.total_amount == 200_000

    # Invariants should pass with hold active
    inv1 = ledger.verify_invariants()
    assert inv1.is_valid
    assert inv1.trial_balance_by_currency["UGX"]["net_imbalance"] == 0

    # Escrow account should have a balance of 200,000
    escrow_acc = ledger.get_account(hold.escrow_account_id)
    assert escrow_acc is not None
    assert escrow_acc.balance == 200_000

    # 2. Release Escrow upon service completion
    released = mgr.release_escrow(hold.escrow_id)
    assert released.status == EscrowStatus.RELEASED
    assert released.resolved_at is not None

    # Escrow liability balance should now be 0
    assert escrow_acc.balance == 0

    # Provider payable should have 85% of 200,000 = 170,000
    worker_acc = ledger.get_account("liability:merchant:plumber_01")
    assert worker_acc is not None
    assert worker_acc.balance == 170_000

    # Verify entire ledger remains zero-sum
    inv2 = ledger.verify_invariants()
    assert inv2.is_valid
    assert inv2.trial_balance_by_currency["UGX"]["net_imbalance"] == 0


def test_escrow_refund_upon_dispute() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)
    mgr = EscrowManager(ledger)

    rule = SplitRule(
        rule_id="dispute_rule",
        recipients=[
            SplitRecipient("worker_x", "PROVIDER", "liability:merchant:worker_x", basis_points=10000)
        ],
    )
    hold = mgr.create_escrow_hold(
        payment_id="pay_disputed_02",
        amount=80_000,
        currency=Currency.UGX,
        split_rule=rule,
    )
    assert hold.status == EscrowStatus.HELD

    refunded = mgr.refund_escrow(hold.escrow_id, reason="Customer canceled gig before arrival")
    assert refunded.status == EscrowStatus.REFUNDED

    escrow_acc = ledger.get_account(hold.escrow_account_id)
    assert escrow_acc.balance == 0

    # Re-release or re-refund should raise
    with pytest.raises(LedgerError, match="cannot be released"):
        mgr.release_escrow(hold.escrow_id)

    with pytest.raises(LedgerError, match="cannot be refunded"):
        mgr.refund_escrow(hold.escrow_id)
