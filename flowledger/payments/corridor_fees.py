"""Cross-border African Mobile-Money corridor fee, statutory levy, and effective FX cost calculator.

All monetary amounts use exact integer minor units (e.g., UGX, KES, TZS, RWF, GHS, NGN)
and basis points (1 bps = 0.01%) to eliminate floating-point rounding drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeTier:
    """Inclusive upper-bound flat telco tariff tier in integer minor units."""

    max_amount_minor: int
    flat_fee_minor: int


@dataclass(frozen=True)
class CorridorProfile:
    """Tariff and statutory levy configuration for a cross-border mobile-money corridor."""

    corridor_id: str
    source_currency: str
    target_currency: str
    provider_route: str
    mid_rate_micro: int  # target minor units per 1_000_000 source minor units
    fx_spread_bps: int  # hidden FX markup in basis points
    sender_fee_tiers: tuple[FeeTier, ...]
    statutory_levy_bps: int  # e.g., Uganda 0.5% MoMo withdrawal levy = 50 bps
    recipient_cashout_bps: int  # estimated destination cash-out cost in bps


@dataclass(frozen=True)
class CorridorQuoteBreakdown:
    """Transparent all-in cost breakdown comparing mid-market value vs net recipient value."""

    corridor_id: str
    provider_route: str
    source_currency: str
    target_currency: str
    send_amount_minor: int
    sender_tariff_minor: int
    statutory_levy_minor: int
    total_sender_debit_minor: int
    mid_market_target_minor: int
    fx_spread_target_minor: int
    recipient_wallet_minor: int
    recipient_cashout_fee_minor: int
    net_recipient_cash_minor: int
    effective_total_cost_bps: int  # All-in drag (sender fee + levy + FX spread + cash-out) in bps


# Reference East/West African mobile-money corridor schedules (configurable by operators)
DEFAULT_CORRIDORS: dict[str, CorridorProfile] = {
    "UGX_KES_MOMO": CorridorProfile(
        corridor_id="UGX_KES_MOMO",
        source_currency="UGX",
        target_currency="KES",
        provider_route="MTN MoMo UG -> M-Pesa KE",
        mid_rate_micro=34_800,  # 1,000,000 UGX ~= 34,800 KES
        fx_spread_bps=220,
        sender_fee_tiers=(
            FeeTier(50_000, 1_500),
            FeeTier(250_000, 3_500),
            FeeTier(1_000_000, 7_500),
            FeeTier(5_000_000, 15_000),
        ),
        statutory_levy_bps=50,  # 0.5% statutory mobile money levy
        recipient_cashout_bps=120,
    ),
    "KES_UGX_MPESA": CorridorProfile(
        corridor_id="KES_UGX_MPESA",
        source_currency="KES",
        target_currency="UGX",
        provider_route="M-Pesa KE -> Airtel/MTN UG",
        mid_rate_micro=28_700_000,  # 1,000,000 KES ~= 28,700,000 UGX
        fx_spread_bps=200,
        sender_fee_tiers=(
            FeeTier(2_500, 65),
            FeeTier(10_000, 160),
            FeeTier(50_000, 350),
            FeeTier(250_000, 600),
        ),
        statutory_levy_bps=150,  # Excise on transfer fees / levy proxy
        recipient_cashout_bps=110,
    ),
    "GHS_NGN_MOMO": CorridorProfile(
        corridor_id="GHS_NGN_MOMO",
        source_currency="GHS",
        target_currency="NGN",
        provider_route="MTN MoMo GH -> Bank/OPay NG",
        mid_rate_micro=105_000_000,
        fx_spread_bps=280,
        sender_fee_tiers=(
            FeeTier(500, 10),
            FeeTier(2_000, 25),
            FeeTier(10_000, 60),
        ),
        statutory_levy_bps=100,
        recipient_cashout_bps=50,
    ),
}


def lookup_tier_fee(amount_minor: int, tiers: tuple[FeeTier, ...]) -> int:
    """Return the flat sender tariff for `amount_minor` using ascending upper-bound tiers."""
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    if not tiers:
        return 0
    for tier in sorted(tiers, key=lambda t: t.max_amount_minor):
        if amount_minor <= tier.max_amount_minor:
            return tier.flat_fee_minor
    return max(tiers, key=lambda t: t.max_amount_minor).flat_fee_minor


def quote_corridor_cost(
    profile: CorridorProfile,
    send_amount_minor: int,
    *,
    include_cashout_in_cost: bool = True,
) -> CorridorQuoteBreakdown:
    """Calculate transparent integer fee, tax levy, FX spread, and effective bps drag."""
    if send_amount_minor <= 0:
        raise ValueError("send_amount_minor must be positive")
    if profile.mid_rate_micro <= 0:
        raise ValueError("mid_rate_micro must be positive")

    sender_fee = lookup_tier_fee(send_amount_minor, profile.sender_fee_tiers)
    levy = (send_amount_minor * profile.statutory_levy_bps + 5_000) // 10_000
    total_debit = send_amount_minor + sender_fee + levy

    # Benchmark: what total_debit would buy at zero-fee mid-market FX
    benchmark_mid_target = (total_debit * profile.mid_rate_micro) // 1_000_000
    mid_target = (send_amount_minor * profile.mid_rate_micro) // 1_000_000
    fx_spread_target = (mid_target * profile.fx_spread_bps) // 10_000
    wallet_target = max(0, mid_target - fx_spread_target)
    cashout_fee = (wallet_target * profile.recipient_cashout_bps + 5_000) // 10_000
    net_cash = max(0, wallet_target - cashout_fee)

    delivered_for_cost = net_cash if include_cashout_in_cost else wallet_target
    if benchmark_mid_target > 0:
        drag_bps = max(
            0,
            ((benchmark_mid_target - delivered_for_cost) * 10_000) // benchmark_mid_target,
        )
    else:
        drag_bps = 0

    return CorridorQuoteBreakdown(
        corridor_id=profile.corridor_id,
        provider_route=profile.provider_route,
        source_currency=profile.source_currency,
        target_currency=profile.target_currency,
        send_amount_minor=send_amount_minor,
        sender_tariff_minor=sender_fee,
        statutory_levy_minor=levy,
        total_sender_debit_minor=total_debit,
        mid_market_target_minor=mid_target,
        fx_spread_target_minor=fx_spread_target,
        recipient_wallet_minor=wallet_target,
        recipient_cashout_fee_minor=cashout_fee,
        net_recipient_cash_minor=net_cash,
        effective_total_cost_bps=drag_bps,
    )


def compare_corridor_routes(
    profiles: list[CorridorProfile],
    send_amount_minor: int,
    *,
    include_cashout_in_cost: bool = True,
) -> list[CorridorQuoteBreakdown]:
    """Rank competing corridor routes from lowest to highest effective all-in cost (bps)."""
    quotes = [
        quote_corridor_cost(p, send_amount_minor, include_cashout_in_cost=include_cashout_in_cost)
        for p in profiles
    ]
    quotes.sort(key=lambda q: (q.effective_total_cost_bps, -q.net_recipient_cash_minor))
    return quotes
