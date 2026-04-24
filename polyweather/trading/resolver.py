"""Settle open paper positions by re-reading each market's final prices.

After a Polymarket market resolves, the winning YES token trades at 1.00 and
the losing ones at 0. We poll the market again via Gamma. For each open paper
position we hold, we look up the current ``outcomePrices[yes]`` of the binary
market that owns the position's ``bucket.token_id``.

Resolution rule:
    exit_price = current YES price (≈ 1.0 if bucket won, ≈ 0.0 if it lost)
    pnl_usd   = shares * exit_price  -  size_usd

We also record a ``calibration_records`` row per settled position so the
reliability curve stays fresh.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from polyweather.db.connection import get_conn
from polyweather.scanner.models import Event, Market
from polyweather.scanner.polymarket_client import GammaClient

log = logging.getLogger(__name__)

# Treat a YES price within this epsilon of 1.0 (or 0.0) as a final settlement.
# Before resolution some markets briefly trade at 0.99 / 0.01 — we still count
# them but flag the outcome as tentative.
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


async def _fetch_current_market(client: GammaClient, condition_id: str) -> Event | None:
    """Re-read the event that owns this group so we see refreshed prices."""
    events = await client.list_events(
        tag_slug=None, active=False, closed=False, archived=False, limit=200
    )
    # The same negRiskMarketID groups many condition_ids; search for any
    # event that has a market whose conditionId equals ours *or* whose
    # negRiskMarketID equals ours.
    for ev in events:
        for m in ev.markets or []:
            if m.conditionId == condition_id or m.negRiskMarketID == condition_id:
                return ev
    # Fallback: try with closed=true (market already resolved)
    events = await client.list_events(
        tag_slug=None, active=False, closed=True, archived=False, limit=200
    )
    for ev in events:
        for m in ev.markets or []:
            if m.conditionId == condition_id or m.negRiskMarketID == condition_id:
                return ev
    return None


def _current_yes_price(event: Event, token_id: str) -> float | None:
    for m in event.markets or []:
        if m.yes_token_id == token_id:
            return m.yes_price
    return None


def _market_is_resolved(event: Event) -> bool:
    """An event is fully resolved when exactly one bucket is near 1.0."""
    yes_prices = []
    for m in event.markets or []:
        if m.yes_price is not None:
            yes_prices.append(m.yes_price)
    if not yes_prices:
        return False
    near_one = sum(1 for p in yes_prices if p >= 1 - _WIN_EPS)
    near_zero = sum(1 for p in yes_prices if p <= _WIN_EPS)
    # Resolved: exactly one bucket near 1, rest near 0
    return near_one == 1 and near_one + near_zero == len(yes_prices)


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


async def settle_open_positions(*, only_past_settle: bool = True) -> dict:
    """Resolve all open paper positions whose underlying market has settled.

    Returns a summary dict with counts and PnL.
    """
    positions = _open_positions()
    if not positions:
        return {
            "checked": 0, "closed": 0, "still_open": 0,
            "total_pnl_usd": 0.0, "details": [],
        }

    now = datetime.now(timezone.utc)
    client = GammaClient()

    # Group positions by condition_id so we fetch each event once
    by_cond: dict[str, list[dict]] = {}
    for p in positions:
        by_cond.setdefault(p["condition_id"], []).append(p)

    closed_count = 0
    still_open = 0
    total_pnl = 0.0
    details: list[dict] = []

    for cond_id, group in by_cond.items():
        # Optional guard: skip positions whose settle time is still in the future
        if only_past_settle:
            settle_str = group[0].get("settle_time_utc")
            if settle_str:
                try:
                    settle_dt = datetime.fromisoformat(settle_str.replace("Z", "+00:00"))
                    if settle_dt > now:
                        still_open += len(group)
                        details.append({
                            "condition_id": cond_id,
                            "skipped": "settle_time in future",
                            "settle_time_utc": settle_str,
                            "positions": len(group),
                        })
                        continue
                except ValueError:
                    pass

        try:
            event = await _fetch_current_market(client, cond_id)
        except Exception as e:  # noqa: BLE001
            log.warning("resolver fetch failed for %s: %s", cond_id, e)
            still_open += len(group)
            details.append({"condition_id": cond_id, "error": str(e)})
            continue

        if event is None:
            still_open += len(group)
            details.append({"condition_id": cond_id, "skipped": "event not found"})
            continue

        resolved = _market_is_resolved(event)
        for p in group:
            yes_price = _current_yes_price(event, p["token_id"])
            if yes_price is None:
                still_open += 1
                details.append({
                    "position_id": p["id"], "skipped": "token not found in event",
                })
                continue
            if not resolved:
                still_open += 1
                details.append({
                    "position_id": p["id"],
                    "skipped": "event not fully resolved",
                    "current_yes_price": yes_price,
                })
                continue

            pnl = _close_position(
                position_id=p["id"],
                exit_price=yes_price,
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
                "exit_price": round(yes_price, 4),
                "pnl_usd": round(pnl, 2),
                "result": "WIN" if yes_price >= 1 - _WIN_EPS else "LOSS",
            })

    return {
        "checked": len(positions),
        "closed": closed_count,
        "still_open": still_open,
        "total_pnl_usd": round(total_pnl, 2),
        "details": details,
    }
