"""Calibration tracking: record (p_model, outcome) and produce reliability bins.

Phase 1 keeps this minimal — store records, compute simple bin stats. Temperature
scaling / Platt comes later once we have meaningful sample size.
"""

from __future__ import annotations

from dataclasses import dataclass

from polyweather.db.connection import get_conn


@dataclass
class ReliabilityBin:
    lower: float
    upper: float
    n: int
    mean_pred: float
    fraction_positive: float


def record_outcome(
    *, market_id: int, bucket_id: int, p_model: float, outcome: bool
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO calibration_records (market_id, bucket_id, p_model, outcome)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, bucket_id, p_model, 1 if outcome else 0),
        )
        return int(cur.lastrowid)


def reliability_bins(
    n_bins: int = 10,
    min_p_market: float | None = None,
) -> list[ReliabilityBin]:
    """Compute reliability bins from settled positions.

    When ``min_p_market`` is set we only include records whose entry price
    (= the market's implied YES probability) is above the floor. This drops
    tail bets (e.g. p_market < 0.05) from the calibration view so the curve
    reflects realistic trading conditions instead of low-information lottery
    tickets.
    """
    bins: list[tuple[float, float, list[float], list[int]]] = []
    step = 1.0 / n_bins
    for i in range(n_bins):
        bins.append((i * step, (i + 1) * step, [], []))

    with get_conn() as conn:
        if min_p_market is not None:
            rows = conn.execute(
                """
                SELECT cr.p_model, cr.outcome
                  FROM calibration_records cr
                  JOIN paper_positions pp
                    ON pp.market_id = cr.market_id AND pp.bucket_id = cr.bucket_id
                 WHERE pp.entry_price >= ?
                """,
                (min_p_market,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT p_model, outcome FROM calibration_records"
            ).fetchall()

    for row in rows:
        p = float(row["p_model"])
        o = int(row["outcome"])
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx][2].append(p)
        bins[idx][3].append(o)

    out: list[ReliabilityBin] = []
    for lo, hi, preds, outs in bins:
        if not preds:
            out.append(ReliabilityBin(lo, hi, 0, 0.0, 0.0))
            continue
        out.append(
            ReliabilityBin(
                lower=lo,
                upper=hi,
                n=len(preds),
                mean_pred=sum(preds) / len(preds),
                fraction_positive=sum(outs) / len(outs),
            )
        )
    return out
