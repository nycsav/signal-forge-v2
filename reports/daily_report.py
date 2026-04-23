#!/usr/bin/env python3
"""
Signal Forge V2 — Daily Trade Report

Reads from data/trades.db, pulls all trades from the last 24 hours,
calculates win rate, total P&L, average hold time, and prints a
clean summary. Also writes to reports/YYYY-MM-DD.txt.

Run manually:  python reports/daily_report.py
Run via cron:   launchd at 7:00 AM ET daily
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
REPORTS_DIR = Path(__file__).parent


def generate_report(days: int = 1) -> str:
    now = datetime.now(ET)
    since = (now - timedelta(days=days)).isoformat()
    date_str = now.strftime("%Y-%m-%d")

    if not DB_PATH.exists():
        return f"ERROR: Database not found at {DB_PATH}"

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row

    # Closed trades in the window
    closed = conn.execute("""
        SELECT * FROM trades
        WHERE status='closed' AND close_reason != 'stale_cleanup'
        AND closed_at > ?
        ORDER BY closed_at DESC
    """, (since,)).fetchall()
    closed = [dict(r) for r in closed]

    # Open positions
    open_pos = conn.execute("""
        SELECT * FROM trades WHERE status='open'
        ORDER BY opened_at DESC
    """).fetchall()
    open_pos = [dict(r) for r in open_pos]

    # Account-level stats (all time)
    all_time = conn.execute("""
        SELECT count(*) as total,
               coalesce(sum(pnl_usd), 0) as total_pnl,
               coalesce(sum(case when pnl_usd > 0 then 1 else 0 end), 0) as wins
        FROM trades
        WHERE status='closed' AND close_reason != 'stale_cleanup'
    """).fetchone()

    conn.close()

    # Calculate metrics
    total = len(closed)
    wins = sum(1 for t in closed if (t.get("pnl_usd") or 0) > 0)
    losses = total - wins
    win_rate = (wins / total * 100) if total > 0 else 0
    net_pnl = sum(t.get("pnl_usd") or 0 for t in closed)
    avg_pnl = net_pnl / total if total > 0 else 0

    hold_times = [t.get("hold_time_hours") or 0 for t in closed if t.get("hold_time_hours")]
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    biggest_win = max((t.get("pnl_usd") or 0 for t in closed), default=0)
    biggest_loss = min((t.get("pnl_usd") or 0 for t in closed), default=0)

    # Exit reason breakdown
    exit_reasons = {}
    for t in closed:
        reason = t.get("close_reason") or "unknown"
        if reason not in exit_reasons:
            exit_reasons[reason] = {"count": 0, "pnl": 0}
        exit_reasons[reason]["count"] += 1
        exit_reasons[reason]["pnl"] += t.get("pnl_usd") or 0

    # Top symbols
    symbol_pnl = {}
    for t in closed:
        sym = t.get("symbol") or "?"
        symbol_pnl[sym] = symbol_pnl.get(sym, 0) + (t.get("pnl_usd") or 0)
    top_winners = sorted(symbol_pnl.items(), key=lambda x: x[1], reverse=True)[:3]
    top_losers = sorted(symbol_pnl.items(), key=lambda x: x[1])[:3]

    # Build report
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  SIGNAL FORGE V2 — DAILY REPORT")
    lines.append(f"  {date_str} | Last {days*24} hours")
    lines.append(f"{'='*60}")
    lines.append(f"")
    lines.append(f"  SUMMARY")
    lines.append(f"  {'─'*40}")
    lines.append(f"  Total trades:    {total}")
    lines.append(f"  Wins / Losses:   {wins} / {losses}")
    lines.append(f"  Win rate:        {win_rate:.1f}%")
    lines.append(f"  Net P&L:         ${net_pnl:+,.2f}")
    lines.append(f"  Avg P&L/trade:   ${avg_pnl:+,.2f}")
    lines.append(f"  Avg hold time:   {avg_hold:.1f} hours")
    lines.append(f"  Biggest winner:  ${biggest_win:+,.2f}")
    lines.append(f"  Biggest loser:   ${biggest_loss:+,.2f}")
    lines.append(f"")
    lines.append(f"  OPEN POSITIONS: {len(open_pos)}")
    for p in open_pos[:5]:
        lines.append(f"    {p.get('symbol', '?'):12} entry=${p.get('entry_price', 0):,.4f} size=${p.get('size_usd', 0):,.2f}")
    lines.append(f"")
    lines.append(f"  EXIT REASONS")
    lines.append(f"  {'─'*40}")
    for reason, data in sorted(exit_reasons.items(), key=lambda x: x[1]["count"], reverse=True):
        avg = data["pnl"] / data["count"] if data["count"] > 0 else 0
        lines.append(f"  {reason:20} {data['count']:3}x  total=${data['pnl']:+8.2f}  avg=${avg:+6.2f}")
    lines.append(f"")
    lines.append(f"  TOP WINNERS (by symbol)")
    lines.append(f"  {'─'*40}")
    for sym, pnl in top_winners:
        lines.append(f"  {sym:12} ${pnl:+,.2f}")
    lines.append(f"")
    lines.append(f"  TOP LOSERS (by symbol)")
    lines.append(f"  {'─'*40}")
    for sym, pnl in top_losers:
        lines.append(f"  {sym:12} ${pnl:+,.2f}")
    lines.append(f"")
    lines.append(f"  ALL-TIME: {dict(all_time)['total']} trades, ${dict(all_time)['total_pnl']:+,.2f} P&L, {dict(all_time)['wins']}/{dict(all_time)['total']} wins ({dict(all_time)['wins']/max(dict(all_time)['total'],1)*100:.0f}%)")
    lines.append(f"{'='*60}")

    return "\n".join(lines)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    report = generate_report(days)
    print(report)

    # Write to file
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    output_path = REPORTS_DIR / f"{date_str}.txt"
    output_path.write_text(report)
    print(f"\nSaved to {output_path}")
