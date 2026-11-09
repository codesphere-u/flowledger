# Operational Scope & Known Limitations

Read this before production use. FlowLedger v2.0 is a correctness-first engine and reference implementation, not a licensed payment service.

1. **Live provider I/O is not included.** The M-Pesa module builds and parses Daraja payloads but performs no HTTP calls, OAuth, or RSA `SecurityCredential` encryption. MTN/Airtel/Wave/Orange adapters are simulations. Fee tiers are illustrative, not published tariffs. Integrating a provider requires their credentials, certification, and sandbox testing.
2. **Storage.** `LedgerEngine` is in-memory and thread-safe within one process. `SqliteLedgerStore` persists and restores it durably (WAL, append-only triggers, hash-verified replay) but is single-node. Multi-writer / multi-node deployments need PostgreSQL with serializable transactions (not yet implemented).
3. **Single-currency journals.** Each `JournalEntry` is one currency. FX is two balanced journals linked by quote ID; the ledger does not itself prove cross-currency value conservation. Rates are caller-supplied; no live rate feed.
4. **ISO 20022 is a pragmatic subset.** pacs.008/pacs.002 are not validated against the full XSD and have not been certified by PAPSS, SWIFT, or any scheme.
5. **Vouchers use HMAC (shared key), not public-key signatures.** They prove integrity and issuer identity to key holders but give no third-party non-repudiation. Offline double-spend across *different* offline merchants is bounded only by voucher limits you set, not eliminated.
6. **Compliance is a signal, not a determination.** The bundled watchlist is synthetic. Load official, licensed lists and have a compliance officer review hits. Thresholds are defaults, not regulatory advice.
7. **Stablecoin rail is a simulation** with no chain access.
