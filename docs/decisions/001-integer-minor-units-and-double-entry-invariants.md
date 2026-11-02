# ADR-001: Integer Minor Units and Double-Entry Hash-Chain Invariants

- **Status**: Accepted
- **Date**: 2026-11-02

## Context

Payment systems operating across African mobile money rails (`UGX`, `KES`, `NGN`) and international settlement currencies (`USD`) process high transaction volumes with percentage-based gateway fees and tiered merchant commissions. Representing monetary values with IEEE-754 floating-point numbers (`float`) introduces binary representation drift (e.g., `0.1 + 0.2 != 0.3`), which causes penny/shilling imbalances in trial balances and breaks automated reconciliation.

Furthermore, single-entry status tables (`UPDATE payments SET status = 'SETTLED'`) lack an auditable trail of how money moved between clearing accounts, settlement banks, fee expenses, and merchant payables.

## Decision

1. **Strict Integer Minor Units**:
   - Every monetary amount, fee, and account balance in FlowLedger is typed as `int` and validated at runtime via `validate_minor_units()`.
   - Passing a `float` or `bool` raises `TypeError` (`InvalidAmountError`) immediately at the domain boundary.
   - Fee calculations use deterministic integer basis-point arithmetic (`(amount * bps) // 10_000`).

2. **Balanced Double-Entry Postings**:
   - Every `JournalEntry` requires at least two `LedgerPosting` legs within the same currency where $\sum \text{Debits} \equiv \sum \text{Credits} > 0$.

3. **SHA-256 Cryptographic Hash Chaining**:
   - Each `JournalEntry` commits `entry_hash = sha256(prev_hash + canonical_json_payload)`, linking back to `GENESIS_HASH` (`"0" * 64`).
   - Any post-hoc mutation of an entry's amount, account, timestamp, or metadata invalidates the chain and is caught by `LedgerEngine.verify_invariants()`.

## Consequences

- **Positive**: Zero floating-point drift; mathematical proof of balance conservation across all accounts; immediate tamper detection.
- **Negative**: API callers must convert display decimals to integer minor units before calling FlowLedger APIs.
