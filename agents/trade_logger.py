"""Signal Forge v2 — Trade Logger

Records EVERY trade outcome for learning. No trade goes unrecorded.
Tracks: entry reason, exit reason, P&L, what signals were active,
what the AI said, what the market did after.

This is how the system learns — by reviewing its own mistakes and wins.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from loguru import logger
from config.settings import settings

DB_PATH = Path(settings.database_path)


def _get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT,
            entry_price REAL,
            exit_price REAL,
            entry_time TEXT,
            exit_time TEXT,
            pnl_pct REAL,
            pnl_usd REAL,
            hold_minutes REAL,
            exit_reason TEXT,

            -- What signals were active at entry
            fear_greed INTEGER,
            market_change_pct REAL,
            regime TEXT,
            rsi REAL,
            ai_confidence REAL,
            ai_direction TEXT,
            consensus INTEGER DEFAULT 0,
            fib_level TEXT,
            arkham_signal TEXT,
            cmc_volume_spike INTEGER DEFAULT 0,

            -- What we learned
            was_profitable INTEGER,
            lesson TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def log_trade_outcome(
    symbol: str, direction: str, entry_price: float, exit_price: float,
    entry_time: str, exit_time: str, pnl_pct: float, pnl_usd: float,
    hold_minutes: float, exit_reason: str,
    fear_greed: int = 0, market_change_pct: float = 0, regime: str = "",
    rsi: float = 0, ai_confidence: float = 0, ai_direction: str = "",
    consensus: bool = False, fib_level: str = "", arkham_signal: str = "",
    cmc_volume_spike: bool = False,
):
    """Record a trade outcome with full context for learning."""
    conn = _get_db()

    # Auto-generate lesson
    lesson = _generate_lesson(
        pnl_pct, exit_reason, market_change_pct, fear_greed,
        ai_confidence, consensus, hold_minutes
    )

    conn.execute("""
        INSERT INTO trade_outcomes (
            symbol, direction, entry_price, exit_price, entry_time, exit_time,
            pnl_pct, pnl_usd, hold_minutes, exit_reason,
            fear_greed, market_change_pct, regime, rsi, ai_confidence, ai_direction,
            consensus, fib_level, arkham_signal, cmc_volume_spike,
            was_profitable, lesson
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        symbol, direction, entry_price, exit_price, entry_time, exit_time,
        pnl_pct, pnl_usd, hold_minutes, exit_reason,
        fear_greed, market_change_pct, regime, rsi, ai_confidence, ai_direction,
        1 if consensus else 0, fib_level, arkham_signal, 1 if cmc_volume_spike else 0,
        1 if pnl_pct > 0 else 0, lesson,
    ))
    conn.commit()
    conn.close()

    logger.info(f"TRADE LOG: {symbol} {direction} P&L={pnl_pct:+.2f}% (${pnl_usd:+.2f}) reason={exit_reason} | {lesson}")


def _generate_lesson(pnl_pct, exit_reason, market_change, fg, confidence, consensus, hold_min):
    """Auto-generate a lesson from the trade outcome."""
    lessons = []

    if pnl_pct > 0:
        if hold_min < 30:
            lessons.append("Quick win — scalp worked")
        elif consensus:
            lessons.append("Consensus trade profitable — dual model confirmation works")
        if market_change > 2 and fg < 25:
            lessons.append("Fear+green entry was correct")
    else:
        if market_change > 3 and pnl_pct < -1:
            lessons.append("CHASED THE RALLY — entered after big move, bought the top")
        if hold_min < 15:
            lessons.append("Stopped out too fast — stop too tight or entry too late")
        if not consensus:
            lessons.append("Non-consensus trade lost — should have required agreement")
        if exit_reason == "stop_loss" and abs(pnl_pct) < 2:
            lessons.append("Tight stop triggered by noise — consider wider stop or better entry")

    return "; ".join(lessons) if lessons else "Standard trade"


def get_recent_outcomes(limit: int = 50) -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_win_rate_by_signal() -> dict:
    """Analyze which signals produce winning trades."""
    conn = _get_db()
    results = {}

    # By consensus
    for consensus_val, label in [(1, "consensus"), (0, "no_consensus")]:
        row = conn.execute("""
            SELECT COUNT(*) as total, SUM(was_profitable) as wins
            FROM trade_outcomes WHERE consensus=?
        """, (consensus_val,)).fetchone()
        if row and row["total"] > 0:
            results[label] = {"total": row["total"], "wins": row["wins"] or 0,
                              "win_rate": (row["wins"] or 0) / row["total"] * 100}

    # By regime
    for regime in ["bull_trend", "bear_trend", "ranging"]:
        row = conn.execute("""
            SELECT COUNT(*) as total, SUM(was_profitable) as wins
            FROM trade_outcomes WHERE regime=?
        """, (regime,)).fetchone()
        if row and row["total"] > 0:
            results[f"regime_{regime}"] = {"total": row["total"], "wins": row["wins"] or 0,
                                           "win_rate": (row["wins"] or 0) / row["total"] * 100}

    # By exit reason
    rows = conn.execute("""
        SELECT exit_reason, COUNT(*) as total, SUM(was_profitable) as wins, AVG(pnl_pct) as avg_pnl
        FROM trade_outcomes GROUP BY exit_reason
    """).fetchall()
    for r in rows:
        results[f"exit_{r['exit_reason']}"] = {
            "total": r["total"], "wins": r["wins"] or 0,
            "avg_pnl": r["avg_pnl"] or 0,
        }

    conn.close()
    return results
