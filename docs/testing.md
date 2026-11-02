# Testing & Verification Guide

FlowLedger includes 40+ automated tests across four verification suites:

1. **`tests/test_ledger_invariants.py`**:
   - Verifies integer minor-unit enforcement, rejection of floats/booleans/negatives, debit-normal vs. credit-normal account balances, rejection of unbalanced journal entries, multi-currency isolation, SHA-256 hash chain continuity, and tamper detection.
2. **`tests/test_state_machine_and_idempotency.py`**:
   - Verifies legal lifecycle transitions, blocking of `FAILED -> SETTLED` without reconciliation override, out-of-order webhook safety (`SETTLED` before `AUTHORIZED`), duplicate webhook idempotency, SHA-256 request payload fingerprinting, `IdempotencyConflictError` (HTTP 409), and multi-threaded concurrency safety.
3. **`tests/test_webhooks_and_providers.py`**:
   - Verifies constant-time HMAC-SHA256 webhook verification, replay protection windows (`X-Timestamp`), nonce caching, MTN MoMo and Airtel Money fee calculations, exponential backoff with jitter, and failure injection scenarios.
4. **`tests/test_reconciliation_and_api.py`**:
   - Verifies all 6 reconciliation classifications (`MATCHED`, `MISSING_IN_LEDGER`, `MISSING_AT_PROVIDER`, `AMOUNT_MISMATCH`, `FEE_DISCREPANCY`, `STALE_UNSETTLED`), balanced adjustment journal proposals, CSV statement import/export, CLI commands (`demo`, `verify-invariants`), and FastAPI endpoints.

## Running Tests

```bash
uv run --with pytest --with fastapi --with httpx --with pydantic pytest -v
```
