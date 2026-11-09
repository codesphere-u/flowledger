# ADR 003: Offline Cryptographic Vouchers & Replay-Safe Settlement Queue

## Status
Accepted

## Context
Rural commerce, agricultural cooperatives, and transport corridors across emerging markets frequently operate with intermittent or non-existent cellular internet connectivity. POS terminals and field agents cannot depend on live HTTP/REST callbacks to accept customer payments. However, accepting transactions offline introduces severe double-spending and replay risks when payments are subsequently submitted for online settlement.

## Decision
We implemented a zero-dependency **Offline Voucher & Asynchronous Queue** architecture:
1. **Cryptographically Signed Vouchers (`Voucher`)**:
   - Vouchers encode `(voucher_id, merchant_id, payer_id, amount, currency, nonce, expires_at)`.
   - Signed using HMAC-SHA256 under a pre-shared merchant key (pure Python standard library).
2. **Replay & Stale-Nonce Protection**:
   - The `OfflineSettlementQueue` tracks previously accepted voucher IDs (`_seen_ids`) to reject exact replay attempts.
   - A monotonic per-payer counter (`nonce > _last_nonce[payer_id]`) prevents replay of previous vouchers or sequence tampering.
3. **Bulk Settlement Draining**:
   - When network connectivity is re-established, the queue's `drain()` method releases verified vouchers in strict nonce order for atomic double-entry ledger posting.

## Consequences
### Positive
- Allows offline merchants to securely accept micro-payments with zero network connectivity.
- Guaranteed replay protection through voucher ID deduplication and monotonic nonces.
- Zero external cryptography dependencies (pure `hmac` and `hashlib`).

### Negative / Trade-offs
- HMAC requires a shared secret between payer/terminal and merchant, suitable for closed-loop wallets and agent networks. Public-key asymmetric signatures (e.g. Ed25519) can be added when external dependencies are permitted.
