# FlowLedger System Architecture (v2.0)

## 1. Double-Entry Chart of Accounts

FlowLedger provisions standardized, institutional double-entry chart-of-accounts across 12 supported currencies:
- **East Africa**: `UGX` (Uganda), `KES` (Kenya), `TZS` (Tanzania), `RWF` (Rwanda)
- **West Africa**: `NGN` (Nigeria), `GHS` (Ghana), `XOF` (West African CFA franc)
- **Central & Southern Africa**: `XAF` (Central African CFA franc), `ZAR` (South Africa)
- **Global & Stablecoin Rails**: `USD` (US Dollar), `USDC` (USD Coin - 6 decimal minor units), `EURC` (Euro Coin)

### Standard Chart of Accounts Pattern:

| Account ID Pattern | Classification | Normal Balance | Purpose |
| :--- | :--- | :--- | :--- |
| `asset:clearing:<provider>:<curr>` | `ASSET` | Debit | Funds authorized by customer at provider awaiting bank settlement |
| `asset:settlement:bank:<curr>` | `ASSET` | Debit | Settled cash received in platform settlement trust bank account |
| `asset:receivable:disputes:<curr>` | `ASSET` | Debit | Disputed or short-settled amounts held pending resolution |
| `asset:fx:clearing:<pair>:<curr>` | `ASSET` | Debit | Central clearing bridge account for cross-border currency pairs |
| `liability:merchant:pending:<curr>` | `LIABILITY` | Credit | Unsettled balance owed to merchant upon customer authorization |
| `liability:merchant:payable:<curr>` | `LIABILITY` | Credit | Net settled balance (`amount - platform_fee`) payable to merchant |
| `liability:customer:funds:<curr>` | `LIABILITY` | Credit | Customer deposit funds held for FX conversion or wallet payout |
| `revenue:platform:fees:<curr>` | `REVENUE` | Credit | Earned platform orchestration fees |
| `revenue:fx:spread:<curr>` | `REVENUE` | Credit | Foreign exchange spread revenue earned on cross-border conversion |
| `expense:provider:fees:<curr>` | `EXPENSE` | Debit | Mobile money gateway collection/settlement fees deducted by provider |
| `equity:capital:reserve:<curr>` | `EQUITY` | Credit | Platform operational reserve equity |

---

## 2. Accounting Flows Across Lifecycle States

### Phase A: Customer Authorization (`AUTHORIZED`)
When a customer approves a prompt on MTN MoMo, Airtel Money, or Safaricom M-Pesa STK Push:
- **DEBIT** `asset:clearing:<provider>:<curr>` (`+amount`)
- **CREDIT** `liability:merchant:pending:<curr>` (`+amount`)

### Phase B: Provider Settlement (`SETTLED`)
When the provider settles net funds to the settlement bank account:
- **DEBIT** `asset:settlement:bank:<curr>` (`+amount - provider_fee`)
- **DEBIT** `expense:provider:fees:<curr>` (`+provider_fee`)
- **CREDIT** `asset:clearing:<provider>:<curr>` (`-amount`)
- **DEBIT** `liability:merchant:pending:<curr>` (`-amount`)
- **CREDIT** `liability:merchant:payable:<curr>` (`+amount - platform_fee`)
- **CREDIT** `revenue:platform:fees:<curr>` (`+platform_fee`)

Across all six postings:
$$\sum \text{Debits} = (\text{amount} - \text{provider\_fee}) + \text{provider\_fee} + \text{amount} = 2 \times \text{amount}$$
$$\sum \text{Credits} = \text{amount} + (\text{amount} - \text{platform\_fee}) + \text{platform\_fee} = 2 \times \text{amount}$$

---

## 3. Out-of-Order Webhook Resolution

Mobile money gateways deliver webhooks out of chronological order during network partitions. FlowLedger's `PaymentStateMachine` guarantees:
1. **Early Settlement (`PENDING_PROVIDER` $\rightarrow$ `SETTLED`)**: Automatically synthesizes the missing `AUTHORIZED` ledger phase before committing the `SETTLED` ledger phase.
2. **Late Authorization (`SETTLED` receiving `AUTHORIZED`)**: Recognized via `SAFE_OUT_OF_ORDER_PREDECESSORS` and recorded in `state_history` with `applied=False, out_of_order_noop=True` without emitting duplicate journal entries.

