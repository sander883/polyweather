from polyweather.model.probability import Bucket, bucket_probabilities, summary_stats


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


def test_probabilities_sum_to_one():
    samples = [68, 69, 71, 71, 72, 73, 74, 75, 75, 76]
    buckets = [
        Bucket("<70", None, 70.0),
        Bucket("70-74", 70.0, 74.0),
        Bucket("74-78", 74.0, 78.0),
        Bucket(">=78", 78.0, None),
    ]
    probs = bucket_probabilities(samples, buckets, alpha=0.0)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs["<70"] == 0.2          # 68, 69
    assert probs["70-74"] == 0.4        # 71, 71, 72, 73 (74 excluded, half-open)
    assert probs["74-78"] == 0.4        # 74, 75, 75, 76
    assert probs[">=78"] == 0.0


def test_laplace_smoothing_no_zero():
    samples = [72.0] * 31
    buckets = [
        Bucket("a", None, 50.0),
        Bucket("b", 50.0, 70.0),
        Bucket("c", 70.0, 74.0),
        Bucket("d", 74.0, None),
    ]
    probs = bucket_probabilities(samples, buckets, alpha=0.5)
    for v in probs.values():
        assert v > 0.0
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    # The bucket containing the mass should still dominate
    assert probs["c"] > 0.9


def test_summary_stats_basic():
    s = summary_stats([70, 71, 72, 73, 74])
    assert s["n"] == 5
    assert s["mean"] == 72.0
    assert s["min"] == 70.0
    assert s["max"] == 74.0
