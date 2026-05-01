"""Point-forecast bucket probability + EV math."""

from polyweather.model.probability import (
    Bucket,
    bucket_prob,
    bucket_probabilities,
    expected_value,
    summary_stats,
)


def test_bucket_contains_half_open():
    b = Bucket("70-74", 70.0, 74.0)
    assert b.contains(70.0) is True
    assert b.contains(73.99) is True
    assert b.contains(74.0) is False
    assert b.contains(69.99) is False


def test_bucket_open_ended():
    hi = Bucket(">=90", 90.0, None)
    lo = Bucket("<50", None, 50.0)
    assert hi.contains(95.0)
    assert not hi.contains(89.99)
    assert lo.contains(49.0)
    assert not lo.contains(50.0)


def test_closed_bucket_is_binary_on_forecast():
    """Closed buckets get 1.0 if the forecast lands inside, 0.0 otherwise."""
    b = Bucket("70-74", 70.0, 74.0)
    assert bucket_prob(72.0, b, sigma=2.0) == 1.0
    assert bucket_prob(70.0, b, sigma=2.0) == 1.0
    assert bucket_prob(73.99, b, sigma=2.0) == 1.0
    assert bucket_prob(74.0, b, sigma=2.0) == 0.0
    assert bucket_prob(69.99, b, sigma=2.0) == 0.0
    assert bucket_prob(60.0, b, sigma=2.0) == 0.0


def test_open_top_bucket_uses_normal_cdf():
    """'>= 90°F' bucket: probability decreases as forecast falls below 90."""
    b = Bucket(">=90", 90.0, None)
    # Forecast right at the boundary → 50%
    assert abs(bucket_prob(90.0, b, sigma=2.0) - 0.5) < 1e-9
    # Forecast 2°F above boundary (one sigma) → ~84%
    p = bucket_prob(92.0, b, sigma=2.0)
    assert 0.83 < p < 0.85
    # Forecast 4°F below boundary (two sigma) → ~2%
    p = bucket_prob(86.0, b, sigma=2.0)
    assert p < 0.03


def test_open_bottom_bucket_uses_normal_cdf():
    """'< 50°F' bucket: probability decreases as forecast rises above 50."""
    b = Bucket("<50", None, 50.0)
    assert abs(bucket_prob(50.0, b, sigma=2.0) - 0.5) < 1e-9
    p = bucket_prob(48.0, b, sigma=2.0)
    assert 0.83 < p < 0.85   # one sigma below boundary
    p = bucket_prob(54.0, b, sigma=2.0)
    assert p < 0.03          # two sigma above boundary


def test_bucket_probabilities_dispatches_correctly():
    """A market with mixed closed + tail buckets returns the right probs."""
    buckets = [
        Bucket("<60", None, 60.0),
        Bucket("60-69", 60.0, 70.0),
        Bucket("70-79", 70.0, 80.0),
        Bucket(">=80", 80.0, None),
    ]
    probs = bucket_probabilities(72.0, buckets, sigma=2.0)
    assert probs["<60"] < 0.001    # 6 sigma below 60
    assert probs["60-69"] == 0.0
    assert probs["70-79"] == 1.0   # closed bucket containing 72°F
    assert probs[">=80"] < 0.001   # 4 sigma below 80


def test_expected_value_positive_when_prob_gt_price():
    # p=0.6, price=0.30 → EV = 0.6 * (1/0.3 - 1) - 0.4 = 1.4 - 0.4 = 1.0
    ev = expected_value(0.6, 0.30)
    assert abs(ev - 1.0) < 1e-9


def test_expected_value_negative_when_overpriced():
    # p=0.2, price=0.50 → EV = 0.2 * 1.0 - 0.8 = -0.6
    ev = expected_value(0.2, 0.50)
    assert abs(ev - (-0.6)) < 1e-9


def test_expected_value_handles_invalid_price():
    assert expected_value(0.5, 0.0) == 0.0
    assert expected_value(0.5, 1.0) == 0.0


def test_summary_stats_basic():
    s = summary_stats([70, 71, 72, 73, 74])
    assert s["n"] == 5
    assert s["mean"] == 72.0
    assert s["min"] == 70.0
    assert s["max"] == 74.0
