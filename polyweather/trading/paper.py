"""Paper trading engine: convert unacted signals into paper positions.

Phase 1 assumes YES-side entry only, filled at the quoted mid-equivalent price.
Real slippage + order-book modeling comes in Phase 2.
"""

from __future__ import annotations

import logging

from polyweather.config import get_settings
from polyweather.db.connection import get_conn

log = logging.getLogger(__name__)


def _has_open_position(market_id: int, bucket_id: int) -> bool:
    """True if there's already an OPEN paper position on this exact bucket.

    We avoid stacking duplicate positions when /scan is triggered repeatedly
    (each scan emits a fresh signal row, but only one position should exist
    per market+bucket until it settles).
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM paper_positions
             WHERE market_id = ? AND bucket_id = ? AND status = 'OPEN'
             LIMIT 1
            """,
            (market_id, bucket_id),
        ).fetchone()
    return row is not None


def _city_exposure_today(city_code: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pp.size_usd), 0) AS total
              FROM paper_positions pp
              JOIN markets m ON m.id = pp.market_id
             WHERE m.city_code = ?
               AND pp.status = 'OPEN'
               AND date(pp.opened_at) = date('now')
            """,
            (city_code,),
        ).fetchone()
    return float(row["total"] or 0.0)


def _market_exposure(market_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(size_usd), 0) AS total
              FROM paper_positions
             WHERE market_id = ? AND status = 'OPEN'
            """,
            (market_id,),
        ).fetchone()
    return float(row["total"] or 0.0)


def open_paper_position(
    *,
    signal_id: int,
    market_id: int,
    bucket_id: int,
    city_code: str,
    entry_price: float,
    size_usd: float,
    p_model: float,
) -> int | None:
    s = get_settings()
    if _has_open_position(market_id, bucket_id):
        with get_conn() as conn:
            conn.execute("UPDATE signals SET acted = 1 WHERE id = ?", (signal_id,))
        log.info("skip signal %d: open position already exists on this bucket",
                 signal_id)
        return None

    in_market = _market_exposure(market_id)
    in_city = _city_exposure_today(city_code)

    if in_market + size_usd > s.max_pct_per_market * s.paper_bankroll:
        log.info("skip signal %d: per-market cap reached", signal_id)
        return None
    if in_city + size_usd > s.max_pct_per_city_day * s.paper_bankroll:
        log.info("skip signal %d: per-city/day cap reached", signal_id)
        return None

    # Apply slippage so paper PnL is closer to live: we always pay above the
    # displayed yes_price when crossing the spread on a small order book.
    # Capped at 0.999 to avoid division blow-ups on already-near-1 entries.
    effective_entry = min(entry_price * (1 + s.simulated_slippage_pct), 0.999)
    shares = size_usd / effective_entry if effective_entry > 0 else 0.0
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_positions
                (market_id, bucket_id, signal_id, side,
                 entry_price, size_usd, shares, p_model_at_entry, status)
            VALUES (?, ?, ?, 'YES', ?, ?, ?, ?, 'OPEN')
            """,
            (market_id, bucket_id, signal_id, effective_entry, size_usd, shares, p_model),
        )
        position_id = int(cur.lastrowid)
        conn.execute("UPDATE signals SET acted = 1 WHERE id = ?", (signal_id,))
    log.info(
        "paper open: pos=%d market=%d bucket=%d size=$%.2f @ %.4f"
        " (display=%.3f, slip=%.1f%%)",
        position_id, market_id, bucket_id, size_usd, effective_entry,
        entry_price, s.simulated_slippage_pct * 100,
    )
    return position_id


def execute_pending_signals() -> list[int]:
    """Take all signals with acted=0 and open paper positions where caps allow."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.market_id, s.bucket_id, s.p_model,
                   s.recommended_size_usd, mb.yes_price, m.city_code
              FROM signals s
              JOIN market_buckets mb ON mb.id = s.bucket_id
              JOIN markets m ON m.id = s.market_id
             WHERE s.acted = 0
             ORDER BY s.id ASC
            """
        ).fetchall()

    opened: list[int] = []
    for r in rows:
        pos_id = open_paper_position(
            signal_id=int(r["id"]),
            market_id=int(r["market_id"]),
            bucket_id=int(r["bucket_id"]),
            city_code=str(r["city_code"]),
            entry_price=float(r["yes_price"]),
            size_usd=float(r["recommended_size_usd"]),
            p_model=float(r["p_model"]),
        )
        if pos_id is not None:
            opened.append(pos_id)
    return opened


def list_positions(status: str | None = "OPEN") -> list[dict]:
    with get_conn() as conn:
        if status is None:
            rows = conn.execute(
                """
                SELECT pp.*, m.question, m.city_code, mb.outcome_label
                  FROM paper_positions pp
                  JOIN markets m ON m.id = pp.market_id
                  JOIN market_buckets mb ON mb.id = pp.bucket_id
                 ORDER BY pp.opened_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT pp.*, m.question, m.city_code, mb.outcome_label
                  FROM paper_positions pp
                  JOIN markets m ON m.id = pp.market_id
                  JOIN market_buckets mb ON mb.id = pp.bucket_id
                 WHERE pp.status = ?
                 ORDER BY pp.opened_at DESC
                """,
                (status,),
            ).fetchall()
    return [dict(r) for r in rows]


def bankroll_summary() -> dict:
    s = get_settings()
    with get_conn() as conn:
        open_row = conn.execute(
            "SELECT COALESCE(SUM(size_usd), 0) AS t FROM paper_positions WHERE status='OPEN'"
        ).fetchone()
        pnl_row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS t FROM paper_positions WHERE status != 'OPEN'"
        ).fetchone()
    open_exposure = float(open_row["t"] or 0.0)
    realized = float(pnl_row["t"] or 0.0)
    return {
        "starting_bankroll": s.paper_bankroll,
        "open_exposure_usd": open_exposure,
        "realized_pnl_usd": realized,
        "available_usd": round(s.paper_bankroll - open_exposure + realized, 2),
    }
