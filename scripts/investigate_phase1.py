"""Phase 1 root-cause investigation.

Run on the machine that has data/polyweather.db:
    python scripts/investigate_phase1.py

Outputs six diagnostics that pinpoint *where* the 27% win rate is coming from:
  1. Calibration accuracy   — is p_model honest?
  2. Edge vs win rate        — does higher edge actually win more often?
  3. p_model vs p_market gap — are we consistently overestimating ourselves?
  4. Per market_type         — high-temp vs low-temp performance
  5. Loser anatomy           — what do the 41 losers look like?
  6. Sample size adequacy    — can we conclude anything from n=56?
"""

from __future__ import annotations

import math
from polyweather.db.connection import get_conn


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def main() -> None:
    with get_conn() as conn:
        # ─── 1. Calibration: p_model bucket → actual win rate ─────────────
        banner("1. CALIBRATION ACCURACY (is p_model honest?)")
        print(f"{'p_model bin':<14} {'n':>4} {'predicted':>10} {'actual':>10} {'gap':>8}")
        print("-" * 50)
        rows = conn.execute("""
            SELECT
                CAST(p_model_at_entry * 10 AS INT) AS bin,
                COUNT(*) AS n,
                AVG(p_model_at_entry) AS pred,
                AVG(CASE WHEN pnl_usd > 0 THEN 1.0 ELSE 0.0 END) AS actual_win
              FROM paper_positions
             WHERE status = 'SETTLED'
             GROUP BY bin
             ORDER BY bin
        """).fetchall()
        for r in rows:
            lo = r["bin"] / 10
            hi = lo + 0.1
            gap = r["actual_win"] - r["pred"]
            flag = " ⚠️" if abs(gap) > 0.20 else ""
            print(f"{lo:.1f}-{hi:.1f}        {r['n']:>4} "
                  f"{r['pred']:>10.3f} {r['actual_win']:>10.3f} "
                  f"{gap:>+8.3f}{flag}")
        print("\n→ If 'gap' is consistently negative, model is OVERCONFIDENT.")
        print("→ If 'gap' is consistently positive, model is UNDERCONFIDENT.")

        # ─── 2. Edge vs win rate ──────────────────────────────────────────
        banner("2. EDGE vs ACTUAL WIN RATE (does edge predict outcomes?)")
        print(f"{'edge band':<15} {'n':>4} {'avg edge':>10} {'win rate':>10} {'avg pnl':>10}")
        print("-" * 55)
        rows = conn.execute("""
            SELECT
                CASE
                    WHEN s.edge < 0.10 THEN '0.08-0.10'
                    WHEN s.edge < 0.15 THEN '0.10-0.15'
                    WHEN s.edge < 0.25 THEN '0.15-0.25'
                    WHEN s.edge < 0.40 THEN '0.25-0.40'
                    ELSE '0.40+'
                END AS band,
                COUNT(*) AS n,
                AVG(s.edge) AS avg_edge,
                AVG(CASE WHEN pp.pnl_usd > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                AVG(pp.pnl_usd) AS avg_pnl
              FROM paper_positions pp
              JOIN signals s ON s.market_id = pp.market_id
                            AND s.bucket_id = pp.bucket_id
             WHERE pp.status = 'SETTLED'
             GROUP BY band
             ORDER BY avg_edge
        """).fetchall()
        for r in rows:
            print(f"{r['band']:<15} {r['n']:>4} {r['avg_edge']:>10.3f} "
                  f"{r['win_rate']:>10.1%} {r['avg_pnl']:>10.2f}")
        print("\n→ Win rate should INCREASE with edge. If flat/random, edge is noise.")

        # ─── 3. p_model vs p_market gap ───────────────────────────────────
        banner("3. p_model vs p_market (how aggressive is the model?)")
        rows = conn.execute("""
            SELECT
                AVG(p_model_at_entry) AS avg_pmodel,
                AVG(entry_price)      AS avg_entry,
                AVG(p_model_at_entry - entry_price) AS avg_gap,
                MIN(p_model_at_entry - entry_price) AS min_gap,
                MAX(p_model_at_entry - entry_price) AS max_gap
              FROM paper_positions
             WHERE status = 'SETTLED'
        """).fetchone()
        print(f"  avg p_model:        {rows['avg_pmodel']:.3f}")
        print(f"  avg entry_price:    {rows['avg_entry']:.3f}")
        print(f"  avg (model-market): {rows['avg_gap']:+.3f}")
        print(f"  range:              {rows['min_gap']:+.3f} … {rows['max_gap']:+.3f}")

        # ─── 4. Per market_type ───────────────────────────────────────────
        banner("4. PER MARKET TYPE (high vs low temp)")
        rows = conn.execute("""
            SELECT
                COALESCE(m.market_type, 'unknown') AS mtype,
                COUNT(*) AS n,
                SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                ROUND(AVG(pp.pnl_usd), 2) AS avg_pnl,
                ROUND(SUM(pp.pnl_usd), 2) AS total
              FROM paper_positions pp
              JOIN markets m ON m.id = pp.market_id
             WHERE pp.status = 'SETTLED'
             GROUP BY mtype
        """).fetchall()
        for r in rows:
            wr = r["wins"] / r["n"] if r["n"] else 0
            print(f"  {r['mtype']:<10} n={r['n']:<3} wins={r['wins']:<3} "
                  f"({wr:.1%}) avg=${r['avg_pnl']} total=${r['total']}")

        # ─── 5. Anatomy of losers ─────────────────────────────────────────
        banner("5. LOSER ANATOMY (what do the 41 losers have in common?)")
        rows = conn.execute("""
            SELECT
                AVG(p_model_at_entry) AS avg_pmodel,
                AVG(entry_price)      AS avg_entry,
                AVG(size_usd)         AS avg_size,
                COUNT(*)              AS n
              FROM paper_positions
             WHERE status = 'SETTLED' AND pnl_usd <= 0
        """).fetchone()
        winners = conn.execute("""
            SELECT
                AVG(p_model_at_entry) AS avg_pmodel,
                AVG(entry_price)      AS avg_entry,
                AVG(size_usd)         AS avg_size,
                COUNT(*)              AS n
              FROM paper_positions
             WHERE status = 'SETTLED' AND pnl_usd > 0
        """).fetchone()
        print(f"{'':<20} {'losers':>10} {'winners':>10}")
        print(f"  {'count':<18} {rows['n']:>10} {winners['n']:>10}")
        print(f"  {'avg p_model':<18} {rows['avg_pmodel']:>10.3f} {winners['avg_pmodel']:>10.3f}")
        print(f"  {'avg entry_price':<18} {rows['avg_entry']:>10.3f} {winners['avg_entry']:>10.3f}")
        print(f"  {'avg size_usd':<18} {rows['avg_size']:>10.2f} {winners['avg_size']:>10.2f}")
        print("\n→ If losers have similar p_model to winners, model can't separate them.")

        # ─── 6. Sample size adequacy ──────────────────────────────────────
        banner("6. STATISTICAL POWER (can we conclude anything from n=56?)")
        n = conn.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status='SETTLED'").fetchone()["n"]
        wins = conn.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status='SETTLED' AND pnl_usd > 0").fetchone()["n"]
        wr = wins / n if n else 0
        # 95% Wilson interval for proportion
        z = 1.96
        denom = 1 + z * z / n
        center = (wr + z * z / (2 * n)) / denom
        margin = z * math.sqrt(wr * (1 - wr) / n + z * z / (4 * n * n)) / denom
        lo = center - margin
        hi = center + margin
        print(f"  n = {n}, wins = {wins}")
        print(f"  observed win rate: {wr:.1%}")
        print(f"  95% confidence interval: [{lo:.1%}, {hi:.1%}]")
        print(f"  → True win rate is somewhere in this range, NOT a point estimate.")
        print(f"  → To halve the interval, you need ~{n*4} samples (~{n*4//12} more days at current rate).")


if __name__ == "__main__":
    main()
