"""Parser tests against realistic Polymarket Gamma payload shapes.

Shapes are modeled after the actual Gamma response observed in the wild:
- negRisk groups (one Event → many binary YES/NO Markets)
- groupItemTitle carrying the bucket label
- outcomePrices / clobTokenIds arriving as JSON-encoded strings
"""

from polyweather.scanner.models import Event, Market
from polyweather.scanner.parser import (
    detect_city,
    detect_market_type,
    group_flat_markets,
    parse_bucket_label,
    parse_event,
)


def _binary_market(
    condition_id: str,
    group_item_title: str,
    yes_price: str,
    neg_risk_group: str = "0xgroup",
    question: str = "Will NYC high temp be in this bucket on April 25?",
    active: bool = True,
    closed: bool = False,
    liquidity: float = 1500.0,
    volume: float = 5000.0,
) -> dict:
    yes_tok = f"tok_yes_{condition_id[-4:]}"
    no_tok = f"tok_no_{condition_id[-4:]}"
    return {
        "id": int(abs(hash(condition_id)) % 10_000_000),
        "conditionId": condition_id,
        "question": question,
        "slug": f"slug-{group_item_title}",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes_price}", "{1 - float(yes_price):.3f}"]',
        "clobTokenIds": f'["{yes_tok}", "{no_tok}"]',
        "active": active,
        "closed": closed,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidity": liquidity,
        "liquidityNum": liquidity,
        "volume": volume,
        "volumeNum": volume,
        "endDate": "2026-04-25T23:59:59Z",
        "endDateIso": "2026-04-25",
        "negRisk": True,
        "negRiskMarketID": neg_risk_group,
        "groupItemTitle": group_item_title,
        "groupItemThreshold": "4",
    }


# ---------- bucket label parsing ----------

def test_parse_range_whole_degrees():
    lo, hi = parse_bucket_label("70-74")
    assert lo == 70.0 and hi == 74.0


def test_parse_range_with_units():
    lo, hi = parse_bucket_label("65°F to 69°F")
    assert lo == 65.0 and hi == 69.0


def test_parse_above():
    lo, hi = parse_bucket_label("Above 80")
    assert lo == 80.0 and hi is None


def test_parse_below():
    lo, hi = parse_bucket_label("Below 50°F")
    assert lo is None and hi == 50.0


def test_parse_celsius_converts_to_fahrenheit():
    lo, hi = parse_bucket_label("1.24C - 1.30C")
    # 1.24°C = 34.232°F, 1.30°C = 34.34°F
    assert abs(lo - 34.232) < 0.01
    assert abs(hi - 34.34) < 0.01


def test_parse_empty_or_unrecognized():
    assert parse_bucket_label("") == (None, None)
    assert parse_bucket_label("who knows") == (None, None)


# ---------- city detection ----------

def test_detect_city_basic():
    assert detect_city("Highest temperature in NYC on April 25") == "NYC"
    assert detect_city("Los Angeles high temp") == "LAX"
    assert detect_city("Chicago daily max") == "ORD"


def test_detect_city_prefers_longer_alias():
    # "New York" must win over "NYC" when both are present? Both map to NYC
    # anyway. This test guards "la" not accidentally matching inside "Alaska".
    assert detect_city("Alaska high temp") is None


# ---------- market_type detection ----------

def test_market_type_high():
    assert detect_market_type("Highest temperature in NYC on April 25") == "high"
    assert detect_market_type("Max temperature in Chicago today") == "high"


def test_market_type_low():
    assert detect_market_type("Lowest temperature in NYC on April 24") == "low"
    assert detect_market_type("Min temperature in LAX") == "low"


def test_market_type_precip():
    assert detect_market_type("Precipitation in NYC in April?") == "precip"
    assert detect_market_type("Rainfall in Chicago this week") == "precip"
    assert detect_market_type("Snowfall in Boston") == "precip"


def test_market_type_other():
    assert detect_market_type("Wind speed in NYC") == "other"
    assert detect_market_type("") == "other"


# ---------- Event-based parsing ----------

