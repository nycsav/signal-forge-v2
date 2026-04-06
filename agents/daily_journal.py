"""Signal Forge v2 — Daily Journal

Tracks all changes, insights, and actions taken each day.
Persisted to SQLite so history survives restarts.
Feeds the dashboard /api/journal/daily endpoint.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from config.settings import settings

DB_PATH = Path(settings.database_path)


def _get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT,
            impact TEXT DEFAULT 'neutral',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_date ON daily_journal(date)")
    conn.commit()
    return conn


def add_entry(date: str, category: str, title: str, detail: str = "", impact: str = "neutral"):
    conn = _get_db()
    conn.execute(
        "INSERT INTO daily_journal (date, category, title, detail, impact) VALUES (?,?,?,?,?)",
        (date, category, title, detail, impact),
    )
    conn.commit()
    conn.close()


def get_entries(date: str = None, limit: int = 100) -> list[dict]:
    conn = _get_db()
    if date:
        rows = conn.execute(
            "SELECT * FROM daily_journal WHERE date=? ORDER BY id DESC LIMIT ?", (date, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daily_journal ORDER BY date DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_dates() -> list[str]:
    conn = _get_db()
    rows = conn.execute("SELECT DISTINCT date FROM daily_journal ORDER BY date DESC").fetchall()
    conn.close()
    return [r["date"] for r in rows]


def seed_history():
    """Seed the journal with the full history of what was built."""
    conn = _get_db()
    existing = conn.execute("SELECT COUNT(*) as cnt FROM daily_journal").fetchone()["cnt"]
    conn.close()
    if existing > 0:
        return  # Already seeded

    entries = [
        # Apr 3
        ("2026-04-03", "build", "Signal Forge v1 launched", "scanner_fix.py running every 15min with Llama 3.2 3B. Coinbase prices + altFINS signals + DeepSeek AI scoring.", "positive"),
        ("2026-04-03", "trade", "6 initial positions opened", "BTC, ETH, SOL, FIL, LINK, LTC — all via Alpaca paper trading. $2K each (hardcoded).", "positive"),
        ("2026-04-03", "build", "Dashboard v1 on port 8888", "FastAPI + HTML dashboard with prices, positions, signals, chat with DeepSeek.", "positive"),
        ("2026-04-03", "fix", "Consolidated codebase", "Removed duplicate src/ directory. Fixed scanner to use Half-Kelly sizing instead of hardcoded $2K.", "positive"),
        ("2026-04-03", "insight", "Fear & Greed at 9 — Extreme Fear", "Market in capitulation. All signals scored 30-50. Scanner correctly not over-trading.", "neutral"),

        # Apr 4
        ("2026-04-04", "build", "Signal Forge v2 — full spec implementation", "Built all 5 phases: Infrastructure, 9-agent scoring engine, execution, learning, Sonar client.", "positive"),
        ("2026-04-04", "build", "Event bus architecture", "Typed Pydantic events, asyncio pub/sub. MarketState → Technical → SignalBundle → AI Analyst → Risk → Execution.", "positive"),
        ("2026-04-04", "build", "7 GitHub repos analyzed", "Downloaded ai-hedge-fund-crypto, CryptoTradingAgents, talipp, AutoHedge, ai-fund, ai-hedge-fund, sibyl. Extracted 5-factor scoring + correlation matrix.", "positive"),
        ("2026-04-04", "trade", "First v2 AI trade: XRP at $1.31", "Full pipeline: Llama 3.2 proposed → Risk Agent approved (R:R passed) → Alpaca filled 1521.72 XRP.", "positive"),
        ("2026-04-04", "build", "Regime Adaptive Engine", "7 market regimes. CAPITULATION detected → threshold lowered 55→40, accumulate strategy, long_only bias.", "positive"),
        ("2026-04-04", "insight", "DeepSeek R1 returns empty on complex prompts", "Switched to Llama 3.2 3B as primary (fast, reliable), DeepSeek as fallback for high-conviction.", "negative"),

        # Apr 5
        ("2026-04-05", "trade", "10 new positions opened", "ARB, FIL, LTC, UNI, LINK, DOT, AVAX, ADA, XRP, SOL — accumulation batch in capitulation.", "positive"),
        ("2026-04-05", "bug", "Monitor Agent DB crashes", "NOT NULL constraint on position_state.direction + database locked errors. Monitor couldn't evaluate exits for ~12 hours.", "negative"),
        ("2026-04-05", "fix", "Monitor Agent rebuilt", "Reads from Alpaca directly, state in memory. No more DB dependency for exit evaluation.", "positive"),
        ("2026-04-05", "bug", "Risk Agent counting stale DB", "Reported 20 open positions (actual: 14). Blocked ALL new proposals for hours.", "negative"),
        ("2026-04-05", "insight", "98.7% veto rate", "Of 824 AI proposals, only 11 approved. Top veto reasons: bad R:R (AI sets stops too tight), max positions, sector correlation.", "neutral"),
        ("2026-04-05", "build", "Expanded watchlist to 50 coins", "Added MATIC, AAVE, RENDER, FET, TIA, SEI, STX, IMX, PEPE, WIF, BONK, FLOKI, etc. 11 sector groups.", "positive"),

        # Apr 6
        ("2026-04-06", "fix", "Risk Agent reads Alpaca positions", "Now queries Alpaca API directly instead of stale DB trades table. Position count correct (14, not 20).", "positive"),
        ("2026-04-06", "fix", "Hold times from Alpaca order timestamps", "Monitor reads filled_at from orders API. BTC showing 77h (correct), not 0h.", "positive"),
        ("2026-04-06", "trade", "6 positions closed — FIRST EXITS!", "BTC +$504, ETH +$269, FIL +$361, LINK +$222, LTC +$126, SOL +$251. All time_72h exits. Total realized: +$1,733.", "positive"),
        ("2026-04-06", "fix", "launchd auto-restart daemon", "com.signalforge.v2.plist — KeepAlive=true. Engine survives crashes and reboots.", "positive"),
        ("2026-04-06", "build", "System Auditor + dashboard audit panel", "15-component health check, spec compliance scoring (73%), priority fix identification.", "positive"),
        ("2026-04-06", "build", "Activity Reporter + token tracking", "Tracks Claude Code (~3.95M tokens), Ollama (~6.5M tokens), all API costs ($0).", "positive"),
        ("2026-04-06", "build", "Cloudflare Tunnel for remote access", "cloudflared tunnel → trycloudflare.com URL. Valid SSL, works on any device.", "positive"),
        ("2026-04-06", "insight", "100% win rate on 20 trades", "14 open (all green) + 6 closed (all profitable). Buying fear in capitulation working.", "positive"),
        ("2026-04-06", "insight", "Trailing stops active on 4 positions", "ADA +5.7%, AVAX +7.1%, DOT +5.7%, FIL +9.0% (before close) — all above 4.5% activation.", "positive"),
        ("2026-04-06", "insight", "AI stop-loss suggestions too tight", "Most R:R vetoes because Llama suggests stops 1-3% below entry. Needs ATR×2.5 override.", "negative"),
        ("2026-04-06", "build", "Daily Journal system", "Persistent SQLite journal tracking all changes, insights, and actions by date.", "positive"),
        ("2026-04-06", "trade", "CRV and DOGE entered", "New positions from v2 engine scan. CRV at $0.22, DOGE at $0.092.", "positive"),
    ]

    conn = _get_db()
    for date, cat, title, detail, impact in entries:
        conn.execute(
            "INSERT INTO daily_journal (date, category, title, detail, impact) VALUES (?,?,?,?,?)",
            (date, cat, title, detail, impact),
        )
    conn.commit()
    conn.close()


# Auto-seed on import
seed_history()
