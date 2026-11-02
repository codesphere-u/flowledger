"""Unit and mathematical invariant tests for the FlowLedger double-entry accounting engine."""

from __future__ import annotations

import pytest

from flowledger.ledger import (
    GENESIS_HASH,
    AccountNotFoundError,
    AccountType,
    Currency,
    CurrencyMismatchError,
    EntryDirection,
    InvalidAmountError,
    JournalEntry,
    LedgerEngine,
    LedgerPosting,
    UnbalancedJournalError,
    validate_minor_units,
)


def test_validate_minor_units_accepts_positive_int() -> None:
    assert validate_minor_units(10_500) == 10_500
    assert validate_minor_units(0, allow_zero=True) == 0


@pytest.mark.parametrize("bad_value", [12.34, 0.0, True, False, "1000", None, [100]])
def test_validate_minor_units_rejects_floats_bools_and_non_ints(bad_value: object) -> None:
    with pytest.raises(TypeError):
        validate_minor_units(bad_value)


@pytest.mark.parametrize("non_positive", [0, -1, -500_000])
def test_validate_minor_units_rejects_zero_and_negative_by_default(non_positive: int) -> None:
    with pytest.raises(ValueError):
        validate_minor_units(non_positive)


def test_account_normal_balances_debit_vs_credit() -> None:
    ledger = LedgerEngine()
    asset = ledger.create_account("asset:cash:ugx", "Cash UGX", AccountType.ASSET, Currency.UGX)
    liab = ledger.create_account("liability:merchant:ugx", "Merchant UGX", AccountType.LIABILITY, Currency.UGX)
    rev = ledger.create_account("revenue:fees:ugx", "Fees UGX", AccountType.REVENUE, Currency.UGX)
    exp = ledger.create_account("expense:gateway:ugx", "Gateway UGX", AccountType.EXPENSE, Currency.UGX)

    assert asset.account_type.is_debit_normal is True
    assert exp.account_type.is_debit_normal is True
    assert liab.account_type.is_debit_normal is False
    assert rev.account_type.is_debit_normal is False

    ledger.record_journal_entry(
        reference="tx_001",
        description="Collection with fees",
        currency=Currency.UGX,
        postings=[
            {"account_id": "asset:cash:ugx", "direction": "DEBIT", "amount": 98_500},
            {"account_id": "expense:gateway:ugx", "direction": "DEBIT", "amount": 1_500},
            {"account_id": "liability:merchant:ugx", "direction": "CREDIT", "amount": 97_500},
            {"account_id": "revenue:fees:ugx", "direction": "CREDIT", "amount": 2_500},
        ],
    )

    assert asset.balance == 98_500
    assert exp.balance == 1_500
    assert liab.balance == 97_500
    assert rev.balance == 2_500
    assert ledger.verify_invariants().is_valid is True


def test_unbalanced_journal_entry_rejected() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)

    with pytest.raises(UnbalancedJournalError, match="sum\\(debits\\)=50000 != sum\\(credits\\)=49000"):
        ledger.record_journal_entry(
            reference="tx_unbalanced",
            description="Imbalanced attempt",
            currency=Currency.UGX,
            postings=[
                {"account_id": "asset:clearing:mtn_momo:ugx", "direction": "DEBIT", "amount": 50_000},
                {"account_id": "liability:merchant:pending:ugx", "direction": "CREDIT", "amount": 49_000},
            ],
        )
    assert len(ledger.list_entries()) == 0


def test_single_leg_journal_entry_rejected() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)

    with pytest.raises(UnbalancedJournalError, match="at least 2 postings"):
        ledger.record_journal_entry(
            reference="tx_single",
            description="Single leg",
            currency=Currency.UGX,
            postings=[
                {"account_id": "asset:clearing:mtn_momo:ugx", "direction": "DEBIT", "amount": 10_000},
            ],
        )


def test_float_amount_in_journal_posting_rejected() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)

    with pytest.raises(InvalidAmountError):
        ledger.record_journal_entry(
            reference="tx_float",
            description="Float amount rejected",
            currency=Currency.UGX,
            postings=[
                {"account_id": "asset:clearing:mtn_momo:ugx", "direction": "DEBIT", "amount": 100.50},
                {"account_id": "liability:merchant:pending:ugx", "direction": "CREDIT", "amount": 100.50},
            ],
        )


