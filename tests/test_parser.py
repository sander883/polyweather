from polyweather.scanner.parser import parse_weather_market


def _market(question, outcomes, prices, token_ids, condition_id="cond_1", **extra):
    d = {
        "conditionId": condition_id,
        "question": question,
        "outcomes": outcomes,
        "outcomePrices": prices,
        "clobTokenIds": token_ids,
        "liquidity": 5000,
        "volume": 20000,
        "endDate": "2026-04-25T23:59:59Z",
    }
    d.update(extra)
    return d


def test_parse_multi_outcome_temperature_market():
    raw = _market(
        question="Highest temperature in NYC on April 25?",
        outcomes=["<65°F", "65-69°F", "70-74°F", "75-79°F", ">=80°F"],
        prices=["0.05", "0.20", "0.40", "0.25", "0.10"],
        token_ids=["t1", "t2", "t3", "t4", "t5"],
    )
    pm = parse_weather_market(raw)
    assert pm is not None
    assert pm.city_code == "NYC"
    assert len(pm.buckets) == 5
    # 70-74 bucket
    b = next(b for b in pm.buckets if b.token_id == "t3")
    assert b.low == 70.0 and b.high == 74.0
    assert b.yes_price == 0.40


def test_parse_binary_above_threshold():
    raw = _market(
        question="Will Los Angeles be above 75°F on April 25?",
        outcomes=["Yes", "No"],
        prices=["0.35", "0.65"],
        token_ids=["y1", "n1"],
    )
    pm = parse_weather_market(raw)
    assert pm is not None
    assert pm.city_code == "LAX"
    assert len(pm.buckets) == 1
    assert pm.buckets[0].low == 75.0
    assert pm.buckets[0].high is None
    assert pm.buckets[0].yes_price == 0.35


def test_parse_unknown_city_returns_none():
    raw = _market(
        question="Will Miami be above 80°F on April 25?",
        outcomes=["Yes", "No"],
        prices=["0.5", "0.5"],
        token_ids=["y", "n"],
    )
    assert parse_weather_market(raw) is None


def test_parse_handles_string_encoded_arrays():
    raw = _market(
        question="Highest temperature in Chicago on April 25?",
        outcomes='["<60", "60-69", "70-79", ">=80"]',
        prices='["0.1", "0.3", "0.4", "0.2"]',
        token_ids='["a","b","c","d"]',
    )
    pm = parse_weather_market(raw)
    assert pm is not None
    assert pm.city_code == "ORD"
    assert len(pm.buckets) == 4
