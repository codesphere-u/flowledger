"""Cross-border FX corridor engine: rate-locked quotes and balanced two-journal ledger posting.

Rates are integer-only: `rate_micro` is the number of *target minor units per 1_000_000 source
minor units*. Rounding is floor (in the house's favour for payouts). A quote is valid for
`ttl_seconds` and can be consumed exactly once.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flowledger.ledger.engine import LedgerEngine
from flowledger.ledger.models import AccountType, Currency, EntryDirection, LedgerPosting, validate_minor_units


class FxError(Exception):
    """Base FX error."""


class QuoteExpiredError(FxError):
    pass


class QuoteConsumedError(FxError):
    pass


@dataclass(frozen=True)
class FxQuote:
    quote_id: str
    source: Currency
    target: Currency
    source_amount: int
    rate_micro: int
    spread_bps: int
    target_amount: int  # amount delivered to the beneficiary
    spread_amount: int  # house spread, in target minor units
    expires_at: str


class FxEngine:
    def __init__(self, ledger: LedgerEngine, ttl_seconds: int = 60) -> None:
        self.ledger = ledger
        self.ttl_seconds = ttl_seconds
        self._quotes: dict[str, FxQuote] = {}
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def quote(
        self,
        source: Currency | str,
        target: Currency | str,
        source_amount: int,
        *,
        rate_micro: int,
        spread_bps: int = 100,
        now: datetime | None = None,
    ) -> FxQuote:
        src, tgt = Currency(source), Currency(target)
        if src == tgt:
            raise FxError("Source and target currencies must differ")
        validate_minor_units(source_amount)
        validate_minor_units(rate_micro)
        if not 0 <= spread_bps < 10_000:
            raise FxError("spread_bps must be in [0, 10000)")
        now = now or datetime.now(timezone.utc)
        mid_target = (source_amount * rate_micro) // 1_000_000
        spread = (mid_target * spread_bps) // 10_000
        target_amount = mid_target - spread
        if target_amount <= 0:
            raise FxError("Amount too small: converts to zero after spread")
        q = FxQuote(
            quote_id=f"fxq_{uuid.uuid4().hex[:16]}",
            source=src, target=tgt, source_amount=source_amount, rate_micro=rate_micro,
            spread_bps=spread_bps, target_amount=target_amount, spread_amount=spread,
            expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
        )
        with self._lock:
            self._quotes[q.quote_id] = q
        return q

    def _ensure_accounts(self, q: FxQuote) -> tuple[str, str, str, str, str]:
        s, t = q.source.value.lower(), q.target.value.lower()
        pair = f"{s}_{t}"
        ids = (
            f"liability:customer:funds:{s}",
            f"asset:fx:clearing:{pair}:{s}",
            f"asset:fx:clearing:{pair}:{t}",
            f"liability:customer:funds:{t}",
            f"revenue:fx:spread:{t}",
        )
        self.ledger.create_account(ids[0], f"Customer funds ({q.source.value})", AccountType.LIABILITY, q.source)
        self.ledger.create_account(ids[1], f"FX clearing {pair} source leg", AccountType.ASSET, q.source)
        self.ledger.create_account(ids[2], f"FX clearing {pair} target leg", AccountType.ASSET, q.target)
        self.ledger.create_account(ids[3], f"Customer funds ({q.target.value})", AccountType.LIABILITY, q.target)
        self.ledger.create_account(ids[4], f"FX spread revenue ({q.target.value})", AccountType.REVENUE, q.target)
        return ids

    def execute(self, quote_id: str, *, now: datetime | None = None) -> tuple[str, str]:
        """Consume a quote; return (source_entry_id, target_entry_id). Each journal balances in its own currency."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            q = self._quotes.get(quote_id)
            if q is None:
                raise FxError(f"Unknown quote {quote_id}")
            if quote_id in self._consumed:
                raise QuoteConsumedError(f"Quote {quote_id} already executed")
            if now > datetime.fromisoformat(q.expires_at):
                raise QuoteExpiredError(f"Quote {quote_id} expired at {q.expires_at}")
            self._consumed.add(quote_id)
        src_funds, src_clear, tgt_clear, tgt_funds, spread_acc = self._ensure_accounts(q)
        D, C = EntryDirection.DEBIT, EntryDirection.CREDIT
        e1 = self.ledger.record_journal_entry(
            reference=quote_id, description=f"FX source leg {q.source.value}", currency=q.source,
            postings=[
                LedgerPosting(src_funds, D, q.source_amount, q.source),
                LedgerPosting(src_clear, C, q.source_amount, q.source),
            ],
            metadata={"fx_quote": quote_id, "leg": "source"},
        )
        e2 = self.ledger.record_journal_entry(
            reference=quote_id, description=f"FX target leg {q.target.value}", currency=q.target,
            postings=[
                LedgerPosting(tgt_clear, D, q.target_amount + q.spread_amount, q.target),
                LedgerPosting(tgt_funds, C, q.target_amount, q.target),
                *([LedgerPosting(spread_acc, C, q.spread_amount, q.target)] if q.spread_amount else []),
            ],
            metadata={"fx_quote": quote_id, "leg": "target", "rate_micro": q.rate_micro},
        )
        return e1.entry_id, e2.entry_id
