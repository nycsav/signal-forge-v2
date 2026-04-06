#!/usr/bin/env python3
"""Signal Forge — Project Token Usage Tracker

Estimates Claude Code token usage for THIS project based on:
- Session transcript sizes (1KB ≈ 250 tokens)
- Subagent invocations (each ≈ 50K-150K tokens)
- Tool calls (each ≈ 500-2000 tokens overhead)

Also tracks Ollama local inference tokens.

Run: python scripts/usage_tracker.py
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
CLAUDE_PROJECT_DIR = Path.home() / ".claude" / "projects" / "-Users-sav"


def estimate_claude_usage() -> dict:
    """Estimate Claude Code token usage for this project."""

    # Session transcript data
    session_dir = CLAUDE_PROJECT_DIR / "543ca1b8-3b3c-46e7-9f86-e85fd38b0439"
    subagent_dir = session_dir / "subagents"
    tool_results_dir = session_dir / "tool-results"

    # Transcript sizes
    total_transcript_bytes = 0
    for p in CLAUDE_PROJECT_DIR.rglob("*"):
        if p.is_file():
            total_transcript_bytes += p.stat().st_size

    # Subagent count
    subagent_count = 0
    if subagent_dir.exists():
        subagent_count = len(list(subagent_dir.iterdir()))

    # Rough token estimates:
    # - Main conversation: ~4 tokens per word, ~5 words per line, ~20 chars per word
    # - 1 byte ≈ 0.25 tokens for mixed code/text
    # - Each subagent: 50K-150K tokens (100K average)
    # - This has been a very long session with heavy tool use

    transcript_tokens = total_transcript_bytes * 0.25
    subagent_tokens = subagent_count * 100_000

    # Estimate from conversation length (this is a 4-day session)
    # Based on typical Claude Code usage patterns:
    # - Each user message + response cycle: ~5K-20K tokens
    # - Heavy tool use sessions: ~50K-100K tokens per hour
    # - This project has been ~8-10 hours of active interaction

    # Conservative estimate based on session size
    estimated_total = transcript_tokens + subagent_tokens

    # Cost estimate (Claude Max subscription)
    # Max plan: $100/month for 5x usage vs Pro
    # Opus: ~$15/M input, ~$75/M output (API rates, not subscription)
    # On subscription: effectively ~$0.02-0.05 per 1K tokens amortized
    est_cost_if_api = estimated_total / 1_000_000 * 45  # blended $45/M rate

    return {
        "project": "Signal Forge v2",
        "session_id": "543ca1b8-3b3c-46e7-9f86-e85fd38b0439",
        "measured_at": datetime.now().isoformat(),
        "transcript_bytes": total_transcript_bytes,
        "subagent_sessions": subagent_count,
        "estimated_tokens": {
            "transcript": int(transcript_tokens),
            "subagents": int(subagent_tokens),
            "total_estimated": int(estimated_total),
        },
        "cost_estimates": {
            "if_api_pay_per_use": f"${est_cost_if_api:,.2f}",
            "actual_on_max_subscription": "Included in $100/mo Max plan",
            "note": "Claude Max is flat rate — no per-token charges. This is the amortized API-equivalent cost.",
        },
        "ollama_local": {
            "model_primary": "llama3.2:3b",
            "model_secondary": "deepseek-r1:14b",
            "est_tokens_per_scan": 17_100,
            "est_tokens_per_hour": 68_400,
            "est_tokens_per_day": 1_641_600,
            "est_tokens_4_days": 6_566_400,
            "cost": "$0.00 (local inference)",
        },
        "daily_breakdown": {
            "apr_3": {"claude_hours": 3, "est_tokens": 400_000, "ollama_tokens": 1_600_000, "trades": 18},
            "apr_4": {"claude_hours": 4, "est_tokens": 600_000, "ollama_tokens": 1_600_000, "trades": 1},
            "apr_5": {"claude_hours": 2, "est_tokens": 300_000, "ollama_tokens": 1_600_000, "trades": 10},
            "apr_6": {"claude_hours": 2, "est_tokens": 300_000, "ollama_tokens": 1_600_000, "trades": 9},
        },
        "totals": {
            "claude_estimated_tokens": int(estimated_total),
            "ollama_estimated_tokens": 6_566_400,
            "combined_tokens": int(estimated_total + 6_566_400),
            "total_cost_usd": 0.00,
            "note": "All local Ollama + Claude Max subscription = $0 marginal cost",
        },
    }


def get_ollama_stats_from_db() -> dict:
    """Pull Ollama usage from agent events in DB."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Count scan cycles (each = Ollama invocation)
        scans = conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_events WHERE agent_name='regime_engine'"
        ).fetchone()

        # Count AI proposals (each = 1 Ollama call)
        proposals = conn.execute(
            "SELECT COUNT(*) as cnt FROM signals_log WHERE decision='proposed'"
        ).fetchone()

        conn.close()

        scan_count = scans["cnt"] if scans else 0
        proposal_count = proposals["cnt"] if proposals else 0

        # Each proposal ≈ 900 tokens (prompt + response)
        ollama_tokens = proposal_count * 900

        return {
            "scan_cycles": scan_count,
            "ai_proposals": proposal_count,
            "estimated_ollama_tokens": ollama_tokens,
        }
    except Exception:
        return {"scan_cycles": 0, "ai_proposals": 0, "estimated_ollama_tokens": 0}


if __name__ == "__main__":
    usage = estimate_claude_usage()
    ollama = get_ollama_stats_from_db()
    usage["ollama_from_db"] = ollama

    print(json.dumps(usage, indent=2))
