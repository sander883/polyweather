"""Synthetic end-to-end: Event-shaped payload → forecasts → signal → paper position."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from polyweather.config import get_settings


@pytest.fixture
def isolated_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("POLYWEATHER_DB_PATH", tmp.name)
    get_settings.cache_clear()
    from polyweather.db.init import init_db
    init_db()
    yield tmp.name
    os.unlink(tmp.name)
    get_settings.cache_clear()


def test_event_pipeline_emits_signal_and_opens_paper_position(isolated_db):
    from polyweather.scanner.models import Event
    from polyweather.scanner.parser import parse_event
    from polyweather.fetchers.gfs_ensemble import EnsembleForecast, persist_forecast
    from polyweather.scanner.scan import (
        _upsert_event,
        _find_forecast_id,
        _insert_signal,
    )
    from polyweather.model.probability import Bucket, bucket_probabilities
    from polyweather.sizing.kelly import recommended_size
    from polyweather.trading.paper import execute_pending_signals, list_positions
    from polyweather.db.connection import get_conn

    settle = (datetime.now(timezone.utc) + timedelta(hours=36)).replace(microsecond=0)

    def binary(cond: str, label: str, yes: str) -> dict:
        return {
            "id": int(abs(hash(cond)) % 10_000_000),
            "conditionId": cond,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{yes}", "{1 - float(yes):.3f}"]',
            "clobTokenIds": f'["tok_{cond}", "tok_{cond}_no"]',
            "active": True, "closed": False, "archived": False,
            "acceptingOrders": True,
            "liquidity": 1500, "liquidityNum": 1500,
            "volume": 5000, "volumeNum": 5000,
            "endDate": settle.isoformat(),
            "endDateIso": settle.date().isoformat(),
            "negRisk": True,
            "negRiskMarketID": "0xgroup_synth",
            "groupItemTitle": label,
        }

    # Five-bucket event; market mispriced at 70-74 (market=0.20, model should be ~0.87)
    event_dict = {
        "id": "evt_synth",
        "slug": "nyc-temp-synth",
        "title": "Highest temperature in NYC on test day",
        "active": True, "closed": False, "archived": False,
        "endDate": settle.isoformat(),
        "markets": [
            binary("c1", "Below 65", "0.10"),
            binary("c2", "65-69", "0.25"),
            binary("c3", "70-74", "0.20"),           # <-- under-priced
            binary("c4", "75-79", "0.30"),
            binary("c5", "Above 80", "0.15"),
        ],
    }
    event = Event.model_validate(event_dict)
    pe = parse_event(event)
    assert pe is not None and pe.city_code == "NYC"
    assert len(pe.buckets) == 5

    # Synthetic ensemble concentrated in 70-74; min kept low to validate
    # that the scanner picks the right array based on market_type.
    members_max = [68.0] + [71.0] * 8 + [72.0] * 10 + [73.0] * 8 + [76.0] * 4
    members_min = [52.0] * 31
    fc = EnsembleForecast(
        city_code="NYC",
        source="gfs_ensemble",
        run_time_utc=datetime.now(timezone.utc),
        target_date=settle.date(),
        target_time_utc=settle.replace(hour=23, minute=59, second=59),
        daily_max_f_per_member=members_max,
        daily_min_f_per_member=members_min,
    )
    # Event must be "high" type for this test's expectations
    assert pe.market_type == "high"
    members = fc.samples_for(pe.market_type)
    assert persist_forecast(fc) > 0

    market_id, tok_to_bucket = _upsert_event(pe)
    assert market_id > 0
    fid = _find_forecast_id("NYC", settle.date().isoformat())
    assert fid

    prob_buckets = [Bucket(b.token_id, b.low, b.high) for b in pe.buckets]
    probs = bucket_probabilities(members, prob_buckets)

    # Find the 70-74 bucket's token id
    target_bucket = next(b for b in pe.buckets if b.outcome_label == "70-74")
    p_model = probs[target_bucket.token_id]
    p_market = target_bucket.yes_price
    assert p_model > 0.70
    edge = p_model - p_market
    assert edge > get_settings().edge_threshold

    sz = recommended_size(
        p_model=p_model, price=p_market, bankroll=get_settings().paper_bankroll
    )
    assert sz.size_usd > 0

    sig_id = _insert_signal(
        market_id=market_id,
        bucket_id=tok_to_bucket[target_bucket.token_id],
        forecast_id=fid,
        p_model=p_model,
        p_market=p_market,
        edge=edge,
        ev=p_model / p_market - 1,
        size_usd=sz.size_usd,
        kelly_used=sz.kelly_used,
        reason="synthetic",
    )
    assert sig_id > 0

    opened = execute_pending_signals()
    assert len(opened) == 1
    positions = list_positions(status="OPEN")
    assert len(positions) == 1
    assert positions[0]["city_code"] == "NYC"
    assert positions[0]["side"] == "YES"

    with get_conn() as conn:
        row = conn.execute("SELECT acted FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert row["acted"] == 1
