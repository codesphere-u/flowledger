"""Tests for African mobile-money corridor fee, levy, and FX transparency calculator."""

from __future__ import annotations

import pytest

from flowledger.payments.corridor_fees import (
    DEFAULT_CORRIDORS,
    CorridorProfile,
    FeeTier,
    compare_corridor_routes,
    quote_corridor_cost,
)


def test_ugx_to_kes_corridor_transparency_breakdown() -> None:
    profile = DEFAULT_CORRIDORS["UGX_KES_MOMO"]
    q = quote_corridor_cost(profile, 200_000)
    assert q.sender_tariff_minor == 3_500
    assert q.statutory_levy_minor == 1_000  # 0.5% of 200,000
    assert q.total_sender_debit_minor == 204_500
    assert q.mid_market_target_minor == 6_960
    assert q.recipient_wallet_minor < q.mid_market_target_minor
    assert q.net_recipient_cash_minor < q.recipient_wallet_minor
    # Effective all-in cost exceeds bare FX spread (220 bps) because it surfaces fee + levy + cashout
    assert q.effective_total_cost_bps > profile.fx_spread_bps

    wallet_only = quote_corridor_cost(profile, 200_000, include_cashout_in_cost=False)
    assert wallet_only.effective_total_cost_bps < q.effective_total_cost_bps


def test_compare_corridor_routes_ranks_cheapest_all_in_route_first() -> None:
    expensive = DEFAULT_CORRIDORS["UGX_KES_MOMO"]
    cheaper = CorridorProfile(
        corridor_id="UGX_KES_PAPSS",
        source_currency="UGX",
        target_currency="KES",
        provider_route="PAPSS Instant Corridor",
        mid_rate_micro=34_800,
        fx_spread_bps=60,
        sender_fee_tiers=(FeeTier(500_000, 1_000),),
        statutory_levy_bps=50,
        recipient_cashout_bps=50,
    )
    ranked = compare_corridor_routes([expensive, cheaper], 200_000)
    assert ranked[0].corridor_id == "UGX_KES_PAPSS"
    assert ranked[0].net_recipient_cash_minor > ranked[1].net_recipient_cash_minor

    with pytest.raises(ValueError):
        quote_corridor_cost(expensive, 0)
