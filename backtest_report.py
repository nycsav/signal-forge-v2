"""
Signal Forge v2 — Backtest Report
===================================
Reads closed paper trades + signal log from data/trades.db and produces
a full performance report including:

  • Overall win rate, avg R:R, Sharpe ratio, max drawdown
  • Per-regime breakdown (capitulation / fear / neutral / greed / euphoria)
  • Per-symbol breakdown
  • Exit reason breakdown (tp1/tp2/tp3 vs stop vs time)
  • RiskAgent veto analysis (what’s being rejected and why)
  • altFINS shadow alignment (where altFINS agreed/disagreed with your signals)
  • Signal score distribution vs outcome
  • Hold time analysis
  • Best/worst trades

Run with:
    python backtest_report.py
    python backtest_report.py --db data/trades.db --min-trades 5
    python backtest_report.py --regime capitulation
    python backtest_report.py --days 30

Outputs to terminal + saves backtest_results/report_<timestamp>.txt
"""

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_DB   = os.path.join(os.path.dirname(__file__), "data", "trades.db")
OUTPUT_DIR   = os.path.join(os.path.dirname(__file__), "backtest_results")
SHADOW_DB    = os.path.join(os.path.dirname(__file__), "live_trades.db")

# Annualisation constant for Sharpe (crypto trades ~24/7/365)
TRADES_PER_YEAR = 365 * 24   # hourly slots


# ── DB helpers ────────────────────────────────────────────────────────────

def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(f"DB not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_closed_trades(conn, days: int | None, regime: str | None) -> list:
    query = """
        SELECT t.*, tf.outcome, tf.fear_greed
        FROM trades t
        LEFT JOIN trade_feedback tf ON tf.trade_id = t.id
        WHERE t.status = 'closed'
          AND t.pnl_pct IS NOT NULL
    """
    params = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query += " AND t.closed_at >= ?"
        params.append(since)
    if regime:
        # regime is stored in market_snapshots; join on nearest snapshot
        # Simplified: filter via signals_log market_regime
        query = query.replace(
            "WHERE t.status = 'closed'",
            """JOIN signals_log sl ON sl.symbol = t.symbol
               AND sl.timestamp <= t.opened_at
               AND sl.market_regime = ?
               WHERE t.status = 'closed'"""
        )
        params.insert(0, regime)
    query += " ORDER BY t.closed_at DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_signals(conn, days: int | None) -> list:
    query = "SELECT * FROM signals_log WHERE 1=1"
    params = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query += " AND timestamp >= ?"
        params.append(since)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_shadow(days: int | None) -> list:
    """Pull altFINS shadow data if available."""
    if not os.path.exists(SHADOW_DB):
        return []
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM altfins_shadow WHERE signal_direction IS NOT NULL"
    params = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query += " AND captured_at >= ?"
        params.append(since)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return rows


# ── Stat helpers ────────────────────────────────────────────────────────────

def mean(vals: list) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def stdev(vals: list) -> float:
    if len(vals) < 2:
        return 0.0
    m = mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def sharpe(returns: list) -> float:
    """Annualised Sharpe. Assumes each return is one trade."""
    if len(returns) < 2:
        return 0.0
    m = mean(returns)
    s = stdev(returns)
    if s == 0:
        return 0.0
    # Scale: sqrt of average trades per year
    scale = math.sqrt(TRADES_PER_YEAR / max(len(returns), 1))
    return (m / s) * scale


def max_drawdown(returns: list) -> float:
    """Maximum peak-to-trough drawdown on cumulative returns."""
    cumulative = []
    total = 0.0
    for r in returns:
        total += r
        cumulative.append(total)
    peak = -math.inf
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def win_rate(trades: list) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("pnl_pct") or 0) > 0)
    return wins / len(trades)


def avg_rr(trades: list) -> float:
    """Average realised R:R = avg_win / abs(avg_loss)."""
    wins  = [t["pnl_pct"] for t in trades if (t.get("pnl_pct") or 0) > 0]
    losses = [abs(t["pnl_pct"]) for t in trades if (t.get("pnl_pct") or 0) < 0]
    if not losses:
        return 0.0
    return mean(wins) / mean(losses) if wins else 0.0


