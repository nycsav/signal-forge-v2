#!/usr/bin/env python3
"""Signal Forge v2 — Nightly DeepSeek R1 Analysis

Runs at 2am via launchd. Reads last 30 closed trades, sends them to
DeepSeek R1 14B for structured analysis. Outputs JSON report suggesting
which signal components are over/underweighted.

Saves to: logs/deepseek_nightly.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from db.repository import Repository
from config.settings import settings

REPORT_PATH = Path(__file__).parent.parent / "logs" / "deepseek_nightly.json"


def load_recent_trades(limit: int = 30) -> list[dict]:
    repo = Repository(settings.database_path)
    return repo.get_recent_trades(limit)


def load_trade_outcomes(limit: int = 30) -> list[dict]:
    """Also pull from trade_outcomes if available."""
    import sqlite3
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def build_prompt(trades: list, outcomes: list) -> str:
    """Build a structured prompt for DeepSeek R1."""
    # Summarize trades
    if not trades and not outcomes:
        return ""

    trade_summaries = []
    for t in (outcomes or trades)[:30]:
        pnl = t.get("pnl_pct") or t.get("pnl_usd", 0)
        won = "WIN" if (t.get("was_profitable") or (pnl and pnl > 0)) else "LOSS"
        trade_summaries.append(
            f"{t.get('symbol','?')} {t.get('direction','?')} P&L={pnl:+.2f}{'%' if 'pnl_pct' in t else '$'} "
            f"exit={t.get('exit_reason') or t.get('close_reason','?')} "
            f"hold={t.get('hold_minutes',0)/60 if t.get('hold_minutes') else t.get('hold_time_hours',0):.0f}h "
            f"conf={t.get('ai_confidence',0)} consensus={t.get('consensus',0)} "
            f"regime={t.get('regime','?')} f&g={t.get('fear_greed',0)} "
            f"→ {won}"
        )

    trades_text = "\n".join(trade_summaries[:30])

    wins = sum(1 for t in (outcomes or trades) if t.get("was_profitable") or (t.get("pnl_pct") or t.get("pnl_usd", 0)) > 0)
    losses = len(outcomes or trades) - wins
    win_rate = wins / len(outcomes or trades) * 100 if (outcomes or trades) else 0

    return f"""You are a quantitative trading analyst reviewing the last 30 closed trades.

TRADE HISTORY ({len(outcomes or trades)} trades, {wins} wins, {losses} losses, {win_rate:.0f}% win rate):
{trades_text}

CURRENT SIGNAL WEIGHTS:
- technical: 35% (RSI, MACD, BB, EMA, volume)
- sentiment: 15% (Fear & Greed, news)
- on_chain: 10% (whale flows, exchange flows)
- ai_analyst: 40% (Qwen3/Llama AI confidence)

Analyze these trades and return ONLY this JSON:
{{
  "analysis": {{
    "overall_assessment": "one paragraph summary of what's working and what isn't",
    "win_rate": {win_rate:.1f},
    "biggest_problem": "the single biggest issue causing losses",
    "biggest_strength": "the single biggest driver of wins"
  }},
  "weight_recommendations": {{
    "technical": {{"current": 0.35, "suggested": <float>, "reason": "why"}},
    "sentiment": {{"current": 0.15, "suggested": <float>, "reason": "why"}},
    "on_chain": {{"current": 0.10, "suggested": <float>, "reason": "why"}},
    "ai_analyst": {{"current": 0.40, "suggested": <float>, "reason": "why"}}
  }},
  "entry_recommendations": [
    "specific recommendation 1",
    "specific recommendation 2",
    "specific recommendation 3"
  ],
  "exit_recommendations": [
    "specific recommendation 1",
    "specific recommendation 2"
  ],
  "risk_recommendations": [
    "specific recommendation 1",
    "specific recommendation 2"
  ]
}}"""


def call_deepseek(prompt: str) -> dict:
    """Call DeepSeek R1 14B via Ollama."""
    try:
        r = httpx.post(
            f"{settings.ollama_host}/api/generate",
            json={
                "model": "deepseek-r1:14b",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 4000},
            },
            timeout=300,  # 5 min timeout for deep analysis
        )
        if r.status_code == 200:
            response = r.json().get("response", "")
            # Strip thinking tags
            import re
            response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()

            # Extract JSON
            matches = re.findall(r"\{[\s\S]*\}", response)
            for m in matches:
                try:
                    parsed = json.loads(m)
                    if "analysis" in parsed or "weight_recommendations" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue

            return {"error": "Failed to parse DeepSeek response", "raw": response[:500]}
    except Exception as e:
        return {"error": str(e)}


def main():
    print(f"[{datetime.now().isoformat()}] Nightly DeepSeek analysis starting...")

    trades = load_recent_trades(30)
    outcomes = load_trade_outcomes(30)
    print(f"  Loaded {len(trades)} trades, {len(outcomes)} outcomes")

    prompt = build_prompt(trades, outcomes)
    if not prompt:
        report = {"error": "No trades to analyze", "timestamp": datetime.now().isoformat()}
    else:
        print(f"  Calling DeepSeek R1 14B (this takes 2-5 minutes)...")
        report = call_deepseek(prompt)
        report["timestamp"] = datetime.now().isoformat()
        report["trades_analyzed"] = len(outcomes or trades)

    # Save report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"  Report saved to {REPORT_PATH}")

    # Print summary
    if "analysis" in report:
        a = report["analysis"]
        print(f"\n  Assessment: {a.get('overall_assessment', '?')[:200]}")
        print(f"  Biggest problem: {a.get('biggest_problem', '?')}")
        print(f"  Biggest strength: {a.get('biggest_strength', '?')}")
    if "weight_recommendations" in report:
        print(f"\n  Weight suggestions:")
        for comp, rec in report["weight_recommendations"].items():
            print(f"    {comp}: {rec.get('current',0):.0%} → {rec.get('suggested',0):.0%} ({rec.get('reason','')})")

    print(f"\n[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    main()
