#!/usr/bin/env python3
"""Signal Forge v2 — Continuous Feedback Loop

Runs every hour. Analyzes recent trades, signals, and market data.
Logs learnings, detects patterns, and recommends parameter adjustments.

Designed to run via launchd hourly. Writes insights to:
- logs/feedback_loop.log (human-readable)
- data/trades.db agent_events table (machine-readable)

This is the system's "brain" — it learns from every scan cycle.
"""

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings

DB_PATH = settings.database_path
LOG_PATH = PROJECT_ROOT / "logs" / "feedback_loop.log"


def _query(sql, params=()):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _since(hours):
    """SQLite-compatible UTC timestamp for N hours ago."""
    # DB uses datetime('now') which is UTC
    from datetime import timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _log_event(agent, event_type, symbol, payload):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute(
        "INSERT INTO agent_events (timestamp, agent_name, event_type, symbol, payload) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), agent, event_type, symbol, json.dumps(payload))
    )
    conn.commit()
    conn.close()


def analyze_signal_quality(hours=1):
    """What signals are we generating and how good are they?"""
    since = _since(hours)
    signals = _query(
        "SELECT symbol, raw_score, direction, decision, ai_confidence, market_regime, fear_greed "
        "FROM signals_log WHERE created_at > ? ORDER BY raw_score DESC", (since,)
    )
    if not signals:
        return {"signals": 0}

    scores = [s["raw_score"] or 0 for s in signals]
    proposed = [s for s in signals if s["decision"] == "proposed"]
    high_score = [s for s in signals if (s["raw_score"] or 0) >= 62]

    return {
        "signals": len(signals),
        "avg_score": sum(scores) / len(scores),
        "max_score": max(scores),
        "proposed": len(proposed),
        "above_threshold": len(high_score),
        "top_symbols": [s["symbol"] for s in signals[:5]],
        "regime": signals[0].get("market_regime", "unknown") if signals else "unknown",
        "fear_greed": signals[0].get("fear_greed", 0) if signals else 0,
    }


def analyze_veto_patterns(hours=1):
    """Why are trades being vetoed? Find systematic blocks."""
    since = _since(hours)
    vetoes = _query(
        "SELECT symbol, payload FROM agent_events WHERE agent_name='risk_agent' "
        "AND event_type='vetoed' AND created_at > ?", (since,)
    )
    reasons = defaultdict(int)
    symbols = defaultdict(int)
    for v in vetoes:
        payload = json.loads(v.get("payload", "{}")) if isinstance(v.get("payload"), str) else {}
        reason = payload.get("reason", "unknown")
        reasons[reason] += 1
        symbols[v.get("symbol", "?")] += 1

    return {
        "total_vetoes": len(vetoes),
        "by_reason": dict(reasons),
        "by_symbol": dict(sorted(symbols.items(), key=lambda x: x[1], reverse=True)[:5]),
    }


def analyze_consensus_patterns(hours=4):
    """How often do models agree? Which symbols get consensus?"""
    since = _since(hours)
    # Check for NO CONSENSUS events
    events = _query(
        "SELECT symbol, payload FROM agent_events WHERE agent_name='ai_analyst' "
        "AND created_at > ?", (since,)
    )
    consensus_yes = 0
    consensus_no = 0
    for e in events:
        payload = json.loads(e.get("payload", "{}")) if isinstance(e.get("payload"), str) else {}
        if payload.get("consensus"):
            consensus_yes += 1
        elif "consensus" in payload:
            consensus_no += 1

    return {
        "consensus_yes": consensus_yes,
        "consensus_no": consensus_no,
        "consensus_rate": consensus_yes / max(consensus_yes + consensus_no, 1) * 100,
    }


def analyze_whale_signals(hours=4):
    """Are whale signals predicting price moves?"""
    since = _since(hours)
    whales = _query(
        "SELECT event_type, payload, created_at FROM agent_events "
        "WHERE agent_name='whale_trigger' AND created_at > ? ORDER BY id DESC", (since,)
    )
    bullish = sum(1 for w in whales if "bullish" in (w.get("event_type") or ""))
    bearish = sum(1 for w in whales if "bearish" in (w.get("event_type") or ""))

    return {
        "total": len(whales),
        "bullish": bullish,
        "bearish": bearish,
        "ratio": f"{bullish}:{bearish}",
        "signal": "accumulation" if bullish > bearish * 3 else "distribution" if bearish > bullish * 3 else "mixed",
    }


