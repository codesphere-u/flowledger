"""Simulated MTN MoMo and Airtel Money sandbox adapters and failure injection simulator."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from flowledger.ledger.models import Currency, validate_minor_units
from flowledger.payments.orchestrator import PaymentIntent, PaymentOrchestrator
from flowledger.payments.state_machine import PaymentState
from flowledger.providers.webhooks import SignedWebhookEnvelope, WebhookVerifier

DEFAULT_MTN_SECRET = "whsec_mtn_sandbox_9f8e7d6c5b4a3210fedcba9876543210"
DEFAULT_AIRTEL_SECRET = "whsec_airtel_sandbox_1a2b3c4d5e6f7890abcdef1234567890"


class FailureScenario(str, Enum):
    """Failure and anomaly injection scenarios for resilience and reconciliation testing."""

    NORMAL = "NORMAL"
    DUPLICATE_WEBHOOK = "DUPLICATE_WEBHOOK"
    OUT_OF_ORDER_WEBHOOK = "OUT_OF_ORDER_WEBHOOK"
    PROVIDER_TIMEOUT_RETRY = "PROVIDER_TIMEOUT_RETRY"
    FEE_MISMATCH = "FEE_MISMATCH"
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    MISSING_AT_PROVIDER = "MISSING_AT_PROVIDER"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CUSTOMER_REVERSAL = "CUSTOMER_REVERSAL"


@dataclass(frozen=True)
class ProviderSettlementRecord:
    """External mobile money provider settlement statement row (CSV/JSON record)."""

    statement_id: str
    provider: str
    payment_id: str
    external_reference: str
    gross_amount: int
    provider_fee: int
    net_settlement_amount: int
    currency: Currency
    settlement_status: str
    settled_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "provider": self.provider,
            "payment_id": self.payment_id,
            "external_reference": self.external_reference,
            "gross_amount": self.gross_amount,
            "provider_fee": self.provider_fee,
            "net_settlement_amount": self.net_settlement_amount,
            "currency": self.currency.value,
            "settlement_status": self.settlement_status,
            "settled_at": self.settled_at,
            "metadata": dict(self.metadata),
        }


def calculate_retry_backoff_ms(
    attempt: int,
    *,
    base_delay_ms: int = 250,
    max_delay_ms: int = 8000,
    deterministic_key: str | None = None,
) -> int:
    """Compute exponential backoff delay in integer milliseconds with deterministic jitter."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    exponential = min(max_delay_ms, base_delay_ms * (2 ** (attempt - 1)))
    if deterministic_key:
        digest = hashlib.sha256(f"{deterministic_key}:{attempt}".encode("utf-8")).digest()
        jitter = digest[0] % max(1, (exponential // 5))
    else:
        jitter = 0
    return min(max_delay_ms, exponential + jitter)


class BaseMobileMoneySandboxAdapter:
    """Base sandbox adapter for East & West African Mobile Money providers."""

    provider_id: str = "base"
    display_name: str = "Base Sandbox"
    fee_bps: int = 120  # 1.20%
    settlement_delay_seconds: int = 15

    def __init__(self, webhook_secret: str) -> None:
        self.verifier = WebhookVerifier(webhook_secret)

    def compute_provider_fee(self, amount: int) -> int:
        validate_minor_units(amount, allow_zero=False)
        return max(1, (amount * self.fee_bps) // 10_000)

    def build_signed_webhook(
        self,
        *,
        payment: PaymentIntent,
        target_state: PaymentState,
        event_id: str | None = None,
        reported_amount: int | None = None,
        reported_fee: int | None = None,
        timestamp: int | None = None,
    ) -> SignedWebhookEnvelope:
        resolved_event_id = event_id or f"evt_{uuid.uuid4().hex[:14]}"
        gross = payment.amount if reported_amount is None else reported_amount
        fee = payment.provider_fee if reported_fee is None else reported_fee
        payload = {
            "event_id": resolved_event_id,
            "provider": self.provider_id,
            "payment_id": payment.payment_id,
            "external_reference": payment.external_reference,
            "status": target_state.value,
            "gross_amount": gross,
            "provider_fee": fee,
            "net_amount": gross - fee,
            "currency": payment.currency.value,
            "customer_msisdn": payment.customer_msisdn,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
        return self.verifier.sign_payload(
            payload, provider=self.provider_id, timestamp=timestamp
        )


class MtnMomoSandboxAdapter(BaseMobileMoneySandboxAdapter):
    """Simulated MTN Mobile Money (MoMo) collection and settlement adapter."""

    provider_id = "mtn_momo"
    display_name = "MTN Mobile Money (MoMo)"
    fee_bps = 120  # 1.20%
    settlement_delay_seconds = 12

    def __init__(self, webhook_secret: str = DEFAULT_MTN_SECRET) -> None:
        super().__init__(webhook_secret)


class AirtelMoneySandboxAdapter(BaseMobileMoneySandboxAdapter):
    """Simulated Airtel Money collection and settlement adapter."""

    provider_id = "airtel_money"
    display_name = "Airtel Money"
    fee_bps = 150  # 1.50%
    settlement_delay_seconds = 18

    def __init__(self, webhook_secret: str = DEFAULT_AIRTEL_SECRET) -> None:
        super().__init__(webhook_secret)


class FailureInjectionSimulator:
    """Executes end-to-end payment scenarios with configurable failure/anomaly injection."""

    def __init__(
        self,
        orchestrator: PaymentOrchestrator,
        mtn_adapter: MtnMomoSandboxAdapter | None = None,
        airtel_adapter: AirtelMoneySandboxAdapter | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.adapters: dict[str, BaseMobileMoneySandboxAdapter] = {
            "mtn_momo": mtn_adapter or MtnMomoSandboxAdapter(),
            "airtel_money": airtel_adapter or AirtelMoneySandboxAdapter(),
        }
        self.settlement_statements: list[ProviderSettlementRecord] = []
        self.simulation_logs: list[dict[str, Any]] = []

    def get_adapter(self, provider: str) -> BaseMobileMoneySandboxAdapter:
        norm = provider.lower().strip()
        if norm not in self.adapters:
            raise ValueError(f"Unknown sandbox provider '{provider}'")
        return self.adapters[norm]

    def process_signed_webhook(
        self,
        envelope: SignedWebhookEnvelope,
        *,
        now_timestamp: int | None = None,
        allow_reconciliation_override: bool = False,
    ) -> dict[str, Any]:
        """Verify HMAC signature & timestamp of a webhook envelope and apply it to the orchestrator."""
        adapter = self.get_adapter(envelope.provider)
        verified_payload = adapter.verifier.verify(
            envelope.raw_body,
            envelope.headers,
            now_timestamp=now_timestamp,
        )
        payment_id = str(verified_payload["payment_id"])
        target_state = PaymentState(verified_payload["status"])
        event_id = str(verified_payload["event_id"])
        fee = int(verified_payload["provider_fee"])

        transition = self.orchestrator.transition_payment(
            payment_id,
            target_state,
            event_type=f"webhook:{envelope.provider}",
            webhook_id=event_id,
            allow_reconciliation_override=allow_reconciliation_override,
            actual_provider_fee=fee,
            reason=f"Verified HMAC webhook {event_id} ({target_state.value})",
        )
        return {
            "verified": True,
            "event_id": event_id,
            "payment_id": payment_id,
            "transition": transition.to_dict(),
        }

    def run_scenario(
        self,
        *,
        payment: PaymentIntent,
        scenario: FailureScenario | str = FailureScenario.NORMAL,
    ) -> dict[str, Any]:
        """Execute a payment lifecycle under the specified failure injection scenario."""
        scen = FailureScenario(scenario)
        adapter = self.get_adapter(payment.provider)
        events: list[dict[str, Any]] = []
        retry_schedule_ms: list[int] = []

        if scen == FailureScenario.NORMAL:
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            events.append(self.process_signed_webhook(w_auth))
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_settle))
            self._append_settlement_row(payment, gross=payment.amount, fee=payment.provider_fee)

        elif scen == FailureScenario.DUPLICATE_WEBHOOK:
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            events.append(self.process_signed_webhook(w_auth))
            # Deliver the exact same SETTLED webhook 3 times and a second distinct event_id with SETTLED
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_settle))
            events.append(self.process_signed_webhook(w_settle))
            w_settle_dup2 = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_settle_dup2))
            self._append_settlement_row(payment, gross=payment.amount, fee=payment.provider_fee)

        elif scen == FailureScenario.OUT_OF_ORDER_WEBHOOK:
            # Provider delivers SETTLED callback BEFORE the AUTHORIZED callback arrives!
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            w_auth_delayed = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            events.append(self.process_signed_webhook(w_settle))
            events.append(self.process_signed_webhook(w_auth_delayed))
            self._append_settlement_row(payment, gross=payment.amount, fee=payment.provider_fee)

        elif scen == FailureScenario.PROVIDER_TIMEOUT_RETRY:
            # Compute 3 exponential backoff retry intervals before provider callback succeeds
            for attempt in (1, 2, 3):
                retry_schedule_ms.append(
                    calculate_retry_backoff_ms(
                        attempt, deterministic_key=payment.payment_id
                    )
                )
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_auth))
            events.append(self.process_signed_webhook(w_settle))
            self._append_settlement_row(payment, gross=payment.amount, fee=payment.provider_fee)

        elif scen == FailureScenario.FEE_MISMATCH:
            # Internal ledger settles with contracted provider_fee, but provider statement deducts extra fee
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_auth))
            events.append(self.process_signed_webhook(w_settle))
            surcharge_fee = payment.provider_fee + max(100, payment.provider_fee // 4)
            self._append_settlement_row(
                payment,
                gross=payment.amount,
                fee=surcharge_fee,
                note="Uncontracted provider gateway surcharge in statement",
            )

        elif scen == FailureScenario.MISSING_IN_LEDGER:
            # Payment timed out / failed locally, or webhook was dropped, yet provider actually settled it!
            w_fail = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.FAILED
            )
            events.append(self.process_signed_webhook(w_fail))
            self._append_settlement_row(
                payment,
                gross=payment.amount,
                fee=payment.provider_fee,
                note="Settled at provider despite local FAILED state (dropped settlement webhook)",
            )

        elif scen == FailureScenario.MISSING_AT_PROVIDER:
            # Payment was authorized locally and remains stuck in AUTHORIZED (or settled locally) without provider settlement row
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            events.append(self.process_signed_webhook(w_auth))
            # Intentionally omit from self.settlement_statements

        elif scen == FailureScenario.AMOUNT_MISMATCH:
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            events.append(self.process_signed_webhook(w_auth))
            events.append(self.process_signed_webhook(w_settle))
            under_reported_gross = max(100, payment.amount - 1500)
            self._append_settlement_row(
                payment,
                gross=under_reported_gross,
                fee=payment.provider_fee,
                note="Gross amount discrepancy on external provider settlement file",
            )

        elif scen == FailureScenario.CUSTOMER_REVERSAL:
            w_auth = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.AUTHORIZED
            )
            w_settle = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.SETTLED
            )
            w_rev = adapter.build_signed_webhook(
                payment=payment, target_state=PaymentState.REVERSED
            )
            events.append(self.process_signed_webhook(w_auth))
            events.append(self.process_signed_webhook(w_settle))
            events.append(self.process_signed_webhook(w_rev))
            self._append_settlement_row(
                payment,
                gross=payment.amount,
                fee=0,
                status="REVERSED",
                note="Customer mobile money reversal / chargeback processed",
            )

        log_entry = {
            "payment_id": payment.payment_id,
            "provider": payment.provider,
            "scenario": scen.value,
            "final_state": payment.state.value,
            "retry_schedule_ms": retry_schedule_ms,
            "events": events,
        }
        self.simulation_logs.append(log_entry)
        return log_entry

    def _append_settlement_row(
        self,
        payment: PaymentIntent,
        *,
        gross: int,
        fee: int,
        status: str = "SETTLED",
        note: str = "",
    ) -> ProviderSettlementRecord:
        record = ProviderSettlementRecord(
            statement_id=f"stmt_{uuid.uuid4().hex[:12]}",
            provider=payment.provider,
            payment_id=payment.payment_id,
            external_reference=payment.external_reference or "",
            gross_amount=gross,
            provider_fee=fee,
            net_settlement_amount=gross - fee,
            currency=payment.currency,
            settlement_status=status,
            settled_at=datetime.now(timezone.utc).isoformat(),
            metadata={"note": note} if note else {},
        )
        self.settlement_statements.append(record)
        return record
