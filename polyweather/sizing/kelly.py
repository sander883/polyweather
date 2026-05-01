"""Fractional Kelly sizing with hard caps."""

from __future__ import annotations

from dataclasses import dataclass

from polyweather.config import get_settings


def kelly_fraction(p_model: float, price: float) -> float:
    """Full Kelly fraction f* = (bp - q) / b.

    Here b is payoff odds: win 1/price − 1 dollars per dollar risked.
    Returns 0 when there's no positive edge.
    """
    if price <= 0 or price >= 1:
        return 0.0
    if p_model <= 0:
        return 0.0
    # p_model == 1.0 is valid (closed-bucket forecast hit) — full Kelly is 1.0
    # and the per-trade/market/city caps will bound the actual size.
    if p_model >= 1.0:
        return 1.0
    b = (1.0 - price) / price
    q = 1.0 - p_model
    f_star = (p_model * b - q) / b
    return max(0.0, f_star)


@dataclass
class SizingResult:
    kelly_full: float
    kelly_used: float            # fractional Kelly applied
    size_usd: float              # final $ size after all caps
    cap_hit: str | None          # which cap bound the size ('kelly' / 'trade' / 'market' / 'city')


def recommended_size(
    *,
    p_model: float,
    price: float,
    bankroll: float,
    already_in_market_usd: float = 0.0,
    already_in_city_today_usd: float = 0.0,
) -> SizingResult:
    s = get_settings()
    f_star = kelly_fraction(p_model, price)
    fraction = s.kelly_fraction
    if p_model >= s.overconfidence_threshold:
        # Until we have enough calibration data to trust very-high p_model
        # values, dampen the Kelly fraction to limit blast radius.
        fraction *= s.overconfidence_kelly_multiplier
    f_used = f_star * fraction

    by_kelly = f_used * bankroll
    by_trade = s.max_pct_per_trade * bankroll
    by_market = max(0.0, s.max_pct_per_market * bankroll - already_in_market_usd)
    by_city = max(0.0, s.max_pct_per_city_day * bankroll - already_in_city_today_usd)

    candidates = {
        "kelly": by_kelly,
        "trade": by_trade,
        "market": by_market,
        "city": by_city,
    }
    cap_name = min(candidates, key=candidates.get)
    size = max(0.0, candidates[cap_name])
    return SizingResult(
        kelly_full=f_star,
        kelly_used=f_used,
        size_usd=round(size, 2),
        cap_hit=cap_name if size > 0 else None,
    )