def analyze_model_performance(hours=4):
    """Track which AI model decisions led to good outcomes."""
    since = _since(hours)
    proposals = _query(
        "SELECT symbol, payload FROM agent_events WHERE agent_name='ai_analyst' "
        "AND event_type='proposal' AND created_at > ?", (since,)
    )
    return {
        "proposals": len(proposals),
        "symbols": list(set(
            p.get("symbol", "?") for p in proposals
        ))[:10],
    }


def generate_recommendations(signal_quality, vetoes, consensus, whales):
    """Based on analysis, generate actionable recommendations."""
    recs = []

    # Signal quality
    if signal_quality.get("avg_score", 0) < 50:
        recs.append("LOW_SIGNAL_QUALITY: Average score below 50 — market lacks setups")
    if signal_quality.get("above_threshold", 0) == 0:
        recs.append("NO_QUALIFYING_SIGNALS: Zero signals above threshold — correct in extreme fear")

    # Veto patterns
    if vetoes.get("total_vetoes", 0) > 0:
        top_reason = max(vetoes.get("by_reason", {}), key=vetoes["by_reason"].get, default="none")
        recs.append(f"TOP_VETO_REASON: {top_reason} ({vetoes['by_reason'].get(top_reason, 0)} times)")

    # Consensus
    rate = consensus.get("consensus_rate", 0)
    if rate < 20:
        recs.append(f"LOW_CONSENSUS: Only {rate:.0f}% agreement — models disagree on market direction")
    elif rate > 80:
        recs.append(f"HIGH_CONSENSUS: {rate:.0f}% agreement — strong directional conviction")

    # Whales
    whale_signal = whales.get("signal", "mixed")
    if whale_signal == "accumulation":
        recs.append(f"WHALE_ACCUMULATION: {whales['bullish']} bullish vs {whales['bearish']} bearish — smart money buying")
    elif whale_signal == "distribution":
        recs.append(f"WHALE_DISTRIBUTION: Bearish whale activity dominant — caution")

    return recs


def main():
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"  FEEDBACK LOOP — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Run all analyses
    signal_q = analyze_signal_quality(hours=1)
    vetoes = analyze_veto_patterns(hours=1)
    consensus = analyze_consensus_patterns(hours=4)
    whales = analyze_whale_signals(hours=4)
    model_perf = analyze_model_performance(hours=4)

    # Print results
    print(f"\n  SIGNALS (last 1h): {signal_q['signals']} scored | "
          f"avg={signal_q.get('avg_score',0):.0f} | max={signal_q.get('max_score',0):.0f} | "
          f"above threshold={signal_q.get('above_threshold',0)}")
    print(f"  REGIME: {signal_q.get('regime','?')} | F&G={signal_q.get('fear_greed',0)}")
    print(f"  TOP: {', '.join(signal_q.get('top_symbols', []))}")

    print(f"\n  VETOES (last 1h): {vetoes['total_vetoes']}")
    for reason, count in vetoes.get("by_reason", {}).items():
        print(f"    {reason}: {count}")

    print(f"\n  CONSENSUS (last 4h): {consensus['consensus_yes']}Y / {consensus['consensus_no']}N "
          f"({consensus['consensus_rate']:.0f}% agreement)")

    print(f"\n  WHALES (last 4h): {whales['total']} events | "
          f"{whales['bullish']} bull / {whales['bearish']} bear | Signal: {whales['signal']}")

    print(f"\n  MODEL PROPOSALS (last 4h): {model_perf['proposals']} proposals")

    # Generate recommendations
    recs = generate_recommendations(signal_q, vetoes, consensus, whales)
    print(f"\n  RECOMMENDATIONS:")
    for r in recs:
        print(f"    -> {r}")

    # Log to DB
    _log_event("feedback_loop", "hourly_analysis", None, {
        "signal_quality": signal_q,
        "vetoes": vetoes,
        "consensus": consensus,
        "whales": whales,
        "recommendations": recs,
        "timestamp": now.isoformat(),
    })

    # Write to log file
    with open(LOG_PATH, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"FEEDBACK LOOP — {now.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Signals: {signal_q['signals']} | Vetoes: {vetoes['total_vetoes']} | "
                f"Consensus: {consensus['consensus_rate']:.0f}% | Whales: {whales['signal']}\n")
        for r in recs:
            f.write(f"  {r}\n")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
