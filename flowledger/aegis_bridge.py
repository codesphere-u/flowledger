"""Nexus Aegis Cryptographic Attestation Bridge for FlowLedger.

Exports FlowLedger double-entry invariants, zero-sum trial balance verification,
and 3-way reconciliation audit reports into canonical Proof-Carrying Agent Execution
(PCAE) attestation payloads with RFC 6962 domain-separated SHA-256 leaf digests (0x00 prefix).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from flowledger.ledger.models import InvariantReport
from flowledger.reconciliation.engine import ReconciliationReport, ReconciliationStatus


def canonical_json_bytes(obj: Any) -> bytes:
    """Return deterministic UTF-8 bytes for an object using sorted keys and minimal whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def hash_leaf_bytes(data: bytes) -> str:
    """Compute RFC 6962 Certificate Transparency leaf hash: SHA-256(0x00 || payload)."""
    return hashlib.sha256(b"\x00" + data).hexdigest()


def create_aegis_financial_payload(
    invariant_report: InvariantReport | dict[str, Any] | None = None,
    recon_report: ReconciliationReport | dict[str, Any] | None = None,
    task_id: str = "flowledger_financial_audit",
) -> dict[str, Any]:
    """
    Transform FlowLedger mathematical invariant checks and 3-way reconciliation results
    into canonical Nexus Aegis attestation format.
    """
    violations: list[str] = []
    journal_count = 0
    account_count = 0
    head_hash = "0" * 64
    trial_balances: dict[str, Any] = {}
    is_ledger_valid = True

    if isinstance(invariant_report, InvariantReport):
        journal_count = invariant_report.journal_count
        account_count = invariant_report.account_count
        head_hash = invariant_report.head_hash
        trial_balances = invariant_report.trial_balance_by_currency
        is_ledger_valid = invariant_report.is_valid
        violations.extend(invariant_report.errors)
    elif isinstance(invariant_report, dict):
        journal_count = int(invariant_report.get("journal_count", 0))
        account_count = int(invariant_report.get("account_count", 0))
        head_hash = str(invariant_report.get("head_hash", "0" * 64))
        trial_balances = invariant_report.get("trial_balance_by_currency", {})
        is_ledger_valid = bool(invariant_report.get("is_valid", True))
        violations.extend(invariant_report.get("errors", []))

    # Check for any non-zero imbalance across currencies
    for ccy, tb in trial_balances.items():
        imbalance = tb.get("net_imbalance", 0)
        if imbalance != 0:
            violations.append(
                f"FINANCIAL:imbalance_detected: Currency [{ccy}] debits != credits (net delta: {imbalance})"
            )

    # Inspect reconciliation discrepancies
    recon_total = 0
    recon_matched = 0
    recon_anomalies = 0
    match_rate = 100.0

    if isinstance(recon_report, ReconciliationReport):
        recon_total = recon_report.total_evaluated
        recon_matched = recon_report.counts_by_status.get(ReconciliationStatus.MATCHED.value, 0)
        recon_anomalies = recon_total - recon_matched
        match_rate = recon_report.match_rate_pct
        if recon_anomalies > 0 and not recon_report.adjustment_proposals:
            violations.append(
                f"FINANCIAL:unresolved_reconciliation: {recon_anomalies} discrepancies without proposals"
            )
    elif isinstance(recon_report, dict):
        recon_total = int(recon_report.get("total_evaluated", 0))
        counts = recon_report.get("counts_by_status", {})
        recon_matched = int(counts.get(ReconciliationStatus.MATCHED.value, 0))
        recon_anomalies = recon_total - recon_matched
        match_rate = float(recon_report.get("match_rate_pct", 100.0))

    passed = is_ledger_valid and len(violations) == 0
    score = 1.0 if passed else max(0.0, round(1.0 - (len(violations) / 4.0), 4))

    metrics = {
        "journal_count": journal_count,
        "account_count": account_count,
        "head_hash": head_hash,
        "currencies_verified": sorted(trial_balances.keys()),
        "reconciliation_evaluated": recon_total,
        "reconciliation_matched": recon_matched,
        "reconciliation_anomalies": recon_anomalies,
        "reconciliation_match_rate_pct": match_rate,
        "task_id": task_id,
        "zero_sum_guarantee": "Integer Minor Units (sum(debits) == sum(credits))",
    }

    return {
        "gate_id": "G_FINANCIAL_INVARIANTS",
        "gate_name": "FlowLedger Double-Entry & Reconciliation Gate",
        "passed": passed,
        "score": round(score, 6),
        "violations": violations,
        "metrics": metrics,
    }


def create_aegis_financial_leaf(
    invariant_report: InvariantReport | dict[str, Any] | None = None,
    recon_report: ReconciliationReport | dict[str, Any] | None = None,
    task_id: str = "flowledger_financial_audit",
) -> dict[str, Any]:
    """
    Return the full RFC 6962 attestation package containing canonical payload,
    leaf hash, and domain separation metadata.
    """
    payload = create_aegis_financial_payload(
        invariant_report=invariant_report,
        recon_report=recon_report,
        task_id=task_id,
    )
    leaf_bytes = canonical_json_bytes(payload)
    leaf_hash = hash_leaf_bytes(leaf_bytes)

    return {
        "payload": payload,
        "leaf_hash": leaf_hash,
        "rfc6962_prefix": "0x00",
        "canonical_bytes_len": len(leaf_bytes),
    }