def test_currency_mismatch_between_entry_and_account_rejected() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)
    ledger.bootstrap_standard_chart_of_accounts(Currency.KES)

    with pytest.raises(CurrencyMismatchError):
        ledger.record_journal_entry(
            reference="tx_curr_mismatch",
            description="Cross-currency without FX account",
            currency=Currency.UGX,
            postings=[
                {"account_id": "asset:clearing:mtn_momo:ugx", "direction": "DEBIT", "amount": 10_000},
                {"account_id": "liability:merchant:pending:kes", "direction": "CREDIT", "amount": 10_000},
            ],
        )


def test_unknown_account_raises_account_not_found() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)

    with pytest.raises(AccountNotFoundError):
        ledger.record_journal_entry(
            reference="tx_missing_acc",
            description="Missing account",
            currency=Currency.UGX,
            postings=[
                {"account_id": "asset:non_existent:ugx", "direction": "DEBIT", "amount": 10_000},
                {"account_id": "liability:merchant:pending:ugx", "direction": "CREDIT", "amount": 10_000},
            ],
        )


def test_hash_chain_continuity_across_multiple_entries() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.NGN)
    assert ledger.head_hash == GENESIS_HASH

    e1 = ledger.record_journal_entry(
        reference="ngn_1",
        description="First NGN entry",
        currency=Currency.NGN,
        postings=[
            LedgerPosting("asset:clearing:mtn_momo:ngn", EntryDirection.DEBIT, 250_000, Currency.NGN),
            LedgerPosting("liability:merchant:pending:ngn", EntryDirection.CREDIT, 250_000, Currency.NGN),
        ],
    )
    assert e1.prev_hash == GENESIS_HASH
    assert len(e1.entry_hash) == 64

    e2 = ledger.record_journal_entry(
        reference="ngn_2",
        description="Second NGN entry",
        currency=Currency.NGN,
        postings=[
            LedgerPosting("asset:settlement:bank:ngn", EntryDirection.DEBIT, 250_000, Currency.NGN),
            LedgerPosting("asset:clearing:mtn_momo:ngn", EntryDirection.CREDIT, 250_000, Currency.NGN),
        ],
    )
    assert e2.prev_hash == e1.entry_hash
    assert ledger.head_hash == e2.entry_hash
    assert ledger.verify_invariants().is_valid is True


def test_invariant_verifier_detects_cryptographic_hash_tampering() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.USD)
    e1 = ledger.record_journal_entry(
        reference="usd_1",
        description="Original description",
        currency=Currency.USD,
        postings=[
            {"account_id": "asset:clearing:mtn_momo:usd", "direction": "DEBIT", "amount": 5_000},
            {"account_id": "liability:merchant:pending:usd", "direction": "CREDIT", "amount": 5_000},
        ],
    )
    # Simulate memory/storage tampering on frozen JournalEntry description without updating hash
    tampered = JournalEntry(
        entry_id=e1.entry_id,
        reference=e1.reference,
        description="Tampered description",
        currency=e1.currency,
        postings=e1.postings,
        timestamp=e1.timestamp,
        prev_hash=e1.prev_hash,
        entry_hash=e1.entry_hash,
        metadata=e1.metadata,
    )
    ledger._entries[0] = tampered

    report = ledger.verify_invariants()
    assert report.is_valid is False
    assert any("cryptographic hash tamper detected" in err for err in report.errors)


def test_invariant_verifier_detects_account_balance_drift() -> None:
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts(Currency.UGX)
    ledger.record_journal_entry(
        reference="ugx_drift",
        description="Valid entry",
        currency=Currency.UGX,
        postings=[
            {"account_id": "asset:clearing:mtn_momo:ugx", "direction": "DEBIT", "amount": 20_000},
            {"account_id": "liability:merchant:pending:ugx", "direction": "CREDIT", "amount": 20_000},
        ],
    )
    # Tamper directly with account debit_total
    acc = ledger.get_account("asset:clearing:mtn_momo:ugx")
    acc.debit_total += 500

    report = ledger.verify_invariants()
    assert report.is_valid is False
    assert any("debit_total" in err for err in report.errors)
