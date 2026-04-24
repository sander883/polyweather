"""Synthetic end-to-end test: no network, no Open-Meteo, no Polymarket.

We inject a ParsedMarket + an EnsembleForecast directly, then drive the same
functions used by scan_once: upsert, probability scoring, signal insertion,
paper execution. This verifies the full in-process pipeline is wired correctly.
"""

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


def test_synthetic_pipeline_emits_signal_and_opens_paper_position(isolated_db):
    from polyweather.scanner.parser import ParsedMarket, ParsedBucket
    from polyweather.fetchers.gfs_ensemble import EnsembleForecast, persist_forecast
    from polyweather.scanner.scan import (
        _upsert_market,
        _find_forecast_id,
        _insert_signal,
    )
    from polyweather.model.probability import Bucket, bucket_probabilities
    from polyweather.sizing.kelly import recommended_size
    from polyweather.trading.paper import execute_pending_signals, list_positions
    from polyweather.db.connection import get_conn

    settle = datetime.now(timezone.utc) + timedelta(hours=36)

    # Synthetic market: NYC temperature, 4 buckets, one is under-priced
    pm = ParsedMarket(
        condition_id="cond_synth_1",
        slug="nyc-temp-synth",
        question="Highest temperature in NYC on test day?",
        city_code="NYC",
        settle_time_utc=settle,
        liquidity_usd=5000.0,
        volume_usd=20000.0,
        buckets=[
            ParsedBucket("t1", "<65", None, 65.0, 0.10),
            ParsedBucket("t2", "65-69", 65.0, 69.0, 0.25),
            ParsedBucket("t3", "70-74", 70.0, 74.0, 0.20),   # ← we'll concentrate here
            ParsedBucket("t4", ">=74", 74.0, None, 0.45),
        ],
        raw={"synthetic": True},
    )

    # Synthetic ensemble: strongly concentrated around 71-73 → model says P(70-74) >> 0.20
    members = [68.0] + [71.0] * 8 + [72.0] * 10 + [73.0] * 8 + [76.0] * 4
    fc = EnsembleForecast(
        city_code="NYC",
        source="gfs_ensemble",
        run_time_utc=datetime.now(timezone.utc),
        target_date=settle.date(),
        target_time_utc=settle.replace(hour=23, minute=59, second=59, microsecond=0),
        daily_max_f_per_member=members,
    )
    forecast_id = persist_forecast(fc)
    assert forecast_id > 0

    market_id, token_to_bucket = _upsert_market(pm)
    assert market_id > 0
    assert len(token_to_bucket) == 4

    fid = _find_forecast_id("NYC", settle.date().isoformat())
    assert fid == forecast_id

    buckets_for_prob = [Bucket(b.token_id, b.low, b.high) for b in pm.buckets]
    probs = bucket_probabilities(members, buckets_for_prob)
    # 70-74 should dominate
    assert probs["t3"] > 0.70
    assert probs["t3"] > probs["t1"] + probs["t2"] + probs["t4"]

    s = get_settings()
    edge = probs["t3"] - 0.20
    assert edge > s.edge_threshold    # confirm it passes the filter
    ev = probs["t3"] / 0.20 - 1.0
    assert ev > 0

    sz = recommended_size(p_model=probs["t3"], price=0.20, bankroll=s.paper_bankroll)
    assert sz.size_usd > 0

    sig_id = _insert_signal(
        market_id=market_id,
        bucket_id=token_to_bucket["t3"],
        forecast_id=fid,
        p_model=probs["t3"],
        p_market=0.20,
        edge=edge,
        ev=ev,
        size_usd=sz.size_usd,
        kelly_used=sz.kelly_used,
        reason="synthetic",
    )
    assert sig_id > 0

    # Execute → paper position
    opened = execute_pending_signals()
    assert len(opened) == 1
    positions = list_positions(status="OPEN")
    assert len(positions) == 1
    pos = positions[0]
    assert pos["city_code"] == "NYC"
    assert pos["side"] == "YES"
    assert pos["size_usd"] == sz.size_usd

    # Signal is marked acted
    with get_conn() as conn:
        row = conn.execute("SELECT acted FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert row["acted"] == 1
