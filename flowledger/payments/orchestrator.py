"""Payment orchestration engine linking state machine, idempotency, and double-entry postings."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from flowledger.ledger.engine import LedgerEngine
from flowledger.ledger.models import Currency, EntryDirection, validate_minor_units
from flowledger.payments.idempotency import IdempotencyStore
from flowledger.payments.state_machine import (
    PaymentState,
    PaymentStateMachine,
    StateTransitionRecord,
)


@dataclass
class PaymentIntent:
    """Internal payment order tracking customer collection, fees, state history, and ledger links."""

    payment_id: str
    idempotency_key: str
    merchant_id: str
    customer_msisdn: str
    provider: str
    amount: int
    currency: Currency
    platform_fee: int
    provider_fee: int
    state: PaymentState = PaymentState.INITIATED
    external_reference: str | None = None
    description: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state_history: list[StateTransitionRecord] = field(default_factory=list)
    journal_entry_ids: list[str] = field(default_factory=list)
    processed_webhook_ids: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def net_merchant_amount(self) -> int:
        return self.amount - self.platform_fee

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "idempotency_key": self.idempotency_key,
            "merchant_id": self.merchant_id,
            "customer_msisdn": self.customer_msisdn,
            "provider": self.provider,
            "amount": self.amount,
            "currency": self.currency.value,
            "platform_fee": self.platform_fee,
            "provider_fee": self.provider_fee,
            "net_merchant_amount": self.net_merchant_amount,
            "state": self.state.value,
            "external_reference": self.external_reference,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_history": [h.to_dict() for h in self.state_history],
            "journal_entry_ids": list(self.journal_entry_ids),
            "processed_webhook_ids": sorted(self.processed_webhook_ids),
            "metadata": dict(self.metadata),
        }


class PaymentNotFoundError(Exception):
    """Raised when a payment intent ID cannot be found."""


class PaymentOrchestrator:
    """Coordinates payment intent creation, state transitions, and double-entry ledger postings."""

    def __init__(
        self,
        ledger: LedgerEngine | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.ledger = ledger or LedgerEngine()
        self.idempotency = idempotency_store or IdempotencyStore()
        self._payments: dict[str, PaymentIntent] = {}

        # Ensure standard accounts exist for all supported currencies
        for curr in Currency:
            self.ledger.bootstrap_standard_chart_of_accounts(curr)

    @staticmethod
    def calculate_default_fees(amount: int, provider: str) -> tuple[int, int]:
        """Compute deterministic integer minor-unit platform fee (2.5%) and provider fee (1.2% or 1.5%)."""
        validate_minor_units(amount, allow_zero=False)
        platform_fee = max(1, (amount * 250) // 10_000)  # 2.50%
        if provider.lower().startswith("mtn"):
            provider_fee = max(1, (amount * 120) // 10_000)  # 1.20%
        else:
            provider_fee = max(1, (amount * 150) // 10_000)  # 1.50%
        return platform_fee, provider_fee

    def create_payment_intent(
        self,
        *,
        idempotency_key: str,
        merchant_id: str,
        customer_msisdn: str,
        amount: int,
        currency: Currency | str = Currency.UGX,
        provider: str = "mtn_momo",
        description: str = "Mobile Money Collection",
        platform_fee: int | None = None,
        provider_fee: int | None = None,
        payment_id: str | None = None,
        auto_submit_to_provider: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[PaymentIntent, bool]:
        """Create a payment intent idempotently and optionally transition to PENDING_PROVIDER."""
        validate_minor_units(amount, allow_zero=False)
        curr = Currency(currency)
        norm_provider = provider.lower().strip()
        if norm_provider not in ("mtn_momo", "airtel_money"):
            raise ValueError(f"Unsupported provider '{provider}'; must be 'mtn_momo' or 'airtel_money'")

        default_plat_fee, default_prov_fee = self.calculate_default_fees(amount, norm_provider)
        resolved_plat_fee = default_plat_fee if platform_fee is None else validate_minor_units(platform_fee, allow_zero=True)
        resolved_prov_fee = default_prov_fee if provider_fee is None else validate_minor_units(provider_fee, allow_zero=True)

        if resolved_plat_fee >= amount:
            raise ValueError("platform_fee must be strictly less than total payment amount")

        fingerprint_payload = {
            "merchant_id": merchant_id,
            "customer_msisdn": customer_msisdn,
            "amount": amount,
            "currency": curr.value,
            "provider": norm_provider,
            "description": description,
            "platform_fee": resolved_plat_fee,
            "provider_fee": resolved_prov_fee,
        }

        def _create() -> dict[str, Any]:
            with self._lock:
                pid = payment_id or f"pay_{uuid.uuid4().hex[:14]}"
                ext_ref = f"{norm_provider[:3].upper()}-{uuid.uuid4().hex[:10].upper()}"
                intent = PaymentIntent(
                    payment_id=pid,
                    idempotency_key=idempotency_key,
                    merchant_id=merchant_id,
                    customer_msisdn=customer_msisdn,
                    provider=norm_provider,
                    amount=amount,
                    currency=curr,
                    platform_fee=resolved_plat_fee,
                    provider_fee=resolved_prov_fee,
                    state=PaymentState.INITIATED,
                    external_reference=ext_ref,
                    description=description,
                    metadata=dict(metadata or {}),
                )
                self._payments[pid] = intent
                if auto_submit_to_provider:
                    self.transition_payment(
                        pid,
                        PaymentState.PENDING_PROVIDER,
                        event_type="provider_submission",
                        reason=f"Submitted collection request to {norm_provider}",
                    )
                return intent.to_dict()

        cached_dict, is_replayed = self.idempotency.execute_or_replay(
            idempotency_key, fingerprint_payload, _create
        )
        intent_obj = self.get_payment(cached_dict["payment_id"])
        return intent_obj, is_replayed

    def get_payment(self, payment_id: str) -> PaymentIntent:
        with self._lock:
            if payment_id not in self._payments:
                raise PaymentNotFoundError(f"Payment '{payment_id}' not found")
            return self._payments[payment_id]

    def list_payments(self) -> list[PaymentIntent]:
        with self._lock:
            return list(self._payments.values())

    def transition_payment(
        self,
        payment_id: str,
        target_state: PaymentState | str,
        *,
        event_type: str = "webhook_callback",
        webhook_id: str | None = None,
        allow_reconciliation_override: bool = False,
        actual_provider_fee: int | None = None,
        record_ledger_entry: bool = True,
        reason: str = "",
    ) -> StateTransitionRecord:
        """Apply a deterministic state transition and record balanced double-entry journal postings."""
        with self._lock:
            intent = self.get_payment(payment_id)
            target = PaymentState(target_state)

            # Deduplicate identical webhook IDs
            if webhook_id and webhook_id in intent.processed_webhook_ids:
                noop = StateTransitionRecord(
                    from_state=intent.state,
                    to_state=intent.state,
                    event_type=event_type,
                    applied=False,
                    idempotent_noop=True,
                    reason=f"Duplicate webhook_id '{webhook_id}' ignored idempotently",
                )
                intent.state_history.append(noop)
                return noop

            record = PaymentStateMachine.evaluate_transition(
                intent.state,
                target,
                event_type=event_type,
                allow_reconciliation_override=allow_reconciliation_override,
                reason=reason,
            )

            if webhook_id:
                intent.processed_webhook_ids.add(webhook_id)

            if not record.applied:
                intent.state_history.append(record)
                return record

            # If intermediate states were implicitly crossed (e.g., PENDING_PROVIDER -> SETTLED
            # when settlement arrived out-of-order before AUTHORIZED), record their ledger entries first!
            if record_ledger_entry:
                for intermediate in record.implicit_intermediate_states:
                    self._post_ledger_for_state(
                        intent,
                        intermediate,
                        actual_provider_fee=actual_provider_fee,
                    )
                self._post_ledger_for_state(
                    intent,
                    target,
                    actual_provider_fee=actual_provider_fee,
                )

            intent.state = target
            intent.updated_at = datetime.now(timezone.utc).isoformat()
            intent.state_history.append(record)
            return record

    def _post_ledger_for_state(
        self,
        intent: PaymentIntent,
        state: PaymentState,
        *,
        actual_provider_fee: int | None = None,
    ) -> None:
        """Record the corresponding double-entry accounting entry for a state transition."""
        c_code = intent.currency.value.lower()
        clearing_acc = f"asset:clearing:{intent.provider}:{c_code}"
        bank_acc = f"asset:settlement:bank:{c_code}"
        dispute_acc = f"asset:receivable:disputes:{c_code}"
        pending_liab = f"liability:merchant:pending:{c_code}"
        payable_liab = f"liability:merchant:payable:{c_code}"
        rev_fees = f"revenue:platform:fees:{c_code}"
        exp_fees = f"expense:provider:fees:{c_code}"

        if state == PaymentState.AUTHORIZED:
            # Customer funds authorized in mobile money clearing; recognize pending merchant liability
            entry = self.ledger.record_journal_entry(
                reference=intent.payment_id,
                description=f"Authorization clearing for {intent.payment_id} via {intent.provider}",
                currency=intent.currency,
                postings=[
                    {
                        "account_id": clearing_acc,
                        "direction": EntryDirection.DEBIT,
                        "amount": intent.amount,
                    },
                    {
                        "account_id": pending_liab,
                        "direction": EntryDirection.CREDIT,
                        "amount": intent.amount,
                    },
                ],
                metadata={
                    "payment_id": intent.payment_id,
                    "phase": "AUTHORIZED",
                    "provider": intent.provider,
                },
            )
            intent.journal_entry_ids.append(entry.entry_id)

        elif state == PaymentState.SETTLED:
            # Provider settles net cash to bank (amount - provider_fee), records gateway fee expense,
            # clears mobile money clearing asset, and reclassifies pending liability into merchant payable + platform revenue.
            prov_fee = (
                intent.provider_fee
                if actual_provider_fee is None
                else validate_minor_units(actual_provider_fee, allow_zero=True)
            )
            net_bank_settlement = intent.amount - prov_fee
            postings = [
                # 1. Clear provider clearing account and settle into bank + gateway fee expense
                {
                    "account_id": bank_acc,
                    "direction": EntryDirection.DEBIT,
                    "amount": net_bank_settlement,
                },
                {
                    "account_id": clearing_acc,
                    "direction": EntryDirection.CREDIT,
                    "amount": intent.amount,
                },
                # 2. Release pending liability into settled merchant payable + platform fee revenue
                {
                    "account_id": pending_liab,
                    "direction": EntryDirection.DEBIT,
                    "amount": intent.amount,
                },
                {
                    "account_id": payable_liab,
                    "direction": EntryDirection.CREDIT,
                    "amount": intent.net_merchant_amount,
                },
                {
                    "account_id": rev_fees,
                    "direction": EntryDirection.CREDIT,
                    "amount": intent.platform_fee,
                },
            ]
            if prov_fee > 0:
                postings.insert(
                    1,
                    {
                        "account_id": exp_fees,
                        "direction": EntryDirection.DEBIT,
                        "amount": prov_fee,
                    },
                )
            entry = self.ledger.record_journal_entry(
                reference=intent.payment_id,
                description=f"Settlement & fee recognition for {intent.payment_id}",
                currency=intent.currency,
                postings=postings,
                metadata={
                    "payment_id": intent.payment_id,
                    "phase": "SETTLED",
                    "provider": intent.provider,
                    "provider_fee_charged": prov_fee,
                    "platform_fee": intent.platform_fee,
                },
            )
            intent.journal_entry_ids.append(entry.entry_id)

        elif state == PaymentState.REVERSED:
            # Check whether payment was settled or only authorized prior to reversal
            prior_states = {h.to_state for h in intent.state_history if h.applied}
            if PaymentState.SETTLED in prior_states or intent.state == PaymentState.SETTLED or intent.state == PaymentState.DISPUTED:
                prov_fee = intent.provider_fee
                net_bank_settlement = intent.amount - prov_fee
                postings = [
                    {
                        "account_id": payable_liab,
                        "direction": EntryDirection.DEBIT,
                        "amount": intent.net_merchant_amount,
                    },
                    {
                        "account_id": rev_fees,
                        "direction": EntryDirection.DEBIT,
                        "amount": intent.platform_fee,
                    },
                    {
                        "account_id": bank_acc,
                        "direction": EntryDirection.CREDIT,
                        "amount": net_bank_settlement,
                    },
                ]
                if prov_fee > 0:
                    postings.append(
                        {
                            "account_id": exp_fees,
                            "direction": EntryDirection.CREDIT,
                            "amount": prov_fee,
                        }
                    )
                entry = self.ledger.record_journal_entry(
                    reference=intent.payment_id,
                    description=f"Full settlement reversal for {intent.payment_id}",
                    currency=intent.currency,
                    postings=postings,
                    metadata={"payment_id": intent.payment_id, "phase": "REVERSED"},
                )
                intent.journal_entry_ids.append(entry.entry_id)
            else:
                # Reversal from AUTHORIZED
                entry = self.ledger.record_journal_entry(
                    reference=intent.payment_id,
                    description=f"Authorization reversal for {intent.payment_id}",
                    currency=intent.currency,
                    postings=[
                        {
                            "account_id": pending_liab,
                            "direction": EntryDirection.DEBIT,
                            "amount": intent.amount,
                        },
                        {
                            "account_id": clearing_acc,
                            "direction": EntryDirection.CREDIT,
                            "amount": intent.amount,
                        },
                    ],
                    metadata={"payment_id": intent.payment_id, "phase": "REVERSED"},
                )
                intent.journal_entry_ids.append(entry.entry_id)

        elif state == PaymentState.DISPUTED:
            # Hold disputed amount in dispute receivable against merchant payable
            entry = self.ledger.record_journal_entry(
                reference=intent.payment_id,
                description=f"Customer dispute hold for {intent.payment_id}",
                currency=intent.currency,
                postings=[
                    {
                        "account_id": dispute_acc,
                        "direction": EntryDirection.DEBIT,
                        "amount": intent.net_merchant_amount,
                    },
                    {
                        "account_id": payable_liab,
                        "direction": EntryDirection.CREDIT,
                        "amount": intent.net_merchant_amount,
                    },
                ],
                metadata={"payment_id": intent.payment_id, "phase": "DISPUTED"},
            )
            intent.journal_entry_ids.append(entry.entry_id)
