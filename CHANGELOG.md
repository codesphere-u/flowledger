# Changelog

All notable changes to **FlowLedger** are documented in this file.

## [1.0.0] - 2026-11-02

### Added
- **Immutable Double-Entry Ledger Engine (`flowledger.ledger`)**:
  - Support for `ASSET`, `LIABILITY`, `REVENUE`, `EXPENSE`, and `EQUITY` account types with strict integer minor-unit arithmetic across `UGX`, `KES`, `NGN`, and `USD`.
  - Balanced `JournalEntry` and `LedgerPosting` validation ($\sum \text{Debits} \equiv \sum \text{Credits}$) and SHA-256 cryptographic hash chaining (`prev_hash` $\rightarrow$ `entry_hash`).
- **Deterministic Payment State Machine (`flowledger.payments.state_machine`)**:
  - Formal transition graph across `INITIATED`, `PENDING_PROVIDER`, `AUTHORIZED`, `SETTLED`, `FAILED`, `REVERSED`, and `DISPUTED`.
  - Safe handling of out-of-order webhooks (e.g., `SETTLED` arriving prior to `AUTHORIZED` callback) and rejection of illegal transitions (`FAILED` $\rightarrow$ `SETTLED`).
- **Idempotency Store & Request Fingerprinting (`flowledger.payments.idempotency`)**:
  - SHA-256 payload fingerprinting per `idempotency_key` with cached deterministic replay and `IdempotencyConflictError` (HTTP 409) on mutated payloads.
- **Cryptographic Webhook Verification & Sandbox Providers (`flowledger.providers`)**:
  - Constant-time HMAC-SHA256 verification with replay window enforcement (`X-Signature`, `X-Timestamp`).
  - `MtnMomoSandboxAdapter` and `AirtelMoneySandboxAdapter` with tiered fee calculation, exponential backoff with jitter, and failure injection simulator.
- **Automated 3-Way Reconciliation Engine (`flowledger.reconciliation`)**:
  - Automated matching across Internal Payment Intents, Double-Entry Ledger Postings, and External Provider Settlement Statements (`MATCHED`, `MISSING_IN_LEDGER`, `MISSING_AT_PROVIDER`, `AMOUNT_MISMATCH`, `FEE_DISCREPANCY`, `STALE_UNSETTLED`) with automated adjustment journal proposals.
- **CLI & Interactive Financial Operations Workbench (`flowledger.cli`, `flowledger.server`)**:
  - `flowledger demo`, `flowledger verify-invariants`, and `flowledger serve` with a dark-mode real-time operations UI.
