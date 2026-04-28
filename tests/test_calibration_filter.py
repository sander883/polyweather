"""Calibration filter: tail bets must be excludable from reliability bins."""

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


def _seed(market_id: int, bucket_id: int, p_model: float, outcome: int,
          entry_price: float) -> None:
    from polyweather.db.connection import get_conn
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_positions
                (market_id, bucket_id, side, entry_price, size_usd, shares,
                 p_model_at_entry, status, exit_price, pnl_usd)
            VALUES (?, ?, 'YES', ?, 15.0, 100.0, ?, 'SETTLED', ?, 0)
            """,
            (market_id, bucket_id, entry_price, p_model, 1.0 if outcome else 0.0),
        )
        conn.execute(
            """
            INSERT INTO calibration_records (market_id, bucket_id, p_model, outcome)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, bucket_id, p_model, outcome),
        )


def test_reliability_bins_filter_excludes_tail_bets(isolated_db):
    from polyweather.db.connection import get_conn
    from polyweather.model.calibration import reliability_bins

    with get_conn() as conn:
        for i in range(4):
            conn.execute(
                "INSERT INTO markets (condition_id, question, raw_json) "
                "VALUES (?, 'q', '{}')",
                (f"0x{i}",),
            )
            mid = conn.execute(
                "SELECT id FROM markets WHERE condition_id = ?", (f"0x{i}",),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO market_buckets (market_id, token_id, outcome_label, yes_price) "
                "VALUES (?, ?, '70-74', 0.5)",
                (mid, f"tok_{i}"),
            )

    # Buckets in DB: ids 1..4 paired with markets 1..4
    # Add records:
    #   - 2 tail bets (entry 0.001) labeled p_model=0.20 (bin 0.2-0.3)
    #   - 2 normal bets (entry 0.40) labeled p_model=0.45 (bin 0.4-0.5)
    _seed(1, 1, 0.20, 1, 0.001)   # tail win
    _seed(2, 2, 0.20, 0, 0.001)   # tail loss
    _seed(3, 3, 0.45, 1, 0.40)
    _seed(4, 4, 0.45, 0, 0.40)

    # No filter → 4 records counted
    bins_all = reliability_bins(n_bins=10, min_p_market=None)
    total_all = sum(b.n for b in bins_all)
    assert total_all == 4

    # Filter at 0.05 → tail bets dropped, only the 2 normal records survive
    bins_filtered = reliability_bins(n_bins=10, min_p_market=0.05)
    total_filtered = sum(b.n for b in bins_filtered)
    assert total_filtered == 2

    # The surviving bin should be 0.4-0.5 with 50% win rate
    target = next(b for b in bins_filtered if 0.4 <= b.lower < 0.5)
    assert target.n == 2
    assert target.fraction_positive == 0.5
