# Testing & Verification Guide

FlowLedger v2.0 includes **94 automated tests** across 8 specialized verification suites, running in `<0.3s` with zero external network dependencies:

1. **`tests/test_v2_pan_african.py` (28 tests)**:
   - **Safaricom M-Pesa Daraja 2.0**: MSISDN normalization (`254...`), Base64 password calculation per Safaricom spec, whole-shilling KES conversion, STK Push body generation, STK callback result parsing, C2B validation/confirmation responses, and B2C payout request formatting.
   - **Regional & Global Wallets**: Multi-wallet fee calculation across Wave (`XOF`), Orange Money (`XOF`/`XAF`), MTN Ghana (`GHS`), Tigo Pesa (`TZS`), MTN Rwanda (`RWF`), and integer minor-unit conversion for the simulated `USDC`/`EURC` stablecoin rail.
   - **ISO 20022 Financial Messaging**: `pacs.008.001.10` credit transfer generation, XML escaping, decimal-to-minor unit conversion, `pacs.002.001.12` payment status reports, and mapping `pacs.002` TxSts codes (`ACCP`, `ACSC`, `RJCT`, `PDNG`) to internal payment states.
   - **Cross-Border FX Corridors**: Integer-only rate-locked quotes, guaranteed quote TTL expiry, single-use enforcement, balanced two-journal ledger postings, and spread revenue accounting.
   - **Offline Payment Vouchers**: HMAC-SHA256 signature verification, tampering detection, expiration checks, monotonic nonce sequencing, and replay-safe settlement queue draining.
   - **Compliance & AML Watchdog**: Fuzzy Jaro-Winkler name screening against sanctions/PEP lists, token-order insensitivity, structuring detection just below statutory limits, and rolling transaction count velocity alerts.
   - **Durable Storage**: SQLite Write-Ahead Logging (WAL) mode verification, append-only triggers, idempotent state persistence, and hash-chain recomputation upon restore.
   - **Merkle Inclusion Proofs (RFC 6962)**: Exact leaf calculation, power-of-two tree splitting, audit path generation, and verification against corrupted leaves and out-of-bounds indices.
   - **MCP Server v2 Tools**: Tool listing, parameter validation, and execution across all 5 new MCP tools.

2. **`tests/test_ledger_invariants.py` (20 tests)**:
   - Verifies integer minor-unit enforcement, rejection of IEEE-754 floats/booleans/negatives, debit-normal vs. credit-normal account balance derivations, rejection of unbalanced journal entries, multi-currency isolation, SHA-256 hash-chain continuity, and automated detection of ledger tampering.

3. **`tests/test_state_machine_and_idempotency.py` (14 tests)**:
   - Verifies legal lifecycle transitions (`INITIATED` -> `PENDING` -> `AUTHORIZED` -> `SETTLED`), blocking of `FAILED -> SETTLED` without explicit reconciliation override, safe out-of-order webhook absorption, duplicate webhook idempotency, SHA-256 request payload fingerprinting, `IdempotencyConflictError` (HTTP 409), and thread-safe concurrency.

4. **`tests/test_webhooks_and_providers.py` (12 tests)**:
   - Verifies constant-time HMAC-SHA256 signature checking (`hmac.compare_digest`), timestamp replay windows (`X-Timestamp`), nonce deduplication, MTN MoMo and Airtel Money fee calculations, monotonic exponential backoff with jitter, and failure injection scenarios.

5. **`tests/test_reconciliation_and_api.py` (9 tests)**:
   - Verifies all 6 reconciliation classifications (`MATCHED`, `MISSING_IN_LEDGER`, `MISSING_AT_PROVIDER`, `AMOUNT_MISMATCH`, `FEE_DISCREPANCY`, `STALE_UNSETTLED`), balanced adjustment proposals, CSV statement round-trip parsing, CLI commands, and FastAPI endpoints.

6. **`tests/test_split_and_escrow.py` (5 tests)**:
   - Verifies two-phase marketplace escrow holds (`HELD` -> `RELEASED` / `REFUNDED`), split payment allocations with basis points and fixed amounts, basis point sum conservation ($\le 10,000$ bps), and zero-sum ledger conservation.

7. **`tests/test_camt053_and_csv.py` (5 tests)**:
   - Verifies ISO 20022 CAMT.053 XML bank statement parsing, element namespace stripping, malformed payload error handling, automated reconciliation against CAMT.053 records, and CSV exceptions export.

8. **`tests/test_mcp_and_aegis.py` (5 tests)**:
   - Verifies MCP JSON-RPC 2.0 protocol (`initialize`, `ping`, `tools/list`, `tools/call`), Aegis G6 canonical JSON hashing, and RFC 6962 financial attestation leaf generation.

---

## Running the Test Suite

```bash
uv run pytest -v
```