---

## 4. Multi-Rail Ingestion Layer

```text
+-----------------------------------------------------------------------------------+
|                        Multi-Rail Provider Ingestion                              |
+-----------------------------------------------------------------------------------+
|  1. East Africa Mobile Money:                                                     |
|     - Safaricom M-Pesa Daraja 2.0 (STK Push, C2B, B2C, whole-shilling rule)       |
|     - MTN MoMo & Airtel Money (HMAC-SHA256, replay window, nonce deduplication)   |
|     - Tigo Pesa Tanzania, MTN Rwanda                                              |
|                                                                                   |
|  2. West & Central Africa:                                                        |
|     - Wave & Orange Money (XOF / XAF CFA Francs, zero-decimal exponents)          |
|     - MTN Ghana (GHS)                                                             |
|                                                                                   |
|  3. Global & Stablecoin Rails:                                                    |
|     - Simulated USDC / EURC Stablecoin Rail (6 decimal minor units)               |
|     - ISO 20022 CAMT.053 XML Bank Statement Parser                                |
+-----------------------------------------------------------------------------------+
```

---

## 5. Cross-Border FX Corridors & Clearing Bridge

To convert funds between currencies without violating single-currency journal conservation:
1. **Integer Rate Quoting**: Rates are represented as `rate_micro` (target minor units per $1,000,000$ source minor units).
2. **Guaranteed Lock Window**: Quotes are issued with a strict TTL (default 60s) and single-use consumption.
3. **Paired Single-Currency Journals**:
   - **Source Leg**: DR `liability:customer:funds:<src>` / CR `asset:fx:clearing:<pair>:<src>`
   - **Target Leg**: DR `asset:fx:clearing:<pair>:<tgt>` / CR `liability:customer:funds:<tgt>` / CR `revenue:fx:spread:<tgt>`

---

## 6. ISO 20022 Financial Messaging

Implements the messaging standard required by PAPSS (Pan-African Payment and Settlement System) and modern RTGS switches:
- **`pacs.008.001.10` (FI Customer Credit Transfer)**: Generates and parses standard inter-bank credit transfer messages, mapping between decimal XML fields and integer minor units.
- **`pacs.002.001.12` (Payment Status Report)**: Generates and parses status updates (`ACCP`, `ACSC`, `RJCT`, `PDNG`), automatically driving payment state machine transitions.
- **`camt.053.001.08` (Bank-to-Customer Statement)**: Ingests end-of-day bank settlement statements for automated 3-way reconciliation.

---

## 7. Offline-First Agent Banking & Settlement Queue

Enables rural agents, agricultural cooperatives, and market vendors to accept transactions without cellular connectivity:
- **Cryptographic Vouchers**: HMAC-SHA256 signed payloads binding voucher ID, merchant ID, payer ID, minor-unit amount, expiration, and a monotonic counter (`nonce`).
- **Replay Protection**: The `OfflineSettlementQueue` checks both unique `voucher_id` and strict monotonic increasing `nonce` per payer.
- **Asynchronous Batch Settlement**: When connectivity resumes, the queue is drained in nonce order and posted to the double-entry ledger.

---

## 8. Compliance, Sanctions & AML Watchdog

- **Fuzzy Name Screening**: Utilizes Jaro-Winkler distance and order-insensitive tokenization to screen names against sanctions and Politically Exposed Persons (PEP) watchlists.
- **Structuring & Velocity Rules**: Sliding-window tracking monitoring transaction counts, cumulative volume, and transactions split just below statutory compliance thresholds.

---

## 9. Durable Storage & Cryptographic Verification

- **SQLite WAL Storage (`SqliteLedgerStore`)**: Single-node persistent storage operating in Write-Ahead Logging mode (`PRAGMA synchronous = FULL`).
- **Append-Only Triggers**: Engine-level SQL triggers reject `UPDATE` and `DELETE` operations on the journal.
- **Hash-Verified Restore**: Replaying stored journal entries recalculates each SHA-256 hash and asserts exact parity with stored digests.
- **RFC 6962 Merkle Trees**: Computes Merkle roots over journal entry hashes and produces verifiable inclusion audit paths for external auditors.
