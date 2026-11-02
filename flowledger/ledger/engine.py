"""Double-entry ledger engine enforcing zero-sum invariants and hash-chain integrity."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from flowledger.ledger.models import (
    GENESIS_HASH,
    Account,
    AccountType,
    Currency,
    EntryDirection,
    InvariantReport,
    JournalEntry,
    LedgerPosting,
    validate_minor_units,
)


class LedgerError(Exception):
    """Base exception for double-entry ledger operations."""


class InvalidAmountError(LedgerError):
    """Raised when a non-integer or non-positive monetary amount is supplied."""


class UnbalancedJournalError(LedgerError):
    """Raised when total debits do not equal total credits in a journal entry."""


class CurrencyMismatchError(LedgerError):
    """Raised when postings or accounts have mismatched currencies."""


class AccountNotFoundError(LedgerError):
    """Raised when referencing an unknown ledger account."""


class HashChainTamperError(LedgerError):
    """Raised when cryptographic audit trail verification fails."""


class LedgerEngine:
    """Thread-safe double-entry accounting engine with SHA-256 hash-chained journal entries."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._entries: list[JournalEntry] = []

    def create_account(
        self,
        account_id: str,
        name: str,
        account_type: AccountType | str,
        currency: Currency | str,
    ) -> Account:
        """Create a ledger account or return existing if identical specification exists."""
        with self._lock:
            acc_type = AccountType(account_type)
            curr = Currency(currency)
            existing = self._accounts.get(account_id)
            if existing is not None:
                if existing.account_type != acc_type or existing.currency != curr:
                    raise LedgerError(
                        f"Account '{account_id}' already exists with conflicting type/currency "
                        f"({existing.account_type.value}/{existing.currency.value})"
                    )
                return existing
            account = Account(
                account_id=account_id,
                name=name,
                account_type=acc_type,
                currency=curr,
            )
            self._accounts[account_id] = account
            return account

    def bootstrap_standard_chart_of_accounts(
        self, currency: Currency | str = Currency.UGX
    ) -> dict[str, Account]:
        """Provision the standard payment orchestration chart of accounts for a currency."""
        curr = Currency(currency)
        c_code = curr.value.lower()
        specs = [
            (
                f"asset:clearing:mtn_momo:{c_code}",
                f"MTN MoMo Clearing ({curr.value})",
                AccountType.ASSET,
            ),
            (
                f"asset:clearing:airtel_money:{c_code}",
                f"Airtel Money Clearing ({curr.value})",
                AccountType.ASSET,
            ),
            (
                f"asset:settlement:bank:{c_code}",
                f"Settlement Bank Trust Account ({curr.value})",
                AccountType.ASSET,
            ),
            (
                f"asset:receivable:disputes:{c_code}",
                f"Dispute & Chargeback Receivable ({curr.value})",
                AccountType.ASSET,
            ),
            (
                f"liability:merchant:pending:{c_code}",
                f"Merchant Pending Clearing Liability ({curr.value})",
                AccountType.LIABILITY,
            ),
            (
                f"liability:merchant:payable:{c_code}",
                f"Merchant Settled Payable ({curr.value})",
                AccountType.LIABILITY,
            ),
            (
                f"revenue:platform:fees:{c_code}",
                f"Platform Orchestration Fee Revenue ({curr.value})",
                AccountType.REVENUE,
            ),
            (
                f"expense:provider:fees:{c_code}",
                f"Mobile Money Provider Gateway Expense ({curr.value})",
                AccountType.EXPENSE,
            ),
            (
                f"equity:capital:reserve:{c_code}",
                f"Platform Operational Reserve Equity ({curr.value})",
                AccountType.EQUITY,
            ),
        ]
        created: dict[str, Account] = {}
        for account_id, name, acc_type in specs:
            created[account_id] = self.create_account(account_id, name, acc_type, curr)
        return created

    def get_account(self, account_id: str) -> Account:
        """Retrieve a ledger account by ID."""
        with self._lock:
            if account_id not in self._accounts:
                raise AccountNotFoundError(f"Ledger account '{account_id}' does not exist")
            return self._accounts[account_id]

    def list_accounts(self, currency: Currency | str | None = None) -> list[Account]:
        """Return all registered accounts, optionally filtered by currency."""
        with self._lock:
            accounts = list(self._accounts.values())
            if currency is not None:
                curr = Currency(currency)
                accounts = [a for a in accounts if a.currency == curr]
            return accounts

    def list_entries(self, reference: str | None = None) -> list[JournalEntry]:
        """Return immutable journal entries in chronological order."""
        with self._lock:
            if reference is not None:
                return [e for e in self._entries if e.reference == reference]
            return list(self._entries)

    @property
    def head_hash(self) -> str:
        """Return the SHA-256 hash of the latest journal entry, or GENESIS_HASH if empty."""
        with self._lock:
            if not self._entries:
                return GENESIS_HASH
            return self._entries[-1].entry_hash

    def record_journal_entry(
        self,
        *,
        reference: str,
        description: str,
        currency: Currency | str,
        postings: Sequence[LedgerPosting | dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        entry_id: str | None = None,
        timestamp: str | None = None,
    ) -> JournalEntry:
        """Validate and commit a balanced double-entry journal entry to the hash chain."""
        with self._lock:
            curr = Currency(currency)
            if not postings or len(postings) < 2:
                raise UnbalancedJournalError(
                    "A double-entry journal transaction requires at least 2 postings"
                )

            normalized_postings: list[LedgerPosting] = []
            for raw in postings:
                if isinstance(raw, LedgerPosting):
                    posting = raw
                elif isinstance(raw, dict):
                    try:
                        validate_minor_units(raw.get("amount"), allow_zero=False)
                    except (TypeError, ValueError) as exc:
                        raise InvalidAmountError(str(exc)) from exc
                    posting = LedgerPosting(
                        account_id=str(raw["account_id"]),
                        direction=EntryDirection(raw["direction"]),
                        amount=raw["amount"],
                        currency=Currency(raw.get("currency", curr)),
                    )
                else:
                    raise LedgerError(f"Unsupported posting specification: {raw!r}")

                if posting.currency != curr:
                    raise CurrencyMismatchError(
                        f"Posting currency {posting.currency.value} does not match "
                        f"journal currency {curr.value}"
                    )
                account = self.get_account(posting.account_id)
                if account.currency != curr:
                    raise CurrencyMismatchError(
                        f"Account '{account.account_id}' currency {account.currency.value} "
                        f"does not match journal currency {curr.value}"
                    )
                normalized_postings.append(posting)

            debits = sum(
                p.amount for p in normalized_postings if p.direction == EntryDirection.DEBIT
            )
            credits = sum(
                p.amount for p in normalized_postings if p.direction == EntryDirection.CREDIT
            )

            if debits <= 0 or credits <= 0:
                raise UnbalancedJournalError(
                    f"Journal entry must have positive debits and credits (debits={debits}, credits={credits})"
                )
            if debits != credits:
                raise UnbalancedJournalError(
                    f"Double-entry invariant violation for reference '{reference}': "
                    f"sum(debits)={debits} != sum(credits)={credits} ({curr.value})"
                )

            resolved_id = entry_id or f"je_{uuid.uuid4().hex[:16]}"
            resolved_ts = timestamp or datetime.now(timezone.utc).isoformat()
            resolved_meta = dict(metadata or {})
            prev_hash = self.head_hash
            postings_tuple = tuple(normalized_postings)

            entry_hash = JournalEntry.compute_hash(
                entry_id=resolved_id,
                reference=reference,
                description=description,
                currency=curr,
                postings=postings_tuple,
                timestamp=resolved_ts,
                prev_hash=prev_hash,
                metadata=resolved_meta,
            )

            journal_entry = JournalEntry(
                entry_id=resolved_id,
                reference=reference,
                description=description,
                currency=curr,
                postings=postings_tuple,
                timestamp=resolved_ts,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
                metadata=resolved_meta,
            )

            # Apply atomic balance updates
            for posting in postings_tuple:
                acc = self._accounts[posting.account_id]
                if posting.direction == EntryDirection.DEBIT:
                    acc.debit_total += posting.amount
                else:
                    acc.credit_total += posting.amount

            self._entries.append(journal_entry)
            return journal_entry

    def verify_invariants(self) -> InvariantReport:
        """Verify zero-sum accounting invariants, account tallies, and SHA-256 hash chain."""
        with self._lock:
            errors: list[str] = []
            expected_prev_hash = GENESIS_HASH

            recomputed_debits: dict[str, int] = { acc_id: 0 for acc_id in self._accounts }
            recomputed_credits: dict[str, int] = { acc_id: 0 for acc_id in self._accounts }

            for idx, entry in enumerate(self._entries):
                # 1. Verify hash chain continuity
                if entry.prev_hash != expected_prev_hash:
                    errors.append(
                        f"Entry #{idx} ({entry.entry_id}) prev_hash mismatch: "
                        f"expected {expected_prev_hash}, found {entry.prev_hash}"
                    )

                expected_hash = JournalEntry.compute_hash(
                    entry_id=entry.entry_id,
                    reference=entry.reference,
                    description=entry.description,
                    currency=entry.currency,
                    postings=entry.postings,
                    timestamp=entry.timestamp,
                    prev_hash=entry.prev_hash,
                    metadata=entry.metadata,
                )
                if entry.entry_hash != expected_hash:
                    errors.append(
                        f"Entry #{idx} ({entry.entry_id}) cryptographic hash tamper detected: "
                        f"expected {expected_hash}, found {entry.entry_hash}"
                    )
                expected_prev_hash = entry.entry_hash

                # 2. Verify entry-level zero-sum invariant
                if entry.total_debits != entry.total_credits:
                    errors.append(
                        f"Entry #{idx} ({entry.entry_id}) unbalanced: "
                        f"debits={entry.total_debits} != credits={entry.total_credits}"
                    )

                for posting in entry.postings:
                    if posting.account_id not in self._accounts:
                        errors.append(
                            f"Entry #{idx} ({entry.entry_id}) references missing account {posting.account_id}"
                        )
                        continue
                    if posting.direction == EntryDirection.DEBIT:
                        recomputed_debits[posting.account_id] += posting.amount
                    else:
                        recomputed_credits[posting.account_id] += posting.amount

            # 3. Verify account totals match journal postings
            for acc_id, acc in self._accounts.items():
                if acc.debit_total != recomputed_debits[acc_id]:
                    errors.append(
                        f"Account '{acc_id}' debit_total ({acc.debit_total}) != "
                        f"journal sum ({recomputed_debits[acc_id]})"
                    )
                if acc.credit_total != recomputed_credits[acc_id]:
                    errors.append(
                        f"Account '{acc_id}' credit_total ({acc.credit_total}) != "
                        f"journal sum ({recomputed_credits[acc_id]})"
                    )

            # 4. Verify trial balance and accounting equation per currency
            trial_balance: dict[str, dict[str, int]] = {}
            currencies = sorted({acc.currency.value for acc in self._accounts.values()})
            for curr_code in currencies:
                curr_accounts = [
                    a for a in self._accounts.values() if a.currency.value == curr_code
                ]
                total_debits = sum(a.debit_total for a in curr_accounts)
                total_credits = sum(a.credit_total for a in curr_accounts)
                debit_normal_net = sum(
                    a.balance for a in curr_accounts if a.account_type.is_debit_normal
                )
                credit_normal_net = sum(
                    a.balance for a in curr_accounts if not a.account_type.is_debit_normal
                )
                net_imbalance = debit_normal_net - credit_normal_net
                trial_balance[curr_code] = {
                    "total_debits": total_debits,
                    "total_credits": total_credits,
                    "debit_normal_balance": debit_normal_net,
                    "credit_normal_balance": credit_normal_net,
                    "net_imbalance": net_imbalance,
                }
                if total_debits != total_credits or net_imbalance != 0:
                    errors.append(
                        f"Currency {curr_code} trial balance violation: "
                        f"total_debits={total_debits}, total_credits={total_credits}, "
                        f"net_imbalance={net_imbalance}"
                    )

            return InvariantReport(
                is_valid=len(errors) == 0,
                journal_count=len(self._entries),
                account_count=len(self._accounts),
                head_hash=self.head_hash,
                currencies_verified=currencies,
                trial_balance_by_currency=trial_balance,
                errors=errors,
            )
