"""Pydantic models must handle Gamma's JSON-encoded list fields."""

from polyweather.scanner.models import Event, Market


def test_market_parses_string_encoded_arrays():
    raw = {
        "id": 1,
        "conditionId": "0xabc",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.42", "0.58"]',
        "clobTokenIds": '["tok_y", "tok_n"]',
        "active": True,
        "closed": False,
    }
    m = Market.model_validate(raw)
    assert m.outcomes == ["Yes", "No"]
    assert m.outcomePrices == ["0.42", "0.58"]
    assert m.yes_price == 0.42
    assert m.yes_token_id == "tok_y"
    assert m.is_tradable


def test_market_is_tradable_rules():
    base = {
        "id": 1, "outcomes": ["Yes", "No"], "outcomePrices": ["0.3", "0.7"],
        "clobTokenIds": ["a", "b"], "active": True, "closed": False,
        "acceptingOrders": True,
    }
    assert Market.model_validate(base).is_tradable
    assert not Market.model_validate({**base, "closed": True}).is_tradable
    assert not Market.model_validate({**base, "active": False}).is_tradable
    assert not Market.model_validate({**base, "archived": True}).is_tradable
    assert not Market.model_validate({**base, "acceptingOrders": False}).is_tradable


def test_event_validates_with_nested_markets():
    raw = {
        "id": "e1",
        "title": "NYC temp",
        "active": True,
        "closed": False,
        "markets": [
            {
                "id": 1, "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.3","0.7"]',
                "clobTokenIds": '["a","b"]',
                "groupItemTitle": "70-74",
                "active": True, "closed": False,
                "acceptingOrders": True,
            }
        ],
        "tags": [{"id": "t1", "slug": "weather", "label": "Weather"}],
    }
    event = Event.model_validate(raw)
    assert event.is_live
    assert len(event.markets) == 1
    assert event.markets[0].groupItemTitle == "70-74"
    assert event.tag_slugs == ["weather"]
