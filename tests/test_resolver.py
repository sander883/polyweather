"""Resolver settles paper positions by re-reading final Polymarket prices.

We test the settlement math by directly driving the close + calibration path
(skipping the network fetch, which is tested separately via the Gamma client).
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


def test_close_position_records_win(isolated_db):
    from polyweather.db.connection import get_conn
    from polyweather.trading.resolver import _close_position

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO markets (condition_id, question, city_code, raw_json)
            VALUES ('0xM1', 'Highest temp in NYC', 'NYC', '{}')
            """,
        )
        market_id = conn.execute("SELECT id FROM markets").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO market_buckets (market_id, token_id, outcome_label, yes_price)
            VALUES (?, 'tok_yes', '70-74', 0.20)
            """,
            (market_id,),
        )
        bucket_id = conn.execute(
            "SELECT id FROM market_buckets WHERE market_id = ?", (market_id,)
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO paper_positions
                (market_id, bucket_id, side, entry_price, size_usd, shares,
                 p_model_at_entry, status)
            VALUES (?, ?, 'YES', 0.20, 15.0, 75.0, 0.70, 'OPEN')
            """,
            (market_id, bucket_id),
        )
        position_id = conn.execute(
            "SELECT id FROM paper_positions"
        ).fetchone()["id"]

    pnl = _close_position(
        position_id=position_id,
        exit_price=1.0,
        shares=75.0,
        size_usd=15.0,
        bucket_id=bucket_id,
        market_id=market_id,
        p_model=0.70,
    )

    # Winning YES at 1.0: shares * 1.0 - size_usd = 75 - 15 = 60
    assert pnl == 60.0

    with get_conn() as conn:
        pos = conn.execute(
            "SELECT status, exit_price, pnl_usd FROM paper_positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        cal = conn.execute(
            "SELECT p_model, outcome FROM calibration_records"
        ).fetchall()

    assert pos["status"] == "SETTLED"
    assert pos["exit_price"] == 1.0
    assert pos["pnl_usd"] == 60.0
    assert len(cal) == 1
    assert cal[0]["p_model"] == 0.70
    assert cal[0]["outcome"] == 1


def test_close_position_records_loss(isolated_db):
    from polyweather.db.connection import get_conn
    from polyweather.trading.resolver import _close_position

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, question, raw_json) VALUES ('0xM2','q','{}')"
        )
        market_id = conn.execute("SELECT id FROM markets").fetchone()["id"]
        conn.execute(
            "INSERT INTO market_buckets (market_id, token_id, yes_price, outcome_label) "
            "VALUES (?, 'tok_yes', 0.40, '70-74')",
            (market_id,),
        )
        bucket_id = conn.execute(
            "SELECT id FROM market_buckets WHERE market_id = ?", (market_id,)
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO paper_positions
                (market_id, bucket_id, side, entry_price, size_usd, shares,
                 p_model_at_entry, status)
            VALUES (?, ?, 'YES', 0.40, 20.0, 50.0, 0.55, 'OPEN')
            """,
            (market_id, bucket_id),
        )
        pos_id = conn.execute("SELECT id FROM paper_positions").fetchone()["id"]

    pnl = _close_position(
        position_id=pos_id, exit_price=0.0,
        shares=50.0, size_usd=20.0,
        bucket_id=bucket_id, market_id=market_id, p_model=0.55,
    )
    assert pnl == -20.0

    with get_conn() as conn:
        cal = conn.execute(
            "SELECT outcome FROM calibration_records"
        ).fetchone()
    assert cal["outcome"] == 0


def test_market_is_resolved_detection():
    from polyweather.scanner.models import Event, Market
    from polyweather.trading.resolver import _market_is_resolved

    def mk(yes: float) -> dict:
        return {
            "id": 1, "conditionId": f"0x{yes}",
            "outcomes": '["Yes","No"]',
            "outcomePrices": f'["{yes}", "{1-yes}"]',
            "clobTokenIds": '["a","b"]',
            "active": True, "closed": False, "acceptingOrders": True,
        }

    resolved = Event.model_validate({
        "id": "e", "title": "x", "active": True, "closed": False,
        "markets": [mk(0.0), mk(0.0), mk(1.0), mk(0.0)],
    })
    assert _market_is_resolved(resolved)

    in_flight = Event.model_validate({
        "id": "e", "title": "x", "active": True, "closed": False,
        "markets": [mk(0.1), mk(0.3), mk(0.4), mk(0.2)],
    })
    assert not _market_is_resolved(in_flight)
