# ADR 002: Cross-Border FX Corridors via Two-Journal Single-Currency Settlement

## Status
Accepted

## Context
Cross-border payments across African trade corridors (e.g. Uganda `UGX` to Kenya `KES`, or Nigeria `NGN` to Ghana `GHS`) require converting between currencies without violating FlowLedger's fundamental accounting invariant: every `JournalEntry` must be strictly single-currency with $\sum \text{Debits} \equiv \sum \text{Credits}$. Allowing multi-currency legs within a single journal entry introduces exchange-rate rounding drift and violates zero-sum mathematical conservation.

## Decision
We implemented a **Two-Journal Clearing Bridge** pattern for cross-border FX conversions:
1. **Integer Rate Representation**:
   - Exchange rates are represented as `rate_micro`: target minor units per $1,000,000$ source minor units. All divisions use integer floor arithmetic, preventing floating-point inaccuracies.
2. **Guaranteed Quote Window**:
   - The `FxEngine` issues an `FxQuote` locked for `ttl_seconds` (default 60s) with a unique `quote_id`.
   - Quotes are single-use: executing a quote adds it to a consumed set, preventing duplicate settlements.
3. **Paired Single-Currency Journals**:
   - **Source Leg Journal (Source Currency)**:
     - DEBIT: `liability:customer:funds:<source>` (`source_amount`)
     - CREDIT: `asset:fx:clearing:<pair>:<source>` (`source_amount`)
   - **Target Leg Journal (Target Currency)**:
     - DEBIT: `asset:fx:clearing:<pair>:<target>` (`target_amount + spread_amount`)
     - CREDIT: `liability:customer:funds:<target>` (`target_amount`)
     - CREDIT: `revenue:fx:spread:<target>` (`spread_amount`)
   - Both journals balance independently in integer minor units within their respective currencies. The journals are cryptographically linked by the shared `quote_id` reference in metadata.

## Consequences
### Positive
- Strict zero-sum ledger conservation is preserved in both currencies.
- Eliminates floating-point rounding errors across foreign exchange conversions.
- Institutional auditability: FX spread revenue and clearing balances are explicitly tracked.

### Negative / Trade-offs
- Cross-currency ledger value conservation relies on the validity of the clearing rate rather than a single atomic journal leg.
