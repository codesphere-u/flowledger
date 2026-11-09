"""Domain models for the FlowLedger immutable double-entry ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

GENESIS_HASH = "0" * 64


class Currency(str, Enum):
    """Supported currencies represented strictly in integer minor units."""

    UGX = "UGX"
    KES = "KES"
    NGN = "NGN"
    USD = "USD"
    GHS = "GHS"
    ZAR = "ZAR"
    TZS = "TZS"
    RWF = "RWF"
    XOF = "XOF"  # West African CFA franc (Wave, Orange Money)
    XAF = "XAF"  # Central African CFA franc
    USDC = "USDC"  # Stablecoin settlement unit (6 decimals; minor unit = 1e-6)
    EURC = "EURC"


class AccountType(str, Enum):
    """Standard double-entry accounting classifications."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"

    @property
    def is_debit_normal(self) -> bool:
        """Return True if debits increase the normal balance of this account type."""
        return self in (AccountType.ASSET, AccountType.EXPENSE)


class EntryDirection(str, Enum):
    """Posting leg direction."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


def validate_minor_units(amount: Any, *, allow_zero: bool = False) -> int:
    """Validate that an amount is strictly an integer in minor units (never float or bool)."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError(
            f"Monetary amounts must be integer minor units, got {type(amount).__name__}: {amount!r}"
        )
    if allow_zero:
        if amount < 0:
            raise ValueError(f"Monetary amount must be non-negative, got {amount}")
    else:
        if amount <= 0:
            raise ValueError(f"Monetary amount must be strictly positive (> 0), got {amount}")
    return amount


@dataclass
class Account:
    """Ledger account maintaining integer minor-unit debit and credit totals."""

    account_id: str
    name: str
    account_type: AccountType
    currency: Currency
    debit_total: int = 0
    credit_total: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        self.account_type = AccountType(self.account_type)
        self.currency = Currency(self.currency)
        validate_minor_units(self.debit_total, allow_zero=True)
        validate_minor_units(self.credit_total, allow_zero=True)

    @property
    def balance(self) -> int:
        """Return the normal balance in integer minor units based on account classification."""
        if self.account_type.is_debit_normal:
            return self.debit_total - self.credit_total
        return self.credit_total - self.debit_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "account_type": self.account_type.value,
            "currency": self.currency.value,
            "debit_total": self.debit_total,
            "credit_total": self.credit_total,
            "balance": self.balance,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LedgerPosting:
    """An immutable single leg (debit or credit) of a double-entry journal transaction."""

    account_id: str
    direction: EntryDirection
    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", EntryDirection(self.direction))
        object.__setattr__(self, "currency", Currency(self.currency))
        validate_minor_units(self.amount, allow_zero=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "direction": self.direction.value,
            "amount": self.amount,
            "currency": self.currency.value,
        }


@dataclass(frozen=True)
class JournalEntry:
    """An immutable, balanced, cryptographically hash-chained double-entry journal record."""

    entry_id: str
    reference: str
    description: str
    currency: Currency
    postings: tuple[LedgerPosting, ...]
    timestamp: str
    prev_hash: str
    entry_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_hash(
        *,
        entry_id: str,
        reference: str,
        description: str,
        currency: Currency,
        postings: tuple[LedgerPosting, ...],
        timestamp: str,
        prev_hash: str,
        metadata: dict[str, Any],
    ) -> str:
        """Compute deterministic SHA-256 hash chaining prev_hash and canonical entry payload."""
        canonical_payload = json.dumps(
            {
                "entry_id": entry_id,
                "reference": reference,
                "description": description,
                "currency": Currency(currency).value,
                "timestamp": timestamp,
                "postings": [p.to_dict() for p in postings],
                "metadata": metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        material = f"{prev_hash}{canonical_payload}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def total_debits(self) -> int:
        return sum(p.amount for p in self.postings if p.direction == EntryDirection.DEBIT)

    @property
    def total_credits(self) -> int:
        return sum(p.amount for p in self.postings if p.direction == EntryDirection.CREDIT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "reference": self.reference,
            "description": self.description,
            "currency": self.currency.value,
            "total_debits": self.total_debits,
            "total_credits": self.total_credits,
            "postings": [p.to_dict() for p in self.postings],
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class InvariantReport:
    """Summary of mathematical and cryptographic ledger invariant checks."""

    is_valid: bool
    journal_count: int
    account_count: int
    head_hash: str
    currencies_verified: list[str]
    trial_balance_by_currency: dict[str, dict[str, int]]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "journal_count": self.journal_count,
            "account_count": self.account_count,
            "head_hash": self.head_hash,
            "currencies_verified": self.currencies_verified,
            "trial_balance_by_currency": self.trial_balance_by_currency,
            "errors": self.errors,
        }
