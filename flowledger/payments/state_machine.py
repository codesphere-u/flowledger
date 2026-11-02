"""Deterministic payment state machine with out-of-order webhook safety."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class PaymentState(str, Enum):
    """Canonical lifecycle states for a mobile money or card payment intent."""

    INITIATED = "INITIATED"
    PENDING_PROVIDER = "PENDING_PROVIDER"
    AUTHORIZED = "AUTHORIZED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    DISPUTED = "DISPUTED"


class InvalidStateTransitionError(Exception):
    """Raised when an invalid or illegal payment state transition is attempted."""

    def __init__(
        self,
        current_state: PaymentState,
        target_state: PaymentState,
        reason: str,
    ) -> None:
        self.current_state = current_state
        self.target_state = target_state
        self.reason = reason
        super().__init__(
            f"Illegal state transition {current_state.value} -> {target_state.value}: {reason}"
        )


# Allowed forward transitions under normal orchestration
ALLOWED_TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.INITIATED: frozenset(
        {
            PaymentState.PENDING_PROVIDER,
            PaymentState.AUTHORIZED,
            PaymentState.FAILED,
        }
    ),
    PaymentState.PENDING_PROVIDER: frozenset(
        {
            PaymentState.AUTHORIZED,
            PaymentState.SETTLED,  # Supports fast/out-of-order settlement before auth webhook
            PaymentState.FAILED,
        }
    ),
    PaymentState.AUTHORIZED: frozenset(
        {
            PaymentState.SETTLED,
            PaymentState.FAILED,
            PaymentState.REVERSED,
        }
    ),
    PaymentState.SETTLED: frozenset(
        {
            PaymentState.REVERSED,
            PaymentState.DISPUTED,
        }
    ),
    PaymentState.DISPUTED: frozenset(
        {
            PaymentState.REVERSED,
            PaymentState.SETTLED,  # Dispute won / resolved in merchant favor
        }
    ),
    PaymentState.FAILED: frozenset(),  # Terminal unless manual reconciliation override
    PaymentState.REVERSED: frozenset(),  # Terminal
}

# Earlier states that may arrive as delayed/out-of-order webhooks after a payment has progressed
SAFE_OUT_OF_ORDER_PREDECESSORS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.AUTHORIZED: frozenset({PaymentState.PENDING_PROVIDER}),
    PaymentState.SETTLED: frozenset(
        {PaymentState.PENDING_PROVIDER, PaymentState.AUTHORIZED}
    ),
    PaymentState.REVERSED: frozenset(
        {PaymentState.PENDING_PROVIDER, PaymentState.AUTHORIZED}
    ),
    PaymentState.DISPUTED: frozenset(
        {PaymentState.PENDING_PROVIDER, PaymentState.AUTHORIZED}
    ),
}


@dataclass(frozen=True)
class StateTransitionRecord:
    """Audit record of a state transition or out-of-order webhook evaluation."""

    from_state: PaymentState
    to_state: PaymentState
    event_type: str
    applied: bool
    idempotent_noop: bool = False
    out_of_order_noop: bool = False
    reconciliation_override: bool = False
    implicit_intermediate_states: tuple[PaymentState, ...] = field(default_factory=tuple)
    reason: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "event_type": self.event_type,
            "applied": self.applied,
            "idempotent_noop": self.idempotent_noop,
            "out_of_order_noop": self.out_of_order_noop,
            "reconciliation_override": self.reconciliation_override,
            "implicit_intermediate_states": [
                s.value for s in self.implicit_intermediate_states
            ],
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class PaymentStateMachine:
    """Deterministic state machine governing payment lifecycle and out-of-order webhook events."""

    @staticmethod
    def evaluate_transition(
        current_state: PaymentState | str,
        target_state: PaymentState | str,
        *,
        event_type: str = "state_change",
        allow_reconciliation_override: bool = False,
        reason: str = "",
    ) -> StateTransitionRecord:
        """Validate and compute a state transition record."""
        curr = PaymentState(current_state)
        target = PaymentState(target_state)

        # 1. Idempotent same-state delivery
        if curr == target:
            return StateTransitionRecord(
                from_state=curr,
                to_state=curr,
                event_type=event_type,
                applied=False,
                idempotent_noop=True,
                reason=reason or f"Duplicate state delivery ({curr.value}) ignored idempotently",
            )

        # 2. Out-of-order delayed webhook from an earlier non-conflicting phase
        if target in SAFE_OUT_OF_ORDER_PREDECESSORS.get(curr, frozenset()):
            return StateTransitionRecord(
                from_state=curr,
                to_state=curr,
                event_type=event_type,
                applied=False,
                out_of_order_noop=True,
                reason=(
                    reason
                    or f"Out-of-order '{target.value}' webhook safely absorbed; "
                    f"payment is already in advanced state '{curr.value}'"
                ),
            )

        # 3. Explicit reconciliation / exception workflow override (e.g. FAILED -> SETTLED)
        if (
            curr == PaymentState.FAILED
            and target == PaymentState.SETTLED
            and allow_reconciliation_override
        ):
            return StateTransitionRecord(
                from_state=curr,
                to_state=target,
                event_type=event_type,
                applied=True,
                reconciliation_override=True,
                implicit_intermediate_states=(PaymentState.AUTHORIZED,),
                reason=(
                    reason
                    or "Manual reconciliation override applied for late provider settlement after timeout/failure"
                ),
            )

        # 4. Standard allowed transition check
        allowed = ALLOWED_TRANSITIONS.get(curr, frozenset())
        if target not in allowed:
            if curr == PaymentState.FAILED and target == PaymentState.SETTLED:
                raise InvalidStateTransitionError(
                    curr,
                    target,
                    "Cannot transition FAILED -> SETTLED without explicit reconciliation exception override",
                )
            raise InvalidStateTransitionError(
                curr,
                target,
                f"Transition from {curr.value} to {target.value} is prohibited by state machine invariants",
            )

        implicit_states: tuple[PaymentState, ...] = ()
        if curr == PaymentState.PENDING_PROVIDER and target == PaymentState.SETTLED:
            # Out-of-order settlement arrived before authorization webhook
            implicit_states = (PaymentState.AUTHORIZED,)

        return StateTransitionRecord(
            from_state=curr,
            to_state=target,
            event_type=event_type,
            applied=True,
            implicit_intermediate_states=implicit_states,
            reason=reason or f"Transitioned {curr.value} -> {target.value}",
        )
