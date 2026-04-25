"""Settle open paper positions by re-reading each market's final prices.

A Polymarket weather event is a negRisk group of binary markets. When the
event resolves, the winning bucket's YES token trades at ~1.0 and all losers
at ~0.0. We identify each open position's binary market by its CLOB token id
(stored at signal time in ``market_buckets.token_id``), batch-fetch their
current state via Gamma, group them by ``negRiskMarketID``, and:

- if the group is fully resolved (exactly one bucket near 1.0, rest near 0)
  → close every matching open paper position, write a calibration record;
- otherwise → leave positions OPEN and report the current YES price as a
  diagnostic so the caller can see what's happening.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from polyweather.db.connection import get_conn
from polyweather.scanner.models import Market
from polyweather.scanner.polymarket_client import GammaClient

log = logging.getLogger(__name__)

# A market is treated as "resolved" when one bucket is within this much of 1.0
# and every other bucket is within this much of 0.0.
_WIN_EPS = 0.02


def _open_positions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pp.id, pp.market_id, pp.bucket_id, pp.entry_price,
                   pp.size_usd, pp.shares, pp.p_model_at_entry,
                   mb.token_id, mb.outcome_label,
                   m.condition_id, m.city_code, m.settle_time_utc, m.question
              FROM paper_positions pp
              JOIN market_buckets mb ON mb.id = pp.bucket_id
              JOIN markets m ON m.id = pp.market_id
             WHERE pp.status = 'OPEN'
             ORDER BY pp.id ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _close_position(
    *,
    position_id: int,
    exit_price: float,
    shares: float,
    size_usd: float,
    bucket_id: int,
    market_id: int,
    p_model: float,
) -> float:
    pnl = shares * exit_price - size_usd
    won = exit_price >= 1 - _WIN_EPS
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE paper_positions
               SET status = 'SETTLED',
                   exit_price = ?,
                   pnl_usd = ?,
                   closed_at = datetime('now')
             WHERE id = ?
            """,
            (exit_price, pnl, position_id),
        )
        conn.execute(
            """
            INSERT INTO calibration_records
                (market_id, bucket_id, p_model, outcome)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, bucket_id, p_model, 1 if won else 0),
        )
    return pnl


def _group_key(m: Market) -> str:
    """Group binaries by negRiskMarketID; fall back to the market's own
    conditionId for non-negRisk events.
    """
    return m.negRiskMarketID or m.conditionId or str(m.id or "")


def _group_is_resolved(group: list[Market]) -> bool:
    yes_prices = [m.yes_price for m in group if m.yes_price is not None]
    if not yes_prices:
        return False
    near_one = sum(1 for p in yes_prices if p >= 1 - _WIN_EPS)
    near_zero = sum(1 for p in yes_prices if p <= _WIN_EPS)
    return near_one == 1 and near_one + near_zero == len(yes_prices)


async def settle_open_positions(*, only_past_settle: bool = True) -> dict:
    """Resolve all open paper positions whose underlying market has settled.

    Returns a summary dict with counts, PnL, and per-group diagnostics.
    """
    positions = _open_positions()
    if not positions:
        return {
            "checked": 0, "closed": 0, "still_open": 0,
            "total_pnl_usd": 0.0, "details": [],
        }

    now = datetime.now(timezone.utc)
    client = GammaClient()

    # Batch-fetch every binary market we have a position in by its token id
    token_ids = sorted({p["token_id"] for p in positions if p.get("token_id")})
    fetched = await client.get_markets_by_token_ids(token_ids)

    # Index fetched markets by token id (each Market has its YES token id)
    by_token: dict[str, Market] = {}
    for m in fetched:
        tid = m.yes_token_id
        if tid:
            by_token[tid] = m

    # Group fetched markets by negRiskMarketID (the logical event)
    by_group: dict[str, list[Market]] = {}
    for m in fetched:
        by_group.setdefault(_group_key(m), []).append(m)

    closed_count = 0
    still_open = 0
    total_pnl = 0.0
    details: list[dict] = []

    for p in positions:
        token_id = p["token_id"]
        # Optional guard: skip positions whose settle time is still in the future
        if only_past_settle and p.get("settle_time_utc"):
            try:
                settle_dt = datetime.fromisoformat(
                    str(p["settle_time_utc"]).replace("Z", "+00:00")
                )
                if settle_dt > now:
                    still_open += 1
                    details.append({
                        "position_id": p["id"], "city_code": p["city_code"],
                        "outcome_label": p["outcome_label"],
                        "skipped": "settle_time in future",
                        "settle_time_utc": p["settle_time_utc"],
                    })
                    continue
            except ValueError:
                pass

        m = by_token.get(token_id)
        if m is None:
            still_open += 1
            details.append({
                "position_id": p["id"], "city_code": p["city_code"],
                "outcome_label": p["outcome_label"],
                "skipped": "binary market not returned by Gamma",
                "token_id": token_id,
            })
            continue

        group_key = _group_key(m)
        group = by_group.get(group_key, [])
        resolved = _group_is_resolved(group)

        if not resolved:
            still_open += 1
            details.append({
                "position_id": p["id"], "city_code": p["city_code"],
                "outcome_label": p["outcome_label"],
                "skipped": "event not yet resolved",
                "current_yes_price": m.yes_price,
                "group_size": len(group),
                "near_one": sum(1 for x in group if (x.yes_price or 0) >= 1 - _WIN_EPS),
                "is_closed_flag": m.closed,
            })
            continue

        exit_price = m.yes_price if m.yes_price is not None else 0.0
        pnl = _close_position(
            position_id=p["id"],
            exit_price=exit_price,
            shares=p["shares"],
            size_usd=p["size_usd"],
            bucket_id=p["bucket_id"],
            market_id=p["market_id"],
            p_model=p["p_model_at_entry"],
        )
        total_pnl += pnl
        closed_count += 1
        details.append({
            "position_id": p["id"],
            "city_code": p["city_code"],
            "outcome_label": p["outcome_label"],
            "exit_price": round(exit_price, 4),
            "pnl_usd": round(pnl, 2),
            "result": "WIN" if exit_price >= 1 - _WIN_EPS else "LOSS",
        })

    return {
        "checked": len(positions),
        "closed": closed_count,
        "still_open": still_open,
        "total_pnl_usd": round(total_pnl, 2),
        "details": details,
    }
