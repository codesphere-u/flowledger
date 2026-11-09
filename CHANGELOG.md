# Changelog

All notable changes to **FlowLedger** are documented in this file.

## [1.1.0] - 2026-11-09

### Added
- **ISO 20022 CAMT.053 XML Parser (`flowledger.reconciliation.camt053`)**:
  - Pure Python, zero-dependency XML bank statement parser extracting `<Stmt>`, `<Ntry>`, `<Amt>`, `<CdtDbtInd>`, and `<Chrgs>`.
  - Automatic conversion of decimal currency statements into exact integer minor units (UGX, KES, NGN, USD) eliminating floating-point rounding errors (Closes #1).
- **Automated CSV Reconciliation & Exceptions Exports (`flowledger.reconciliation.engine`)**:
  - `export_reconciliation_csv` and `export_exceptions_csv` utilities.
  - CLI flags `--export-csv`, `--export-exceptions-csv`, `--csv`, and `--exceptions-csv` for direct integration into accounting workflows (Closes #2).
- **Multi-Party Split Payments & Marketplace Escrow (`flowledger.payments.split`)**:
  - `SplitRule` with basis point and fixed fee definitions, strictly preserving $\sum \text{Split} \equiv \text{Net Amount}$.
  - Two-phase `EscrowManager` supporting escrow hold creation, balanced supplier releases, and dispute refunds.
- **Model Context Protocol (MCP) Financial Server (`flowledger.server.mcp`)**:
  - JSON-RPC 2.0 stdio server (`flowledger mcp`) exposing 7 production-grade financial tools (`flowledger_create_payment`, `flowledger_get_account_balance`, `flowledger_verify_ledger_invariants`, `flowledger_run_reconciliation`, `flowledger_create_escrow_hold`, `flowledger_release_escrow`, `flowledger_export_aegis_g6`).
- **Nexus Aegis G6 Bridge (`flowledger.aegis_bridge`)**:
  - `create_aegis_financial_leaf` generating deterministic RFC 6962 SHA-256 Merkle leaves (`0x00 || canonical_json`) and canonical JSON payloads for verification in decentralized audit ledgers (`flowledger export-aegis`).
- **Test Suite**:
  - Expanded unit test suite from 51 to 66 tests with 100% pass rate.

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
