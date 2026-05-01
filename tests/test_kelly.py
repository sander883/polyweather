from polyweather.sizing.kelly import kelly_fraction, recommended_size


def test_kelly_zero_when_no_edge():
    # p == price → no edge
    assert kelly_fraction(0.5, 0.5) == 0.0


def test_kelly_positive_when_edge_exists():
    # classic: p=0.6, price=0.5, b=1 → f* = (0.6*1 - 0.4)/1 = 0.2
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9


def test_kelly_capped_to_zero_on_negative_edge():
    assert kelly_fraction(0.3, 0.5) == 0.0


def test_kelly_edge_cases():
    assert kelly_fraction(0.0, 0.5) == 0.0
    assert kelly_fraction(0.5, 0.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0


def test_kelly_p_one_is_full_bet():
    # Phase 2A — closed-bucket point forecast gives p=1.0 exactly. The full
    # Kelly fraction is 1.0 (bet everything); per-trade caps then bound the
    # actual size.
    assert kelly_fraction(1.0, 0.30) == 1.0
    assert kelly_fraction(1.0, 0.99) == 1.0


def test_recommended_size_respects_trade_cap():
    # p=0.6, price=0.2 → huge Kelly → should be clamped by trade cap
    r = recommended_size(p_model=0.6, price=0.2, bankroll=500.0)
    assert r.size_usd <= 500.0 * 0.03 + 0.01  # 3% cap
    assert r.cap_hit in {"trade", "kelly", "market", "city"}


def test_recommended_size_respects_market_cap():
    # Already deep in this market → market cap should bind
    r = recommended_size(
        p_model=0.6, price=0.2, bankroll=500.0,
        already_in_market_usd=40.0,  # 8% of 500 = 40 → market remaining 0
    )
    assert r.size_usd == 0.0


def test_overconfidence_dampener_halves_kelly():
    # p=0.84 is below the 0.85 threshold, so it gets the full ¼ Kelly.
    # p=0.95 is over the threshold, so its Kelly fraction is halved (×0.5).
    # Compare kelly_used / kelly_full ratios to neutralize differing f*.
    safe = recommended_size(p_model=0.84, price=0.40, bankroll=10_000.0)
    over = recommended_size(p_model=0.95, price=0.40, bankroll=10_000.0)

    safe_ratio = safe.kelly_used / safe.kelly_full
    over_ratio = over.kelly_used / over.kelly_full

    assert abs(safe_ratio - 0.25) < 1e-9
    assert abs(over_ratio - 0.125) < 1e-9   # 0.25 × 0.5 dampener
