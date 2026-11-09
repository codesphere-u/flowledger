"""Offline-first signed payment vouchers and a replay-safe settlement queue.

Vouchers are authenticated with HMAC-SHA256 under a per-merchant shared key (stdlib only). This
proves integrity and issuer identity to anyone holding the key; it is not a public-key signature,
so it does not give third-party non-repudiation. Ed25519 can be layered in via the `Signer`
protocol when a crypto dependency is acceptable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from flowledger.ledger.models import Currency, validate_minor_units


class VoucherError(Exception):
    pass


class InvalidSignatureError(VoucherError):
    pass


class VoucherExpiredError(VoucherError):
    pass


class VoucherReplayError(VoucherError):
    pass


@dataclass(frozen=True)
class Voucher:
    voucher_id: str
    merchant_id: str
    payer_id: str
    amount: int
    currency: Currency
    nonce: int  # strictly increasing per payer
    expires_at: str
    signature: str = ""

    def payload(self) -> bytes:
        return json.dumps(
            {
                "voucher_id": self.voucher_id, "merchant_id": self.merchant_id, "payer_id": self.payer_id,
                "amount": self.amount, "currency": Currency(self.currency).value,
                "nonce": self.nonce, "expires_at": self.expires_at,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()


def sign_voucher(v: Voucher, key: bytes) -> Voucher:
    validate_minor_units(v.amount)
    sig = hmac.new(key, v.payload(), hashlib.sha256).hexdigest()
    return Voucher(**{**v.__dict__, "signature": sig})


def verify_voucher(v: Voucher, key: bytes, *, now: datetime | None = None) -> None:
    expected = hmac.new(key, v.payload(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v.signature):
        raise InvalidSignatureError("Voucher signature mismatch")
    now = now or datetime.now(timezone.utc)
    if now > datetime.fromisoformat(v.expires_at):
        raise VoucherExpiredError(f"Voucher {v.voucher_id} expired")


class OfflineSettlementQueue:
    """Collects vouchers accepted offline and settles them in bulk once connectivity returns.

    Replay protection: a voucher_id may settle once, and each payer's nonce must strictly
    increase, so a captured voucher cannot be re-presented.
    """

    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = keys  # merchant_id -> key
        self._pending: list[Voucher] = []
        self._seen_ids: set[str] = set()
        self._last_nonce: dict[str, int] = {}
        self._lock = threading.Lock()

    def accept(self, v: Voucher, *, now: datetime | None = None) -> None:
        key = self._keys.get(v.merchant_id)
        if key is None:
            raise VoucherError(f"No key for merchant {v.merchant_id}")
        verify_voucher(v, key, now=now)
        with self._lock:
            if v.voucher_id in self._seen_ids:
                raise VoucherReplayError(f"Voucher {v.voucher_id} already accepted")
            if v.nonce <= self._last_nonce.get(v.payer_id, 0):
                raise VoucherReplayError(f"Stale nonce {v.nonce} for payer {v.payer_id}")
            self._seen_ids.add(v.voucher_id)
            self._last_nonce[v.payer_id] = v.nonce
            self._pending.append(v)

    def drain(self) -> list[Voucher]:
        """Return pending vouchers in nonce order and clear the queue (to be settled in the ledger)."""
        with self._lock:
            out = sorted(self._pending, key=lambda x: (x.payer_id, x.nonce))
            self._pending = []
            return out

    def __len__(self) -> int:
        return len(self._pending)
