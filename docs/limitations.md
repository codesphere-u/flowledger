# Operational Scope & Known Limitations

1. **In-Memory Storage Engine by Default**:
   - FlowLedger v1.0 uses thread-safe in-memory storage (`threading.RLock`) so that the 50-transaction simulation, invariant verifier, and interactive workbench run out-of-the-box with zero external database dependencies. For production deployment, the `LedgerEngine` and `IdempotencyStore` repositories should be backed by PostgreSQL with `SERIALIZABLE` or `REPEATABLE READ` transactions and `SELECT ... FOR UPDATE` row-level account locking.
2. **Simulated Sandbox Providers**:
   - `MtnMomoSandboxAdapter` and `AirtelMoneySandboxAdapter` generate synthetic, cryptographically signed webhooks and settlement statements for architectural demonstration and chaos testing; they do not initiate live telecom USSD pushes.
3. **Single-Currency Journal Entries**:
   - Each `JournalEntry` enforces a single currency across all legs. Cross-currency FX conversion requires pairing two single-currency journal entries through dedicated FX liquidity accounts.