def expectancy(trades: list) -> float:
    """Kelly expectancy: (win_rate * avg_win) - (loss_rate * avg_loss)."""
    if not trades:
        return 0.0
    wr = win_rate(trades)
    wins   = [t["pnl_pct"] for t in trades if (t.get("pnl_pct") or 0) > 0]
    losses = [abs(t["pnl_pct"]) for t in trades if (t.get("pnl_pct") or 0) < 0]
    avg_w = mean(wins) if wins else 0.0
    avg_l = mean(losses) if losses else 0.0
    return (wr * avg_w) - ((1 - wr) * avg_l)


# ── Report sections ───────────────────────────────────────────────────────────

def section(title: str) -> str:
    bar = "─" * 60
    return f"\n{bar}\n  {title}\n{bar}"


def fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%" if v else "n/a"


def build_report(trades: list, signals: list, shadow: list,
                 regime_filter: str | None, days: int | None) -> str:
    lines = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"Signal Forge v2 — Backtest Report  [{ts}]")
    if regime_filter:
        lines.append(f"Filter: regime={regime_filter}")
    if days:
        lines.append(f"Filter: last {days} days")
    lines.append(f"Closed trades analysed: {len(trades)}")

    if len(trades) < 3:
        lines.append("\n⚠  Fewer than 3 closed trades — results are not statistically meaningful.")
        lines.append("   Keep paper trading. Return when you have 20+ closed trades.")
        return "\n".join(lines)

    returns = [t.get("pnl_pct") or 0 for t in trades]

    # ─ Overall performance ─────────────────────────────────────────
    lines.append(section("OVERALL PERFORMANCE"))
    lines.append(f"  Trades        : {len(trades)}")
    lines.append(f"  Win rate      : {win_rate(trades)*100:.1f}%")
    lines.append(f"  Avg R:R       : {avg_rr(trades):.2f}")
    lines.append(f"  Expectancy    : {fmt_pct(expectancy(trades))} per trade")
    lines.append(f"  Sharpe        : {sharpe(returns):.2f}  (annualised)")
    lines.append(f"  Max drawdown  : {fmt_pct(max_drawdown(returns))}")
    total_pnl = sum(t.get("pnl_usd") or 0 for t in trades)
    lines.append(f"  Total P&L     : ${total_pnl:+.2f}")
    avg_hold = mean([t.get("hold_time_hours") or 0 for t in trades])
    lines.append(f"  Avg hold time : {avg_hold:.1f}h")

    # ─ Regime breakdown ──────────────────────────────────────────
    regime_map: dict[str, list] = {}
    for sig in signals:
        r = sig.get("market_regime") or "unknown"
        regime_map.setdefault(r, [])

    # Map trades to regime via signal score proximity
    sig_lookup: dict[str, str] = {}
    for s in signals:
        key = f"{s['symbol']}_{s['timestamp'][:16]}"
        sig_lookup[key] = s.get("market_regime", "unknown")

    regime_trades: dict[str, list] = {}
    for t in trades:
        # Best-effort: find the signal that spawned this trade
        regime = "unknown"
        for s in signals:
            if s["symbol"] == t["symbol"] and s["timestamp"] <= t["opened_at"]:
                regime = s.get("market_regime") or "unknown"
                break
        regime_trades.setdefault(regime, []).append(t)

    lines.append(section("PERFORMANCE BY REGIME"))
    for reg, rtrades in sorted(regime_trades.items()):
        wr = win_rate(rtrades) * 100
        rr = avg_rr(rtrades)
        exp = expectancy(rtrades) * 100
        lines.append(
            f"  {reg:<16} {len(rtrades):>3} trades  "
            f"WR={wr:.0f}%  R:R={rr:.2f}  exp={exp:+.2f}%"
        )

    # ─ Per-symbol ───────────────────────────────────────────────────
    sym_trades: dict[str, list] = {}
    for t in trades:
        sym_trades.setdefault(t["symbol"], []).append(t)

    lines.append(section("PERFORMANCE BY SYMBOL"))
    for sym, strades in sorted(sym_trades.items(), key=lambda x: -len(x[1])):
        wr = win_rate(strades) * 100
        pnl = sum(t.get("pnl_usd") or 0 for t in strades)
        lines.append(
            f"  {sym:<8} {len(strades):>3} trades  "
            f"WR={wr:.0f}%  P&L=${pnl:+.2f}"
        )

    # ─ Exit reason breakdown ───────────────────────────────────────
    exit_map: dict[str, list] = {}
    for t in trades:
        reason = t.get("close_reason") or "unknown"
        exit_map.setdefault(reason, []).append(t)

    lines.append(section("EXIT REASON BREAKDOWN"))
    for reason, etrades in sorted(exit_map.items(), key=lambda x: -len(x[1])):
        wr = win_rate(etrades) * 100
        avg_pnl = mean([t.get("pnl_pct") or 0 for t in etrades]) * 100
        lines.append(
            f"  {reason:<20} {len(etrades):>3} trades  "
            f"WR={wr:.0f}%  avg={avg_pnl:+.2f}%"
        )

    # ─ Signal score distribution ─────────────────────────────────
    lines.append(section("SIGNAL SCORE vs OUTCOME"))
    buckets = [(60,70), (70,80), (80,90), (90,101)]
    for lo, hi in buckets:
        bucket = [t for t in trades
                  if lo <= (t.get("signal_score") or 0) < hi]
        if not bucket:
            continue
        wr = win_rate(bucket) * 100
        avg_pnl = mean([t.get("pnl_pct") or 0 for t in bucket]) * 100
        lines.append(
            f"  Score {lo}-{hi}   {len(bucket):>3} trades  "
            f"WR={wr:.0f}%  avg={avg_pnl:+.2f}%"
        )

    # ─ RiskAgent veto analysis ──────────────────────────────────
    vetoed = [s for s in signals if s.get("decision") == "vetoed"]
    approved = [s for s in signals if s.get("decision") == "approved"]

    lines.append(section("RISKAGENT VETO ANALYSIS"))
    total_sig = len(signals)
    lines.append(f"  Total signals  : {total_sig}")
    lines.append(f"  Approved       : {len(approved)}  ({len(approved)/max(total_sig,1)*100:.0f}%)")
    lines.append(f"  Vetoed         : {len(vetoed)}  ({len(vetoed)/max(total_sig,1)*100:.0f}%)")

    veto_reasons: dict[str, int] = {}
    for s in vetoed:
        r = s.get("veto_reason") or "unknown"
        veto_reasons[r] = veto_reasons.get(r, 0) + 1
    if veto_reasons:
        lines.append("  Top veto reasons:")
        for reason, count in sorted(veto_reasons.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"    {count:>4}x  {reason}")

    # ─ altFINS shadow alignment ────────────────────────────────
    if shadow:
        lines.append(section("altFINS SHADOW ALIGNMENT"))
        lines.append(f"  Shadow rows available: {len(shadow)}")
        agree = 0
        disagree = 0
        for t in trades:
            direction = t.get("direction", "").upper()
            sym = t["symbol"]
            # Find nearest shadow row within 15min of trade open
            for s in shadow:
                if s["symbol"] != sym:
                    continue
                altfins_dir = (s.get("signal_direction") or "").upper()
                altfins_trend = (s.get("short_term_trend") or "").upper()
                if not altfins_dir and not altfins_trend:
                    continue
                # Loose alignment: bullish trend = long, bearish = short
                if (direction == "LONG" and ("UP" in altfins_trend or altfins_dir == "BULLISH")) or \
                   (direction == "SHORT" and ("DOWN" in altfins_trend or altfins_dir == "BEARISH")):
                    agree += 1
                else:
                    disagree += 1
                break
        total_cmp = agree + disagree
        if total_cmp > 0:
            lines.append(f"  Agree    : {agree}  ({agree/total_cmp*100:.0f}%)")
            lines.append(f"  Disagree : {disagree}  ({disagree/total_cmp*100:.0f}%)")
            lines.append("  Tip: if disagree > 30%, altFINS is catching signals your system misses.")
        else:
            lines.append("  Not enough overlapping data yet. Run for 24-48h first.")
    else:
        lines.append("\n  [altFINS shadow] No shadow data yet — run altfins_shadow.py for 24h first.")

    # ─ Best / worst trades ────────────────────────────────────────
    lines.append(section("TOP 5 BEST TRADES"))
    best = sorted(trades, key=lambda x: x.get("pnl_pct") or 0, reverse=True)[:5]
    for t in best:
        lines.append(
            f"  {t['symbol']:<8} {fmt_pct(t.get('pnl_pct'))}  "
            f"score={t.get('signal_score','?')}  "
            f"exit={t.get('close_reason','?')}  "
            f"{t.get('opened_at','')[:16]}"
        )

    lines.append(section("TOP 5 WORST TRADES"))
    worst = sorted(trades, key=lambda x: x.get("pnl_pct") or 0)[:5]
    for t in worst:
        lines.append(
            f"  {t['symbol']:<8} {fmt_pct(t.get('pnl_pct'))}  "
            f"score={t.get('signal_score','?')}  "
            f"exit={t.get('close_reason','?')}  "
            f"{t.get('opened_at','')[:16]}"
        )

    # ─ Recommendations ─────────────────────────────────────────────
    lines.append(section("AUTOMATED RECOMMENDATIONS"))
    wr = win_rate(trades)
    sr = sharpe(returns)
    veto_rate = len(vetoed) / max(total_sig, 1)

    if wr < 0.40:
        lines.append("  ⚠  Win rate below 40% — signal score floor may be too low.")
        lines.append("     Consider raising MIN_SIGNAL_SCORE_FLOOR from 62 to 65-68.")
    if wr > 0.65:
        lines.append("  ✅ Win rate above 65% — consider lowering score floor to increase frequency.")
    if sr < 0.5:
        lines.append("  ⚠  Sharpe below 0.5 — returns are too volatile relative to wins.")
        lines.append("     Review ATR stop multiplier (currently 2.5x — try 2.0x).")
    if sr > 1.5:
        lines.append("  ✅ Sharpe above 1.5 — strong risk-adjusted returns. Scale up position size.")
    if veto_rate > 0.70:
        lines.append(f"  ⚠  Veto rate {veto_rate*100:.0f}% — RiskAgent is rejecting most signals.")
        lines.append("     Check top veto reasons above and tune accordingly.")
    if avg_rr(trades) < 1.5:
        lines.append("  ⚠  R:R below 1.5 — exits happening too early or stops too tight.")
        lines.append("     Review TP1 (1.5R) — may need to widen to 2R.")

    # Hold time flag
    avg_h = mean([t.get("hold_time_hours") or 0 for t in trades])
    if avg_h > 48:
        lines.append("  ⚠  Avg hold > 48h — most exits are time-based, not target-based.")
        lines.append("     Signals may be correct direction but wrong timing. Review entry triggers.")

    lines.append("\n" + "─" * 60)
    lines.append("  Run again after 20+ closed trades for statistically valid results.")
    lines.append("─" * 60)

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Signal Forge v2 Backtest Report")
    parser.add_argument("--db",      default=DEFAULT_DB,  help="Path to trades.db")
    parser.add_argument("--days",    type=int, default=None, help="Only last N days")
    parser.add_argument("--regime",  default=None,
                        help="Filter by regime: capitulation|fear|neutral|greed|euphoria")
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum closed trades to run report (default 3)")
    args = parser.parse_args()

    print(f"Connecting to {args.db}...")
    conn = connect(args.db)

    trades  = fetch_closed_trades(conn, args.days, args.regime)
    signals = fetch_signals(conn, args.days)
    shadow  = fetch_shadow(args.days)
    conn.close()

    print(f"Found {len(trades)} closed trades, {len(signals)} signal rows, "
          f"{len(shadow)} altFINS shadow rows.")

    report = build_report(trades, signals, shadow, args.regime, args.days)
    print(report)

    # Save to file
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{OUTPUT_DIR}/report_{ts}.txt"
    if args.regime:
        fname = f"{OUTPUT_DIR}/report_{args.regime}_{ts}.txt"
    with open(fname, "w") as f:
        f.write(report)
    print(f"\nSaved to {fname}")


if __name__ == "__main__":
    main()
