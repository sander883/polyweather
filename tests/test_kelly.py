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
    assert kelly_fraction(1.0, 0.5) == 0.0
    assert kelly_fraction(0.5, 0.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0


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
