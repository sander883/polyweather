"""Settle open paper positions by re-reading each market's final prices.

Discovery strategy: weather events live under ``/events?tag_slug=weather``;
we fetch both the live set (``active=true&closed=false``) and the settled set
(``closed=true``), index every nested binary market by ``negRiskMarketID``,
then look up each open position's group via ``markets.condition_id`` (which
we set to ``negRiskMarketID`` at scan time).

A group is treated as resolved when exactly one binary's YES price is within
``_WIN_EPS`` of 1.0 and every other YES price is within ``_WIN_EPS`` of 0.0.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from polyweather.db.connection import get_conn
from polyweather.scanner.models import Event, Market
from polyweather.scanner.polymarket_client import GammaClient

log = logging.getLogger(__name__)

_WIN_EPS = 0.02
_WEATHER_TAGS = ("weather", "temperature", "climate")


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
    return m.negRiskMarketID or m.conditionId or str(m.id or "")


def _group_is_resolved(group: list[Market]) -> bool:
    yes_prices = [m.yes_price for m in group if m.yes_price is not None]
    if not yes_prices:
        return False
    near_one = sum(1 for p in yes_prices if p >= 1 - _WIN_EPS)
    near_zero = sum(1 for p in yes_prices if p <= _WIN_EPS)
    return near_one == 1 and near_one + near_zero == len(yes_prices)


async def _collect_groups(client: GammaClient) -> dict[str, list[Market]]:
    """Build a dict ``negRiskMarketID -> [binary markets]`` covering both live
    and settled weather events.
    """
    groups: dict[str, list[Market]] = {}
    seen_market_ids: set[str] = set()
    fetch_states = [
        {"active": True, "closed": False},
        {"active": False, "closed": True},
        {"active": True, "closed": True},
    ]
    for tag in _WEATHER_TAGS:
        for state in fetch_states:
            try:
                events: list[Event] = await client.list_events(
                    tag_slug=tag, limit=200, **state
                )
            except Exception as e:  # noqa: BLE001
                log.warning("list_events failed tag=%s state=%s: %s", tag, state, e)
                continue
            for ev in events:
                for m in ev.markets or []:
                    mid = str(m.id or m.conditionId or "")
                    if mid in seen_market_ids:
                        continue
                    seen_market_ids.add(mid)
                    groups.setdefault(_group_key(m), []).append(m)
    log.info("resolver: indexed %d groups across %d binaries",
             len(groups), len(seen_market_ids))
    return groups


async def settle_open_positions(*, only_past_settle: bool = True) -> dict:
    positions = _open_positions()
    if not positions:
        return {
            "checked": 0, "closed": 0, "still_open": 0,
            "total_pnl_usd": 0.0, "details": [],
        }

    now = datetime.now(timezone.utc)
    client = GammaClient()
    groups = await _collect_groups(client)

    closed_count = 0
    still_open = 0
    total_pnl = 0.0
    details: list[dict] = []

    for p in positions:
        group_id = p["condition_id"]   # this is the negRiskMarketID we stored
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

        group = groups.get(group_id)
        if not group:
            still_open += 1
            details.append({
                "position_id": p["id"], "city_code": p["city_code"],
                "outcome_label": p["outcome_label"],
                "skipped": "group not found in /events",
                "group_id": group_id,
            })
            continue

        # Find the binary in this group whose YES token matches our position
        binary = next(
            (m for m in group if m.yes_token_id == token_id), None
        )
        if binary is None:
            still_open += 1
            details.append({
                "position_id": p["id"], "city_code": p["city_code"],
                "outcome_label": p["outcome_label"],
                "skipped": "token_id not in group",
                "group_size": len(group),
            })
            continue

        # Two paths to "resolved":
        # 1) The whole group is settled (one near 1, rest near 0).
        # 2) Just this binary is closed=true with a near-0 / near-1 price —
        #    that's enough to settle our position even if the group fetch was
        #    incomplete (Polymarket pagination / archival can drop sibling
        #    binaries).
        binary_settled = (
            binary.closed
            and binary.yes_price is not None
            and (
                binary.yes_price <= _WIN_EPS
                or binary.yes_price >= 1 - _WIN_EPS
            )
        )
        if not (_group_is_resolved(group) or binary_settled):
            still_open += 1
            details.append({
                "position_id": p["id"], "city_code": p["city_code"],
                "outcome_label": p["outcome_label"],
                "skipped": "event not yet resolved",
                "current_yes_price": binary.yes_price,
                "group_size": len(group),
                "near_one": sum(
                    1 for x in group if (x.yes_price or 0) >= 1 - _WIN_EPS
                ),
                "is_closed_flag": binary.closed,
            })
            continue

        exit_price = binary.yes_price if binary.yes_price is not None else 0.0
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
