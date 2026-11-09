# FlowLedger v2.0 Architecture Additions

```text
 Providers (offline builders/parsers)      Compliance
 M-Pesa STK/C2B/B2C | Wave/Orange | sim     Sanctions (Jaro-Winkler) + Velocity
        |                                          |
        v                                          v
 Offline vouchers (HMAC, nonce) ---> Orchestrator / State machine ---> FX engine (rate-locked)
                                              |                              |
                                              v                              v
                                   LedgerEngine (double-entry, hash chain, integer minor units)
                                              |
                          +-------------------+--------------------+
                          v                                        v
              SqliteLedgerStore (WAL, append-only)      Merkle root + inclusion proofs (RFC 6962)
                                              |
                              ISO 20022 pacs.008/002, CAMT.053, MCP tools
```

## FX accounting
A quote converts `source_amount` at `rate_micro` (target minor per 1e6 source minor), floors, and splits `mid = delivered + spread`.

Source journal (source currency): DR `liability:customer:funds:src` / CR `asset:fx:clearing:<pair>:src`.
Target journal (target currency): DR `asset:fx:clearing:<pair>:tgt` (delivered+spread) / CR `liability:customer:funds:tgt` (delivered) / CR `revenue:fx:spread:tgt` (spread).
Each journal balances in its own currency; the quote ID links them via `reference` and metadata.

## Storage guarantees
`persist` is one `BEGIN IMMEDIATE` transaction and idempotent by `entry_id`. `restore` replays entries through the normal validation path and aborts with `StorageIntegrityError` if any recomputed `entry_hash` differs from the stored one. SQLite triggers reject `UPDATE`/`DELETE` on the journal.

## Merkle proofs
Leaves are entry hashes. Root and audit paths follow RFC 6962 (`0x00` leaf / `0x01` node prefixes, split at the largest power of two below n). `verify_inclusion` recomputes the root from leaf, index, tree size and path.
