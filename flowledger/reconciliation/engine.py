"""Automated 3-Way Reconciliation Engine across Payment Orders, Ledger Postings, and Provider Statements."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from flowledger.ledger.engine import LedgerEngine
from flowledger.ledger.models import Currency, EntryDirection, JournalEntry
from flowledger.payments.orchestrator import PaymentIntent
from flowledger.payments.state_machine import PaymentState
from flowledger.providers.sandbox import ProviderSettlementRecord


class ReconciliationStatus(str, Enum):
    """Canonical 3-way reconciliation classification outcomes."""

    MATCHED = "MATCHED"
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    MISSING_AT_PROVIDER = "MISSING_AT_PROVIDER"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    FEE_DISCREPANCY = "FEE_DISCREPANCY"
    STALE_UNSETTLED = "STALE_UNSETTLED"


@dataclass(frozen=True)
class AdjustmentProposal:
    """Actionable double-entry adjustment proposal generated for a reconciliation discrepancy."""

    payment_id: str
    discrepancy_type: ReconciliationStatus
    currency: Currency
    description: str
    proposed_postings: list[dict[str, Any]]
    requires_state_override: bool = False
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "discrepancy_type": self.discrepancy_type.value,
            "currency": self.currency.value,
            "description": self.description,
            "proposed_postings": list(self.proposed_postings),
            "requires_state_override": self.requires_state_override,
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True)
class ReconciliationItem:
    """Single 3-way reconciliation result comparing Order, Ledger, and Provider Statement."""

    payment_id: str
    provider: str
    currency: Currency
    status: ReconciliationStatus
    order_state: str | None
    order_amount: int | None
    ledger_settled_amount: int | None
    ledger_provider_fee: int | None
    provider_gross_amount: int | None
    provider_fee_amount: int | None
    discrepancy_delta: int
    explanation: str
    adjustment_proposal: AdjustmentProposal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "provider": self.provider,
            "currency": self.currency.value,
            "status": self.status.value,
            "order_state": self.order_state,
            "order_amount": self.order_amount,
            "ledger_settled_amount": self.ledger_settled_amount,
            "ledger_provider_fee": self.ledger_provider_fee,
            "provider_gross_amount": self.provider_gross_amount,
            "provider_fee_amount": self.provider_fee_amount,
            "discrepancy_delta": self.discrepancy_delta,
            "explanation": self.explanation,
            "adjustment_proposal": (
                self.adjustment_proposal.to_dict() if self.adjustment_proposal else None
            ),
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """Complete 3-way reconciliation batch report with counts, items, and proposals."""

    reconciled_at: str
    total_evaluated: int
    counts_by_status: dict[str, int]
    items: list[ReconciliationItem]
    adjustment_proposals: list[AdjustmentProposal]
    match_rate_pct: float = field(default=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled_at": self.reconciled_at,
            "total_evaluated": self.total_evaluated,
            "counts_by_status": dict(self.counts_by_status),
            "match_rate_pct": self.match_rate_pct,
            "items": [i.to_dict() for i in self.items],
            "adjustment_proposals": [p.to_dict() for p in self.adjustment_proposals],
        }


class ThreeWayReconciliationEngine:
    """Reconciles (1) Internal Payment Orders, (2) Double-Entry Ledger Postings, and (3) Provider Statements."""

    @staticmethod
    def export_settlement_csv(records: Sequence[ProviderSettlementRecord]) -> str:
        """Serialize provider settlement statement records to canonical CSV."""
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "statement_id",
                "provider",
                "payment_id",
                "external_reference",
                "gross_amount",
                "provider_fee",
                "net_settlement_amount",
                "currency",
                "settlement_status",
                "settled_at",
            ],
        )
        writer.writeheader()
        for r in records:
            writer.writerow(
                {
                    "statement_id": r.statement_id,
                    "provider": r.provider,
                    "payment_id": r.payment_id,
                    "external_reference": r.external_reference,
                    "gross_amount": r.gross_amount,
                    "provider_fee": r.provider_fee,
                    "net_settlement_amount": r.net_settlement_amount,
                    "currency": r.currency.value,
                    "settlement_status": r.settlement_status,
                    "settled_at": r.settled_at,
                }
            )
        return buf.getvalue()

    @staticmethod
    def parse_settlement_csv(csv_text: str) -> list[ProviderSettlementRecord]:
        """Parse provider settlement CSV rows into `ProviderSettlementRecord` instances."""
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        records: list[ProviderSettlementRecord] = []
        for row in reader:
            records.append(
                ProviderSettlementRecord(
                    statement_id=row["statement_id"],
                    provider=row["provider"],
                    payment_id=row["payment_id"],
                    external_reference=row.get("external_reference", ""),
                    gross_amount=int(row["gross_amount"]),
                    provider_fee=int(row["provider_fee"]),
                    net_settlement_amount=int(row["net_settlement_amount"]),
                    currency=Currency(row["currency"]),
                    settlement_status=row.get("settlement_status", "SETTLED"),
                    settled_at=row.get(
                        "settled_at", datetime.now(timezone.utc).isoformat()
                    ),
                )
            )
        return records

    def reconcile(
        self,
        *,
        payments: Sequence[PaymentIntent],
        ledger: LedgerEngine,
        provider_statements: Sequence[ProviderSettlementRecord | dict[str, Any]],
    ) -> ReconciliationReport:
        """Perform 3-way reconciliation across internal payment intents, ledger entries, and provider statements."""
        payment_map: dict[str, PaymentIntent] = {p.payment_id: p for p in payments}

        normalized_statements: dict[str, ProviderSettlementRecord] = {}
        for raw in provider_statements:
            if isinstance(raw, ProviderSettlementRecord):
                stmt = raw
            else:
                stmt = ProviderSettlementRecord(
                    statement_id=str(raw.get("statement_id", "stmt_ext")),
                    provider=str(raw["provider"]),
                    payment_id=str(raw["payment_id"]),
                    external_reference=str(raw.get("external_reference", "")),
                    gross_amount=int(raw["gross_amount"]),
                    provider_fee=int(raw["provider_fee"]),
                    net_settlement_amount=int(
                        raw.get(
                            "net_settlement_amount",
                            int(raw["gross_amount"]) - int(raw["provider_fee"]),
                        )
                    ),
                    currency=Currency(raw.get("currency", "UGX")),
                    settlement_status=str(raw.get("settlement_status", "SETTLED")),
                    settled_at=str(
                        raw.get("settled_at", datetime.now(timezone.utc).isoformat())
                    ),
                )
            normalized_statements[stmt.payment_id] = stmt

        # Index ledger entries by reference (payment_id)
        entries_by_payment: dict[str, list[JournalEntry]] = {}
        for entry in ledger.list_entries():
            entries_by_payment.setdefault(entry.reference, []).append(entry)

        all_payment_ids = sorted(
            set(payment_map.keys())
            | set(normalized_statements.keys())
            | set(entries_by_payment.keys())
        )

        items: list[ReconciliationItem] = []
        proposals: list[AdjustmentProposal] = []
        counts: dict[str, int] = {s.value: 0 for s in ReconciliationStatus}

        for pid in all_payment_ids:
            intent = payment_map.get(pid)
            stmt = normalized_statements.get(pid)
            p_entries = entries_by_payment.get(pid, [])

            item = self._evaluate_single_payment(
                payment_id=pid,
                intent=intent,
                stmt=stmt,
                entries=p_entries,
            )
            items.append(item)
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
            if item.adjustment_proposal is not None:
                proposals.append(item.adjustment_proposal)

        total = len(items)
        matched_count = counts.get(ReconciliationStatus.MATCHED.value, 0)
        match_rate = round((matched_count * 100.0) / total, 2) if total > 0 else 100.0

        return ReconciliationReport(
            reconciled_at=datetime.now(timezone.utc).isoformat(),
            total_evaluated=total,
            counts_by_status=counts,
            items=items,
            adjustment_proposals=proposals,
            match_rate_pct=match_rate,
        )

    def _evaluate_single_payment(
        self,
        *,
        payment_id: str,
        intent: PaymentIntent | None,
        stmt: ProviderSettlementRecord | None,
        entries: list[JournalEntry],
    ) -> ReconciliationItem:
        provider = (
            intent.provider
            if intent is not None
            else (stmt.provider if stmt is not None else "unknown")
        )
        currency = (
            intent.currency
            if intent is not None
            else (stmt.currency if stmt is not None else Currency.UGX)
        )
        c_code = currency.value.lower()

        # Extract ledger settlement metrics from SETTLED / REVERSED journal entries
        settled_entries = [
            e for e in entries if e.metadata.get("phase") == "SETTLED"
        ]
        reversed_entries = [
            e for e in entries if e.metadata.get("phase") == "REVERSED"
        ]
        auth_entries = [
            e for e in entries if e.metadata.get("phase") == "AUTHORIZED"
        ]

        ledger_settled_gross: int | None = None
        ledger_prov_fee: int | None = None
        if settled_entries:
            latest_settled = settled_entries[-1]
            clearing_credits = [
                p.amount
                for p in latest_settled.postings
                if p.direction == EntryDirection.CREDIT
                and p.account_id.startswith("asset:clearing:")
            ]
            fee_debits = [
                p.amount
                for p in latest_settled.postings
                if p.direction == EntryDirection.DEBIT
                and p.account_id.startswith("expense:provider:fees:")
            ]
            ledger_settled_gross = clearing_credits[0] if clearing_credits else intent.amount if intent else None
            ledger_prov_fee = fee_debits[0] if fee_debits else 0

        # Case 1: Provider statement settled, but missing in ledger (or order marked FAILED/INITIATED/PENDING without ledger settlement)
        if stmt is not None and stmt.settlement_status == "SETTLED" and not settled_entries:
            gross = stmt.gross_amount
            prov_fee = stmt.provider_fee
            plat_fee = intent.platform_fee if intent else max(1, (gross * 250) // 10_000)
            net_merch = gross - plat_fee
            net_bank = gross - prov_fee

            proposal = AdjustmentProposal(
                payment_id=payment_id,
                discrepancy_type=ReconciliationStatus.MISSING_IN_LEDGER,
                currency=currency,
                description=(
                    f"Backfill missing settlement journal entry for {payment_id} "
                    f"confirmed in {provider} settlement statement ({stmt.statement_id})"
                ),
                requires_state_override=(
                    intent is not None and intent.state == PaymentState.FAILED
                ),
                recommended_action=(
                    "Execute manual reconciliation override (FAILED -> SETTLED) and commit backfill settlement postings"
                ),
                proposed_postings=[
                    {
                        "account_id": f"asset:settlement:bank:{c_code}",
                        "direction": EntryDirection.DEBIT.value,
                        "amount": net_bank,
                        "currency": currency.value,
                    },
                    {
                        "account_id": f"expense:provider:fees:{c_code}",
                        "direction": EntryDirection.DEBIT.value,
                        "amount": prov_fee,
                        "currency": currency.value,
                    },
                    {
                        "account_id": f"liability:merchant:payable:{c_code}",
                        "direction": EntryDirection.CREDIT.value,
                        "amount": net_merch,
                        "currency": currency.value,
                    },
                    {
                        "account_id": f"revenue:platform:fees:{c_code}",
                        "direction": EntryDirection.CREDIT.value,
                        "amount": plat_fee,
                        "currency": currency.value,
                    },
                ],
            )
            return ReconciliationItem(
                payment_id=payment_id,
                provider=provider,
                currency=currency,
                status=ReconciliationStatus.MISSING_IN_LEDGER,
                order_state=intent.state.value if intent else None,
                order_amount=intent.amount if intent else None,
                ledger_settled_amount=None,
                ledger_provider_fee=None,
                provider_gross_amount=stmt.gross_amount,
                provider_fee_amount=stmt.provider_fee,
                discrepancy_delta=stmt.gross_amount,
                explanation=(
                    f"Provider statement settled {stmt.gross_amount} {currency.value}, "
                    f"but internal ledger has no SETTLED journal entry (order state: "
                    f"{intent.state.value if intent else 'MISSING'})"
                ),
                adjustment_proposal=proposal,
            )

        # Case 2: Internal order/ledger is SETTLED, but missing from external provider statement
        if stmt is None and (
            (intent is not None and intent.state == PaymentState.SETTLED)
            or bool(settled_entries)
        ):
            expected_amt = ledger_settled_gross or (intent.amount if intent else 0)
            proposal = AdjustmentProposal(
                payment_id=payment_id,
                discrepancy_type=ReconciliationStatus.MISSING_AT_PROVIDER,
                currency=currency,
                description=(
                    f"Reclassify unconfirmed bank settlement for {payment_id} back to clearing suspense"
                ),
                recommended_action="Raise settlement claim with provider treasury or reverse premature bank settlement entry",
                proposed_postings=[
                    {
                        "account_id": f"asset:clearing:{provider}:{c_code}",
                        "direction": EntryDirection.DEBIT.value,
                        "amount": expected_amt,
                        "currency": currency.value,
                    },
                    {
                        "account_id": f"asset:settlement:bank:{c_code}",
                        "direction": EntryDirection.CREDIT.value,
                        "amount": expected_amt,
                        "currency": currency.value,
                    },
                ],
            )
            return ReconciliationItem(
                payment_id=payment_id,
                provider=provider,
                currency=currency,
                status=ReconciliationStatus.MISSING_AT_PROVIDER,
                order_state=intent.state.value if intent else "SETTLED",
                order_amount=intent.amount if intent else expected_amt,
                ledger_settled_amount=ledger_settled_gross,
                ledger_provider_fee=ledger_prov_fee,
                provider_gross_amount=None,
                provider_fee_amount=None,
                discrepancy_delta=-expected_amt,
                explanation=(
                    f"Internal ledger recorded SETTLED for {expected_amt} {currency.value}, "
                    f"but record is absent from {provider} settlement batch"
                ),
                adjustment_proposal=proposal,
            )

        # Case 3: Stale unsettled payment (stuck in AUTHORIZED or PENDING_PROVIDER with no provider settlement)
        if stmt is None and intent is not None and intent.state in (
            PaymentState.AUTHORIZED,
            PaymentState.PENDING_PROVIDER,
            PaymentState.INITIATED,
        ):
            amt = intent.amount
            proposal = None
            if auth_entries:
                proposal = AdjustmentProposal(
                    payment_id=payment_id,
                    discrepancy_type=ReconciliationStatus.STALE_UNSETTLED,
                    currency=currency,
                    description=f"Release stale authorization clearing hold for {payment_id}",
                    recommended_action="Query provider transaction status API; if expired, reverse clearing authorization",
                    proposed_postings=[
                        {
                            "account_id": f"liability:merchant:pending:{c_code}",
                            "direction": EntryDirection.DEBIT.value,
                            "amount": amt,
                            "currency": currency.value,
                        },
                        {
                            "account_id": f"asset:clearing:{provider}:{c_code}",
                            "direction": EntryDirection.CREDIT.value,
                            "amount": amt,
                            "currency": currency.value,
                        },
                    ],
                )
            return ReconciliationItem(
                payment_id=payment_id,
                provider=provider,
                currency=currency,
                status=ReconciliationStatus.STALE_UNSETTLED,
                order_state=intent.state.value,
                order_amount=intent.amount,
                ledger_settled_amount=None,
                ledger_provider_fee=None,
                provider_gross_amount=None,
                provider_fee_amount=None,
                discrepancy_delta=amt,
                explanation=(
                    f"Payment remains in non-terminal state '{intent.state.value}' "
                    f"for {amt} {currency.value} without provider settlement confirmation"
                ),
                adjustment_proposal=proposal,
            )

        # Case 4: Both Ledger and Provider Statement exist — compare Gross Amount & Provider Fee
        if stmt is not None and settled_entries:
            expected_gross = ledger_settled_gross or (intent.amount if intent else stmt.gross_amount)
            expected_fee = ledger_prov_fee if ledger_prov_fee is not None else (intent.provider_fee if intent else stmt.provider_fee)

            # Check for REVERSED alignment first
            if stmt.settlement_status == "REVERSED" and reversed_entries:
                return ReconciliationItem(
                    payment_id=payment_id,
                    provider=provider,
                    currency=currency,
                    status=ReconciliationStatus.MATCHED,
                    order_state=intent.state.value if intent else "REVERSED",
                    order_amount=expected_gross,
                    ledger_settled_amount=expected_gross,
                    ledger_provider_fee=0,
                    provider_gross_amount=stmt.gross_amount,
                    provider_fee_amount=stmt.provider_fee,
                    discrepancy_delta=0,
                    explanation="3-way match verified (REVERSED across Order, Ledger, and Provider Statement)",
                )

            # 4a. Gross amount mismatch
            if stmt.gross_amount != expected_gross:
                delta = expected_gross - stmt.gross_amount
                abs_delta = abs(delta)
                proposal = AdjustmentProposal(
                    payment_id=payment_id,
                    discrepancy_type=ReconciliationStatus.AMOUNT_MISMATCH,
                    currency=currency,
                    description=(
                        f"Record dispute receivable for gross settlement shortfall of "
                        f"{abs_delta} {currency.value} on {payment_id}"
                    ),
                    recommended_action="Post settlement shortfall to dispute receivable and escalate to provider",
                    proposed_postings=[
                        {
                            "account_id": f"asset:receivable:disputes:{c_code}",
                            "direction": (
                                EntryDirection.DEBIT.value
                                if delta > 0
                                else EntryDirection.CREDIT.value
                            ),
                            "amount": abs_delta,
                            "currency": currency.value,
                        },
                        {
                            "account_id": f"asset:settlement:bank:{c_code}",
                            "direction": (
                                EntryDirection.CREDIT.value
                                if delta > 0
                                else EntryDirection.DEBIT.value
                            ),
                            "amount": abs_delta,
                            "currency": currency.value,
                        },
                    ],
                )
                return ReconciliationItem(
                    payment_id=payment_id,
                    provider=provider,
                    currency=currency,
                    status=ReconciliationStatus.AMOUNT_MISMATCH,
                    order_state=intent.state.value if intent else "SETTLED",
                    order_amount=intent.amount if intent else expected_gross,
                    ledger_settled_amount=expected_gross,
                    ledger_provider_fee=expected_fee,
                    provider_gross_amount=stmt.gross_amount,
                    provider_fee_amount=stmt.provider_fee,
                    discrepancy_delta=delta,
                    explanation=(
                        f"Gross amount mismatch: Ledger recorded {expected_gross} {currency.value} "
                        f"vs Provider statement {stmt.gross_amount} {currency.value} (delta={delta})"
                    ),
                    adjustment_proposal=proposal,
                )

            # 4b. Provider fee discrepancy
            if stmt.provider_fee != expected_fee:
                fee_delta = stmt.provider_fee - expected_fee
                abs_fee_delta = abs(fee_delta)
                proposal = AdjustmentProposal(
                    payment_id=payment_id,
                    discrepancy_type=ReconciliationStatus.FEE_DISCREPANCY,
                    currency=currency,
                    description=(
                        f"True-up provider gateway fee variance of {fee_delta} {currency.value} "
                        f"for {payment_id}"
                    ),
                    recommended_action="Commit fee true-up journal entry between provider fee expense and settlement bank account",
                    proposed_postings=[
                        {
                            "account_id": f"expense:provider:fees:{c_code}",
                            "direction": (
                                EntryDirection.DEBIT.value
                                if fee_delta > 0
                                else EntryDirection.CREDIT.value
                            ),
                            "amount": abs_fee_delta,
                            "currency": currency.value,
                        },
                        {
                            "account_id": f"asset:settlement:bank:{c_code}",
                            "direction": (
                                EntryDirection.CREDIT.value
                                if fee_delta > 0
                                else EntryDirection.DEBIT.value
                            ),
                            "amount": abs_fee_delta,
                            "currency": currency.value,
                        },
                    ],
                )
                return ReconciliationItem(
                    payment_id=payment_id,
                    provider=provider,
                    currency=currency,
                    status=ReconciliationStatus.FEE_DISCREPANCY,
                    order_state=intent.state.value if intent else "SETTLED",
                    order_amount=intent.amount if intent else expected_gross,
                    ledger_settled_amount=expected_gross,
                    ledger_provider_fee=expected_fee,
                    provider_gross_amount=stmt.gross_amount,
                    provider_fee_amount=stmt.provider_fee,
                    discrepancy_delta=fee_delta,
                    explanation=(
                        f"Provider fee discrepancy: Ledger expected fee {expected_fee} {currency.value}, "
                        f"Provider deducted {stmt.provider_fee} {currency.value} (variance={fee_delta})"
                    ),
                    adjustment_proposal=proposal,
                )

            # 4c. Exact 3-way match!
            return ReconciliationItem(
                payment_id=payment_id,
                provider=provider,
                currency=currency,
                status=ReconciliationStatus.MATCHED,
                order_state=intent.state.value if intent else "SETTLED",
                order_amount=intent.amount if intent else expected_gross,
                ledger_settled_amount=expected_gross,
                ledger_provider_fee=expected_fee,
                provider_gross_amount=stmt.gross_amount,
                provider_fee_amount=stmt.provider_fee,
                discrepancy_delta=0,
                explanation=(
                    f"Exact 3-way match across Payment Intent, Double-Entry Ledger, and {provider} Statement"
                ),
            )

        # Fallback for terminal failed orders with no ledger or provider statement
        return ReconciliationItem(
            payment_id=payment_id,
            provider=provider,
            currency=currency,
            status=ReconciliationStatus.MATCHED,
            order_state=intent.state.value if intent else None,
            order_amount=intent.amount if intent else None,
            ledger_settled_amount=0,
            ledger_provider_fee=0,
            provider_gross_amount=0,
            provider_fee_amount=0,
            discrepancy_delta=0,
            explanation="Cleanly terminated without settlement movement",
        )
