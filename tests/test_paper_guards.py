"""Paper trading should not stack duplicate positions on the same bucket."""

from __future__ import annotations

import os
import tempfile

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


def _seed_signal(market_id: int, bucket_id: int, p_model: float = 0.6) -> int:
    from polyweather.db.connection import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (market_id, bucket_id, p_model, p_market, edge, ev,
                 recommended_size_usd, kelly_fraction_used, reason)
            VALUES (?, ?, ?, 0.30, 0.30, 1.0, 15.0, 0.05, 'test')
            """,
            (market_id, bucket_id, p_model),
        )
        return int(cur.lastrowid)


def test_duplicate_signal_is_skipped(isolated_db):
    from polyweather.db.connection import get_conn
    from polyweather.trading.paper import (
        execute_pending_signals,
        list_positions,
    )

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, question, city_code, raw_json) "
            "VALUES ('0xM','q','NYC','{}')"
        )
        market_id = conn.execute("SELECT id FROM markets").fetchone()["id"]
        conn.execute(
            "INSERT INTO market_buckets (market_id, token_id, outcome_label, yes_price) "
            "VALUES (?, 'tok','70-74',0.30)",
            (market_id,),
        )
        bucket_id = conn.execute(
            "SELECT id FROM market_buckets WHERE market_id = ?", (market_id,)
        ).fetchone()["id"]

    _seed_signal(market_id, bucket_id)
    opened_first = execute_pending_signals()
    assert len(opened_first) == 1
    assert len(list_positions(status="OPEN")) == 1

    # Second scan emits a fresh signal on the same (market, bucket) pair.
    _seed_signal(market_id, bucket_id)
    opened_second = execute_pending_signals()
    assert opened_second == []                              # nothing new opened
    assert len(list_positions(status="OPEN")) == 1          # still one position

    # The second signal must have been marked acted=1 so it doesn't keep
    # being re-considered on every subsequent scan.
    from polyweather.db.connection import get_conn as _gc
    with _gc() as conn:
        rows = conn.execute("SELECT acted FROM signals ORDER BY id").fetchall()
    assert [r["acted"] for r in rows] == [1, 1]


def test_slippage_widens_entry_price(isolated_db):
    """Paper open should record an entry above the displayed yes_price."""
    from polyweather.db.connection import get_conn
    from polyweather.trading.paper import open_paper_position

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, question, city_code, raw_json) "
            "VALUES ('0xS','q','NYC','{}')"
        )
        market_id = conn.execute("SELECT id FROM markets").fetchone()["id"]
        conn.execute(
            "INSERT INTO market_buckets (market_id, token_id, outcome_label, yes_price) "
            "VALUES (?, 'tok', '70-74', 0.40)",
            (market_id,),
        )
        bucket_id = conn.execute("SELECT id FROM market_buckets").fetchone()["id"]
        cur = conn.execute(
            """
            INSERT INTO signals (market_id, bucket_id, p_model, p_market, edge, ev,
                                 recommended_size_usd, kelly_fraction_used)
            VALUES (?, ?, 0.6, 0.4, 0.2, 0.5, 15.0, 0.05)
            """,
            (market_id, bucket_id),
        )
        signal_id = int(cur.lastrowid)

    pos_id = open_paper_position(
        signal_id=signal_id,
        market_id=market_id,
        bucket_id=bucket_id,
        city_code="NYC",
        entry_price=0.40,    # displayed yes_price
        size_usd=15.0,
        p_model=0.6,
    )
    assert pos_id is not None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT entry_price, shares FROM paper_positions WHERE id = ?",
            (pos_id,),
        ).fetchone()

    # 5% slippage: 0.40 → 0.42, shares = 15 / 0.42 ≈ 35.71
    assert abs(row["entry_price"] - 0.42) < 1e-9
    assert abs(row["shares"] - (15.0 / 0.42)) < 1e-6
