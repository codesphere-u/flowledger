# ADR 004: ISO 20022 Messaging Subsystem and SQLite WAL Ledger Durability

## Status
Accepted

## Context
Production financial systems require standard inter-bank messaging protocols and crash-resilient persistence:
1. Emerging regional clearing systems (such as Afreximbank's PAPSS) mandate **ISO 20022 XML** messages (`pacs.008` customer credit transfer and `pacs.002` payment status reports).
2. The initial in-memory `LedgerEngine` lacks crash durability across process restarts. Enterprise deployments need atomic, append-only disk persistence with tamper-evident restore verification.

## Decision
1. **ISO 20022 Messaging (`flowledger/reconciliation/iso20022.py`)**:
   - `generate_pacs008` & `parse_pacs008`: Formats and parses `pacs.008.001.10` messages, converting decimal amounts into exact integer minor units per currency exponent.
   - `generate_pacs002` & `parse_pacs002`: Formats and parses `pacs.002.001.12` status reports, mapping `TxSts` codes (`ACCP`, `ACSC`, `RJCT`, `PDNG`) directly to FlowLedger's canonical `PaymentState`.
2. **SQLite WAL Storage Engine (`flowledger/ledger/storage.py`)**:
   - Implemented `SqliteLedgerStore` utilizing SQLite in Write-Ahead Logging (`WAL`) mode with `synchronous = FULL`.
   - SQLite database triggers (`journal_no_update`, `journal_no_delete`) enforce strict append-only constraints at the database engine level.
   - Restoring a ledger replays every stored entry through the standard `record_journal_entry` verification pipeline, asserting that the recomputed `entry_hash` exactly matches the stored hash (`StorageIntegrityError` raised upon drift).
3. **RFC 6962 Merkle Tree Inclusion Proofs (`flowledger/ledger/merkle.py`)**:
   - Implemented RFC 6962 standard Merkle tree calculation over entry hashes with `0x00` leaf and `0x01` internal node domain separation.
   - Generates and verifies $O(\log N)$ logarithmic inclusion proofs allowing auditors to verify transaction existence without exposing other journal records.

## Consequences
### Positive
- Interoperability with PAPSS, EAC, and global ISO 20022 banking rails.
- Crash-safe single-node persistence with hardware sync and tamper-evident restore verification.
- Zero external database daemon dependencies needed for durable single-node execution.
