"""Marketplace Multi-Party Split Payments & Two-Phase Escrow Hold-Release Engine.

Enforces zero-sum mathematical invariants across gig-economy and marketplace platforms
(e.g., Funa Gigs) with two-phase escrow lifecycles and integer minor-unit allocations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from flowledger.ledger.engine import LedgerEngine, LedgerError
from flowledger.ledger.models import (
    AccountType,
    Currency,
    EntryDirection,
    LedgerPosting,
    validate_minor_units,
)


class EscrowStatus(str, Enum):
    """Lifecycle states for a marketplace escrow deposit."""

    HELD = "HELD"
    RELEASED = "RELEASED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True)
class SplitRecipient:
    """Designation for a split allocation leg."""

    recipient_id: str
    role: str  # e.g., "PROVIDER", "PLATFORM", "AFFILIATE", "GATEWAY"
    account_id: str
    basis_points: int | None = None  # 100 bps = 1.00%, 10_000 bps = 100.00%
    fixed_amount: int | None = None  # integer minor units


@dataclass(frozen=True)
class SplitRule:
    """Collection of split recipient allocations for a marketplace payment."""

    rule_id: str
    recipients: list[SplitRecipient]

    def compute_allocations(self, total_amount: int) -> list[tuple[SplitRecipient, int]]:
        """Compute exact integer minor-unit allocations ensuring sum(allocations) == total_amount."""
        validate_minor_units(total_amount, allow_zero=False)
        allocations: list[tuple[SplitRecipient, int]] = []
        allocated_so_far = 0

        # Process fixed amounts first
        for r in self.recipients:
            if r.fixed_amount is not None:
                amt = validate_minor_units(r.fixed_amount, allow_zero=False)
                allocations.append((r, amt))
                allocated_so_far += amt

        remaining = total_amount - allocated_so_far
        if remaining < 0:
            raise LedgerError(
                f"Fixed split allocations ({allocated_so_far}) exceed total amount ({total_amount})"
            )

        # Process basis point allocations
        bps_pairs = [
            (r, r.basis_points)
            for r in self.recipients
            if r.basis_points is not None
        ]
        total_bps = sum(bps for _, bps in bps_pairs)
        if total_bps > 10_000:
            raise LedgerError(f"Total split basis points exceed 10,000 (100%): {total_bps} bps")

        for idx, (r, bps) in enumerate(bps_pairs):
            if idx == len(bps_pairs) - 1 and total_bps == 10_000:
                # Absorb integer division rounding into last recipient to guarantee zero minor-unit leakage
                amt = total_amount - allocated_so_far
            else:
                amt = (total_amount * bps) // 10_000
            allocations.append((r, amt))
            allocated_so_far += amt

        if allocated_so_far != total_amount:
            raise LedgerError(
                f"Split allocations sum ({allocated_so_far}) does not match total amount ({total_amount})"
            )

        return allocations


@dataclass
class EscrowHold:
    """Escrow deposit record securing customer funds prior to milestone delivery."""

    escrow_id: str
    payment_id: str
    total_amount: int
    currency: Currency
    status: EscrowStatus = EscrowStatus.HELD
    escrow_account_id: str = ""
    source_clearing_account: str = ""
    held_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str | None = None
    split_rule: SplitRule | None = None
    allocations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "escrow_id": self.escrow_id,
            "payment_id": self.payment_id,
            "total_amount": self.total_amount,
            "currency": self.currency.value,
            "status": self.status.value,
            "escrow_account_id": self.escrow_account_id,
            "source_clearing_account": self.source_clearing_account,
            "held_at": self.held_at,
            "resolved_at": self.resolved_at,
            "allocations": list(self.allocations),
        }


class EscrowManager:
    """Coordinates marketplace escrow holds, releases, and dispute refunds with double-entry ledger."""

    def __init__(self, ledger: LedgerEngine) -> None:
        self.ledger = ledger
        self._holds: dict[str, EscrowHold] = {}

    def create_escrow_hold(
        self,
        payment_id: str,
        amount: int,
        currency: Currency | str,
        split_rule: SplitRule,
        source_clearing_account: str = "asset:clearing:mtn_momo:ugx",
        escrow_id: str | None = None,
    ) -> EscrowHold:
        """Lock customer payment in an immutable escrow liability account pending service verification."""
        curr = Currency(currency)
        validate_minor_units(amount, allow_zero=False)
        e_id = escrow_id or f"esc_{uuid.uuid4().hex[:12]}"

        escrow_account_id = f"liability:escrow:{curr.value.lower()}:{e_id}"
        self.ledger.create_account(
            account_id=escrow_account_id,
            name=f"Escrow Hold ({e_id})",
            account_type=AccountType.LIABILITY,
            currency=curr,
        )

        computed = split_rule.compute_allocations(amount)
        alloc_dicts = [
            {
                "recipient_id": r.recipient_id,
                "role": r.role,
                "account_id": r.account_id,
                "amount": leg_amount,
            }
            for r, leg_amount in computed
        ]

        # Double-entry posting: Debit clearing asset, Credit escrow liability
        postings = [
            LedgerPosting(
                account_id=source_clearing_account,
                direction=EntryDirection.DEBIT,
                amount=amount,
                currency=curr,
            ),
            LedgerPosting(
                account_id=escrow_account_id,
                direction=EntryDirection.CREDIT,
                amount=amount,
                currency=curr,
            ),
        ]

        self.ledger.record_journal_entry(
            reference=payment_id,
            description=f"Escrow deposit held for payment {payment_id} ({e_id})",
            currency=curr,
            postings=postings,
            metadata={"escrow_id": e_id, "type": "ESCROW_HOLD"},
        )

        hold = EscrowHold(
            escrow_id=e_id,
            payment_id=payment_id,
            total_amount=amount,
            currency=curr,
            status=EscrowStatus.HELD,
            escrow_account_id=escrow_account_id,
            source_clearing_account=source_clearing_account,
            split_rule=split_rule,
            allocations=alloc_dicts,
        )
        self._holds[e_id] = hold
        return hold

    def release_escrow(self, escrow_id: str) -> EscrowHold:
        """Release escrowed funds to marketplace service provider and platform commission accounts."""
        hold = self._holds.get(escrow_id)
        if hold is None:
            raise LedgerError(f"Escrow hold not found: {escrow_id}")
        if hold.status != EscrowStatus.HELD:
            raise LedgerError(f"Escrow hold '{escrow_id}' cannot be released from status {hold.status.value}")

        curr = hold.currency
        total_amount = hold.total_amount

        # Debit escrow liability (clearing the hold)
        postings = [
            LedgerPosting(
                account_id=hold.escrow_account_id,
                direction=EntryDirection.DEBIT,
                amount=total_amount,
                currency=curr,
            )
        ]

        # Credit each split recipient
        for alloc in hold.allocations:
            acc_id = alloc["account_id"]
            amt = alloc["amount"]
            # Ensure target account exists
            acc_type = AccountType.REVENUE if alloc["role"] == "PLATFORM" else AccountType.LIABILITY
            self.ledger.create_account(
                account_id=acc_id,
                name=f"Marketplace Account ({alloc['recipient_id']})",
                account_type=acc_type,
                currency=curr,
            )
            postings.append(
                LedgerPosting(
                    account_id=acc_id,
                    direction=EntryDirection.CREDIT,
                    amount=amt,
                    currency=curr,
                )
            )

        self.ledger.record_journal_entry(
            reference=hold.payment_id,
            description=f"Escrow release for payment {hold.payment_id} ({escrow_id})",
            currency=curr,
            postings=postings,
            metadata={"escrow_id": escrow_id, "type": "ESCROW_RELEASE"},
        )

        hold.status = EscrowStatus.RELEASED
        hold.resolved_at = datetime.now(timezone.utc).isoformat()
        return hold

    def refund_escrow(
        self,
        escrow_id: str,
        reason: str = "Marketplace gig dispute / task canceled",
        refund_destination_account: str | None = None,
    ) -> EscrowHold:
        """Refund escrowed funds back to customer clearing / payable account."""
        hold = self._holds.get(escrow_id)
        if hold is None:
            raise LedgerError(f"Escrow hold not found: {escrow_id}")
        if hold.status != EscrowStatus.HELD:
            raise LedgerError(f"Escrow hold '{escrow_id}' cannot be refunded from status {hold.status.value}")

        curr = hold.currency
        dest_account = refund_destination_account or hold.source_clearing_account
        total_amount = hold.total_amount

        postings = [
            LedgerPosting(
                account_id=hold.escrow_account_id,
                direction=EntryDirection.DEBIT,
                amount=total_amount,
                currency=curr,
            ),
            LedgerPosting(
                account_id=dest_account,
                direction=EntryDirection.CREDIT,
                amount=total_amount,
                currency=curr,
            ),
        ]

        self.ledger.record_journal_entry(
            reference=hold.payment_id,
            description=f"Escrow refund for {hold.payment_id}: {reason}",
            currency=curr,
            postings=postings,
            metadata={"escrow_id": escrow_id, "type": "ESCROW_REFUND", "reason": reason},
        )

        hold.status = EscrowStatus.REFUNDED
        hold.resolved_at = datetime.now(timezone.utc).isoformat()
        return hold

    def get_hold(self, escrow_id: str) -> EscrowHold | None:
        return self._holds.get(escrow_id)
