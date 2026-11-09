"""Regional wallet profiles (Wave, Orange Money, M-Pesa) and a simulated stablecoin rail.

Fee schedules here are *simulation defaults*, not published provider tariffs. Every figure is
integer basis points so ledger postings stay float-free.
"""

from __future__ import annotations

from dataclasses import dataclass

from flowledger.ledger.models import Currency, validate_minor_units


@dataclass(frozen=True)
class WalletProfile:
    provider_id: str
    display_name: str
    currency: Currency
    fee_bps: int
    minor_unit_exponent: int  # decimal places of the currency's minor unit

    def provider_fee(self, amount_minor: int) -> int:
        validate_minor_units(amount_minor)
        return max(1, (amount_minor * self.fee_bps) // 10_000)


WALLET_PROFILES: dict[str, WalletProfile] = {
    "mpesa": WalletProfile("mpesa", "Safaricom M-Pesa", Currency.KES, 100, 2),
    "wave_sn": WalletProfile("wave_sn", "Wave (Senegal)", Currency.XOF, 100, 0),
    "orange_ci": WalletProfile("orange_ci", "Orange Money (Cote d'Ivoire)", Currency.XOF, 150, 0),
    "orange_cm": WalletProfile("orange_cm", "Orange Money (Cameroon)", Currency.XAF, 150, 0),
    "mtn_gh": WalletProfile("mtn_gh", "MTN MoMo (Ghana)", Currency.GHS, 100, 2),
    "tigopesa_tz": WalletProfile("tigopesa_tz", "Tigo Pesa (Tanzania)", Currency.TZS, 120, 2),
    "mtn_rw": WalletProfile("mtn_rw", "MTN MoMo (Rwanda)", Currency.RWF, 100, 0),
}


def get_wallet(provider_id: str) -> WalletProfile:
    try:
        return WALLET_PROFILES[provider_id]
    except KeyError as exc:
        raise KeyError(f"Unknown wallet provider '{provider_id}'") from exc


class SimulatedStablecoinRail:
    """Deterministic USDC/EURC conversion helper. No chain access; for ledger design and tests.

    Stablecoin amounts use 6 decimals (1 USDC = 1_000_000 minor units).
    `rate_micro` is units of stablecoin minor per 1 unit of fiat minor, scaled by 1e6.
    """

    def __init__(self, fee_bps: int = 50) -> None:
        self.fee_bps = fee_bps

    def quote(self, fiat_minor: int, usd_per_fiat_minor_micro: int) -> dict[str, int]:
        """Return gross/fee/net stablecoin minor units for a fiat deposit."""
        validate_minor_units(fiat_minor)
        validate_minor_units(usd_per_fiat_minor_micro)
        gross = (fiat_minor * usd_per_fiat_minor_micro) // 1_000_000 * 1  # stable minor units
        fee = (gross * self.fee_bps) // 10_000
        return {"gross": gross, "fee": fee, "net": gross - fee}
