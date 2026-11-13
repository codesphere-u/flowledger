"""End-to-end FlowLedger example: collect an M-Pesa payment, convert UGX->KES, persist, prove inclusion.

Run:  python examples/quickstart.py
Everything is offline; no provider is contacted.
"""

import tempfile
from pathlib import Path

from flowledger import (
    FxEngine,
    LedgerEngine,
    SqliteLedgerStore,
    inclusion_proof,
    merkle_root,
    verify_inclusion,
)
from flowledger.ledger.models import Currency, EntryDirection, LedgerPosting
from flowledger.providers import mpesa


def main() -> dict:
    # 1. Build the Daraja STK Push body for KES 1,500.00 (amounts are integer minor units: cents).
    stk = mpesa.build_stk_push(
        shortcode="174379", passkey="demo-passkey", timestamp="20261109120000",
        amount_minor=150_000, msisdn="0712345678",
        callback_url="https://example.com/mpesa/callback", account_reference="INV-001",
    )
    print("STK Amount sent to Safaricom:", stk["Amount"], "KES")

    # 2. Record the collected funds in a balanced, hash-chained ledger.
    ledger = LedgerEngine()
    ledger.bootstrap_standard_chart_of_accounts("KES")
    ledger.record_journal_entry(
        reference="INV-001", description="M-Pesa collection", currency="KES",
        postings=[
            LedgerPosting("asset:settlement:bank:kes", EntryDirection.DEBIT, 150_000, Currency.KES),
            LedgerPosting("liability:merchant:payable:kes", EntryDirection.CREDIT, 150_000, Currency.KES),
        ],
    )

    # 3. Quote and execute a UGX -> KES conversion (rate_micro = KES minor per 1,000,000 UGX minor).
    fx = FxEngine(ledger)
    quote = fx.quote("UGX", "KES", 3_700_000, rate_micro=35_000, spread_bps=100)
    fx.execute(quote.quote_id)
    print(f"FX: {quote.source_amount} UGX -> {quote.target_amount} KES minor (spread {quote.spread_amount})")

    # 4. Verify invariants, persist durably, restore, and compare the chain head.
    report = ledger.verify_invariants()
    assert report.is_valid, report.errors
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteLedgerStore(Path(tmp) / "ledger.db")
        store.persist(ledger)
        restored = store.restore()
        store.close()
    assert restored.head_hash == ledger.head_hash

    # 5. Prove entry #0 is in the ledger without revealing the others.
    leaves = [bytes.fromhex(e.entry_hash) for e in ledger.list_entries()]
    root = merkle_root(leaves)
    proof = inclusion_proof(leaves, 0)
    assert verify_inclusion(leaves[0], 0, len(leaves), proof, root)
    print("Entries:", len(leaves), "| Merkle root:", root.hex()[:16], "... | inclusion proof verified")
    return {"entries": len(leaves), "valid": report.is_valid}


if __name__ == "__main__":
    main()
