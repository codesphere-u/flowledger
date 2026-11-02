# FlowLedger Architecture

## 1. Double-Entry Chart of Accounts

For each supported currency (`UGX`, `KES`, `NGN`, `USD`), FlowLedger provisions a standardized 9-account chart of accounts:

| Account ID Pattern | Classification | Normal Balance | Purpose |
| :--- | :--- | :--- | :--- |
| `asset:clearing:mtn_momo:<curr>` | `ASSET` | Debit | Funds authorized by customer in MTN MoMo awaiting bank settlement |
| `asset:clearing:airtel_money:<curr>` | `ASSET` | Debit | Funds authorized by customer in Airtel Money awaiting bank settlement |
| `asset:settlement:bank:<curr>` | `ASSET` | Debit | Settled cash received in platform settlement trust bank account |
| `asset:receivable:disputes:<curr>` | `ASSET` | Debit | Disputed or short-settled amounts held pending resolution |
| `liability:merchant:pending:<curr>` | `LIABILITY` | Credit | Unsettled balance owed to merchant upon customer authorization |
| `liability:merchant:payable:<curr>` | `LIABILITY` | Credit | Net settled balance (`amount - platform_fee`) payable to merchant |
| `revenue:platform:fees:<curr>` | `REVENUE` | Credit | Earned platform orchestration fees |
| `expense:provider:fees:<curr>` | `EXPENSE` | Debit | Mobile money gateway collection/settlement fees deducted by provider |
| `equity:capital:reserve:<curr>` | `EQUITY` | Credit | Platform operational reserve equity |

## 2. Accounting Flows Across Lifecycle States

### Phase A: Customer Authorization (`AUTHORIZED`)
When a customer approves the USSD push prompt on MTN MoMo or Airtel Money:
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

Notice that across all six legs:
$$\sum \text{Debits} = (\text{amount} - \text{provider\_fee}) + \text{provider\_fee} + \text{amount} = 2 \times \text{amount}$$
$$\sum \text{Credits} = \text{amount} + (\text{amount} - \text{platform\_fee}) + \text{platform\_fee} = 2 \times \text{amount}$$

## 3. Out-of-Order Webhook Resolution

Mobile money gateways frequently deliver webhooks out of chronological order during network partitions. FlowLedger's `PaymentStateMachine` guarantees:
1. **Early Settlement (`PENDING_PROVIDER` $\rightarrow$ `SETTLED`)**: Automatically synthesizes the missing `AUTHORIZED` ledger phase before committing the `SETTLED` ledger phase.
2. **Late Authorization (`SETTLED` receiving `AUTHORIZED`)**: Recognized via `SAFE_OUT_OF_ORDER_PREDECESSORS` and recorded in `state_history` with `applied=False, out_of_order_noop=True` without emitting duplicate journal entries.
