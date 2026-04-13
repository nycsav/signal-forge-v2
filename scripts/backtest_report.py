#!/usr/bin/env python3
"""Signal Forge v2 — Backtest Report

Reads a backtest SQLite DB (schema: `trades` table with regime + score_threshold
columns) and prints the same summary that `historical_backtest.py` prints
at the end of a run. Useful for re-reporting without re-running the replay.

Usage:
    python scripts/backtest_report.py
    python scripts/backtest_report.py --db data/backtest_trades.db
    python scripts/backtest_report.py --db data/backtest_trades.db --regime capitulation
    python scripts/backtest_report.py --db data/backtest_trades.db --symbol BTC-USD
"""

import argparse
import math
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "backtest_trades.db"


def load_trades(db_path: Path, where_clauses: list[str], params: list) -> list[dict]:
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    rows = conn.execute(f"SELECT * FROM trades{where_sql}", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}

    pnls = [t["pnl_pct"] or 0 for t in trades]
    wins = [t for t in trades if (t["pnl_pct"] or 0) > 0]
    losses = [t for t in trades if (t["pnl_pct"] or 0) <= 0]

    total_pnl_usd = sum((t["pnl_usd"] or 0) for t in trades)
    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 0
    std = math.sqrt(var)
    sharpe = (mean / (std + 1e-9)) * math.sqrt(365 * 24) if std > 0 else 0.0

    downside = [p for p in pnls if p < 0]
    dstd = math.sqrt(sum(p * p for p in downside) / len(downside)) if downside else 0
    sortino = (mean / (dstd + 1e-9)) * math.sqrt(365 * 24) if dstd > 0 else 0.0

    gross_w = sum((t["pnl_usd"] or 0) for t in wins)
    gross_l = abs(sum((t["pnl_usd"] or 0) for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else float("inf")

    avg_win = sum((t["pnl_pct"] or 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum((t["pnl_pct"] or 0) for t in losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    regime_counts: dict[str, int] = {}
    for t in trades:
        r = t.get("regime") or "unknown"
        regime_counts[r] = regime_counts.get(r, 0) + 1
    top_regimes = sorted(regime_counts.items(), key=lambda kv: -kv[1])[:3]

    exit_counts: dict[str, int] = {}
    for t in trades:
        r = t.get("close_reason") or "unknown"
        exit_counts[r] = exit_counts.get(r, 0) + 1

    symbol_counts: dict[str, int] = {}
    for t in trades:
        symbol_counts[t["symbol"]] = symbol_counts.get(t["symbol"], 0) + 1

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl_usd": total_pnl_usd,
        "avg_win_pct": avg_win * 100,
        "avg_loss_pct": avg_loss * 100,
        "rr": rr,
        "sharpe": sharpe,
        "sortino": sortino,
        "profit_factor": pf,
        "top_regimes": top_regimes,
        "exit_breakdown": exit_counts,
        "symbols": sorted(symbol_counts.items(), key=lambda kv: -kv[1]),
    }


def print_block(label: str, s: dict):
    if s["trades"] == 0:
        print(f"  {label:>14} │ NO TRADES")
        return
    top3 = ", ".join(f"{r}({c})" for r, c in s["top_regimes"])
    print(
        f"  {label:>14} │ "
        f"n={s['trades']:<5} "
        f"win={s['win_rate']:5.1f}% "
        f"pnl=${s['total_pnl_usd']:+9.2f} "
        f"sharpe={s['sharpe']:5.2f} "
        f"sortino={s['sortino']:5.2f} "
        f"PF={s['profit_factor']:5.2f}"
    )
    print(f"                 │   R:R={s['rr']:4.2f}  avg_win={s['avg_win_pct']:+5.2f}%  avg_loss={s['avg_loss_pct']:+5.2f}%")
    print(f"                 │   top_regimes=[{top3}]")
    exits = " ".join(f"{k}={v}" for k, v in sorted(s["exit_breakdown"].items(), key=lambda kv: -kv[1]))
    print(f"                 │   exits: {exits}")


def main():
    p = argparse.ArgumentParser(description="Signal Forge v2 — Backtest Report")
    p.add_argument("--db", type=str, default=str(DEFAULT_DB))
    p.add_argument("--threshold", type=float, default=None,
                   help="Filter to single score_threshold (default: report all)")
    p.add_argument("--regime", type=str, default=None,
                   help="Filter to single regime (e.g. capitulation)")
    p.add_argument("--symbol", type=str, default=None,
                   help="Filter to single symbol (e.g. BTC-USD)")
    args = p.parse_args()

    db_path = Path(args.db)
    where: list[str] = []
    params: list = []
    if args.threshold is not None:
        where.append("score_threshold = ?")
        params.append(args.threshold)
    if args.regime is not None:
        where.append("regime = ?")
        params.append(args.regime)
    if args.symbol is not None:
        where.append("symbol = ?")
        params.append(args.symbol)

    print(f"Signal Forge v2 — Backtest Report")
    print(f"  db={db_path}")
    if where:
        print(f"  filters: {' AND '.join(where)} → {params}")
    print()

    all_trades = load_trades(db_path, where, params)
    if not all_trades:
        print("  No trades matched the filters.")
        return

    # Group by score_threshold if we have more than one
    thresholds = sorted({t["score_threshold"] for t in all_trades if t["score_threshold"] is not None})
    print(f"{'=' * 82}")
    print(f"  SUMMARY — {len(all_trades)} trades, {len(thresholds)} threshold(s)")
    print(f"{'=' * 82}")

    if len(thresholds) > 1:
        for thr in thresholds:
            bucket = [t for t in all_trades if t["score_threshold"] == thr]
            print_block(f"thr={thr:.0f}", summarize(bucket))
            print()
        print(f"  {'-' * 78}")
        print_block("ALL", summarize(all_trades))
    else:
        print_block("ALL", summarize(all_trades))

    # Per-symbol breakdown
    print()
    print(f"{'=' * 82}")
    print(f"  BY SYMBOL")
    print(f"{'=' * 82}")
    sym_counts: dict[str, list[dict]] = {}
    for t in all_trades:
        sym_counts.setdefault(t["symbol"], []).append(t)
    for sym, rows in sorted(sym_counts.items(), key=lambda kv: -len(kv[1]))[:20]:
        print_block(sym, summarize(rows))


if __name__ == "__main__":
    main()
