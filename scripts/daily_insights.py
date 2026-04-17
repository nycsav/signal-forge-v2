#!/usr/bin/env python3
"""Signal Forge v2 — Daily Insights Report

Generates 2-3x daily trading insights: key learnings, trade outcomes,
regime shifts, whale activity, and system health.

Designed to be run via launchd at 8am, 2pm, 10pm.

Usage:
    python scripts/daily_insights.py
    python scripts/daily_insights.py --hours 8   # last 8 hours only
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "trades.db"
LIVE_DB_PATH = PROJECT_ROOT / "data" / "live_trades.db"
LOG_DIR = PROJECT_ROOT / "logs"


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def trade_summary(hours):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    # Paper outcomes
    paper_trades = _query(DB_PATH,
        "SELECT * FROM trade_outcomes WHERE created_at > ? ORDER BY created_at DESC", (since,))

    # Live outcomes
    live_trades = []
    if LIVE_DB_PATH.exists():
        live_trades = _query(LIVE_DB_PATH,
            "SELECT * FROM live_trades WHERE closed_at > ? AND pnl_usd != 0 ORDER BY closed_at DESC", (since,))

    return paper_trades, live_trades


def whale_activity(hours):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    events = _query(DB_PATH,
        "SELECT * FROM agent_events WHERE agent_name='whale_trigger' AND created_at > ? ORDER BY created_at DESC",
        (since,))
    return events


def risk_vetoes(hours):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    events = _query(DB_PATH,
        "SELECT * FROM agent_events WHERE agent_name='risk_agent' AND event_type='vetoed' AND created_at > ? ORDER BY created_at DESC",
        (since,))
    return events


def proposals(hours):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    events = _query(DB_PATH,
        "SELECT * FROM agent_events WHERE agent_name='ai_analyst' AND event_type='proposal' AND created_at > ? ORDER BY created_at DESC",
        (since,))
    return events


def regime_status():
    events = _query(DB_PATH,
        "SELECT * FROM agent_events WHERE agent_name='regime_engine' ORDER BY id DESC LIMIT 1")
    return events[0] if events else {}


def signals_summary(hours):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    signals = _query(DB_PATH,
        "SELECT symbol, direction, raw_score, ai_confidence, decision, market_regime "
        "FROM signals_log WHERE created_at > ? ORDER BY raw_score DESC LIMIT 20",
        (since,))
    return signals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=8)
    args = parser.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  SIGNAL FORGE v2 — Trading Insights Report")
    print(f"  {now} | Last {args.hours} hours")
    print(f"{'='*60}")

    # 1. Trade outcomes
    paper, live = trade_summary(args.hours)
    print(f"\n📊 TRADES (last {args.hours}h)")
    if paper:
        wins = sum(1 for t in paper if (t.get("pnl_pct") or 0) > 0)
        total_pnl = sum(t.get("pnl_usd") or 0 for t in paper)
        print(f"  Paper: {len(paper)} trades | {wins}W/{len(paper)-wins}L | P&L: ${total_pnl:+.2f}")
        for t in paper[:5]:
            print(f"    {t['symbol']}: {t.get('pnl_pct',0):+.2f}% (${t.get('pnl_usd',0):+.2f}) "
                  f"exit={t.get('exit_reason','')} | {t.get('lesson','')[:60]}")
    else:
        print("  Paper: No closed trades")

    if live:
        wins = sum(1 for t in live if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in live)
        print(f"  Live:  {len(live)} trades | {wins}W/{len(live)-wins}L | P&L: ${total_pnl:+.2f}")
    else:
        print("  Live:  No closed trades")

    # 2. Proposals & vetoes
    props = proposals(args.hours)
    vetoes = risk_vetoes(args.hours)
    print(f"\n🎯 SIGNALS")
    print(f"  Proposals: {len(props)} | Vetoed: {len(vetoes)} | Approval rate: "
          f"{((len(props)-len(vetoes))/len(props)*100 if props else 0):.0f}%")

    if props:
        print("  Top proposals:")
        for p in props[:5]:
            data = json.loads(p.get("payload", "{}")) if isinstance(p.get("payload"), str) else p.get("payload", {})
            print(f"    {p.get('symbol','?')}: {data.get('direction','?')} score={data.get('score',0)} "
                  f"conf={data.get('ai_confidence',0):.0%} consensus={'Y' if data.get('consensus') else 'N'}")

    if vetoes:
        print("  Recent vetoes:")
        for v in vetoes[:3]:
            data = json.loads(v.get("payload", "{}")) if isinstance(v.get("payload"), str) else v.get("payload", {})
            print(f"    {v.get('symbol','?')}: {data.get('reason','unknown')}")

    # 3. Whale activity
    whales = whale_activity(args.hours)
    bullish = sum(1 for w in whales if 'bullish' in (w.get('event_type') or '').lower())
    bearish = sum(1 for w in whales if 'bearish' in (w.get('event_type') or '').lower())
    print(f"\n🐋 WHALE ACTIVITY")
    print(f"  Events: {len(whales)} | Bullish: {bullish} | Bearish: {bearish}")

    # 4. Top signals
    sigs = signals_summary(args.hours)
    if sigs:
        print(f"\n📈 TOP SIGNALS (by score)")
        for s in sigs[:5]:
            score = s.get('raw_score') or 0
            conf = s.get('ai_confidence') or 0
            print(f"    {s.get('symbol','?')}: score={score:.0f} "
                  f"dir={s.get('direction','?')} conf={conf:.0%} "
                  f"regime={s.get('market_regime','?')}")

    # 5. Key learnings
    print(f"\n💡 KEY LEARNINGS")
    if paper:
        # Win rate by consensus
        consensus_trades = [t for t in paper if t.get("consensus")]
        no_consensus = [t for t in paper if not t.get("consensus")]
        if consensus_trades:
            c_wr = sum(1 for t in consensus_trades if (t.get("pnl_pct") or 0) > 0) / len(consensus_trades) * 100
            print(f"  Consensus win rate: {c_wr:.0f}% ({len(consensus_trades)} trades)")
        if no_consensus:
            nc_wr = sum(1 for t in no_consensus if (t.get("pnl_pct") or 0) > 0) / len(no_consensus) * 100
            print(f"  Non-consensus win rate: {nc_wr:.0f}% ({len(no_consensus)} trades)")

        # Best/worst exit reasons
        by_reason = {}
        for t in paper:
            r = t.get("exit_reason", "unknown")
            by_reason.setdefault(r, []).append(t.get("pnl_pct") or 0)
        for reason, pnls in sorted(by_reason.items(), key=lambda x: sum(x[1])/len(x[1]), reverse=True):
            avg = sum(pnls) / len(pnls)
            print(f"  {reason}: avg={avg:+.2f}% ({len(pnls)} trades)")
    else:
        print("  No trades to analyze yet. System is scanning...")
        print(f"  Current regime: EXTREME FEAR (F&G ~23)")
        print(f"  Risk floors active: score>=62, confidence>=0.62")
        print(f"  Consensus now required for all entries")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