def test_parse_event_with_negrisk_group():
    event_dict = {
        "id": "evt_1",
        "slug": "nyc-temp-apr-25",
        "title": "Highest temperature in NYC on April 25",
        "active": True,
        "closed": False,
        "archived": False,
        "endDate": "2026-04-25T23:59:59Z",
        "markets": [
            _binary_market("0xA1", "Below 65", "0.10"),
            _binary_market("0xA2", "65-69", "0.25"),
            _binary_market("0xA3", "70-74", "0.35"),
            _binary_market("0xA4", "75-79", "0.20"),
            _binary_market("0xA5", "Above 80", "0.10"),
        ],
    }
    event = Event.model_validate(event_dict)
    pe = parse_event(event)
    assert pe is not None
    assert pe.city_code == "NYC"
    assert pe.market_type == "high"
    assert pe.group_id == "0xgroup"
    assert len(pe.buckets) == 5
    labels = {b.outcome_label for b in pe.buckets}
    assert labels == {"Below 65", "65-69", "70-74", "75-79", "Above 80"}
    # liquidity should be sum across buckets
    assert pe.liquidity_usd == 5 * 1500


def test_parse_event_lowest_temperature():
    event_dict = {
        "id": "evt_low",
        "title": "Lowest temperature in NYC on April 24",
        "active": True, "closed": False,
        "endDate": "2026-04-24T23:59:59Z",
        "markets": [
            _binary_market("0xL1", "42-43", "0.05",
                           question="Lowest temperature in NYC on April 24"),
            _binary_market("0xL2", "50-51", "0.25",
                           question="Lowest temperature in NYC on April 24"),
        ],
    }
    event = Event.model_validate(event_dict)
    pe = parse_event(event)
    assert pe is not None
    assert pe.market_type == "low"


def test_parse_event_skips_precipitation():
    event_dict = {
        "id": "evt_precip",
        "title": "Precipitation in NYC in April?",
        "active": True, "closed": False,
        "endDate": "2026-04-30T23:59:59Z",
        "markets": [
            _binary_market("0xP1", ">6\"", "0.01",
                           question="Precipitation in NYC in April"),
        ],
    }
    event = Event.model_validate(event_dict)
    assert parse_event(event) is None


def test_parse_event_skips_closed_markets_within_group():
    event_dict = {
        "id": "evt_2",
        "title": "NYC temperature April 25",
        "active": True,
        "closed": False,
        "endDate": "2026-04-25T23:59:59Z",
        "markets": [
            _binary_market("0xB1", "70-74", "0.30"),
            _binary_market("0xB2", "75-79", "0.25", closed=True),
            _binary_market("0xB3", "Above 80", "0.10"),
        ],
    }
    event = Event.model_validate(event_dict)
    pe = parse_event(event)
    assert pe is not None
    assert len(pe.buckets) == 2  # closed one skipped
    assert {b.outcome_label for b in pe.buckets} == {"70-74", "Above 80"}


def test_parse_event_skips_unknown_city():
    event_dict = {
        "id": "evt_3",
        "title": "Miami temp April 25",
        "active": True,
        "closed": False,
        "endDate": "2026-04-25T23:59:59Z",
        "markets": [_binary_market("0xC1", "70-74", "0.30")],
    }
    event = Event.model_validate(event_dict)
    assert parse_event(event) is None


# ---------- Fallback: flat-market grouping ----------

def test_group_flat_markets_by_neg_risk_id():
    flat = [
        Market.model_validate(_binary_market(
            "0xD1", "70-74", "0.30", "0xgroup_nyc",
            question="Highest temperature in NYC on April 25",
        )),
        Market.model_validate(_binary_market(
            "0xD2", "75-79", "0.25", "0xgroup_nyc",
            question="Highest temperature in NYC on April 25",
        )),
        Market.model_validate(_binary_market(
            "0xE1", "60-64", "0.40", "0xgroup_lax",
            question="Highest temperature in Los Angeles on April 25",
        )),
    ]
    events = group_flat_markets(flat)
    assert len(events) == 2
    by_city = {e.city_code: e for e in events}
    assert set(by_city) == {"NYC", "LAX"}
    assert len(by_city["NYC"].buckets) == 2
    assert len(by_city["LAX"].buckets) == 1
    assert all(e.market_type == "high" for e in events)
