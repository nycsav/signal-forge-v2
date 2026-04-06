"""Signal Forge v2 — Database Repository

All SQLite operations go through here. Thread-safe connection management.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from loguru import logger


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # ── Trades ──

    def insert_trade(self, **kwargs) -> int:
        conn = self._conn()
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        cursor = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        conn.commit()
        trade_id = cursor.lastrowid
        conn.close()
        return trade_id

    def update_trade(self, trade_id: int, **kwargs):
        conn = self._conn()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE trades SET {sets} WHERE id=?", [*kwargs.values(), trade_id])
        conn.commit()
        conn.close()

    def get_open_trades(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_closed_trades_since(self, since: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND closed_at > ? ORDER BY closed_at DESC",
            (since,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Signals ──

    def log_signal(self, **kwargs) -> int:
        conn = self._conn()
        if "score_breakdown" in kwargs and isinstance(kwargs["score_breakdown"], dict):
            kwargs["score_breakdown"] = json.dumps(kwargs["score_breakdown"])
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        cursor = conn.execute(
            f"INSERT INTO signals_log ({cols}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        conn.commit()
        sig_id = cursor.lastrowid
        conn.close()
        return sig_id

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM signals_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Position State ──

    def upsert_position(self, symbol: str, **kwargs):
        conn = self._conn()
        kwargs["symbol"] = symbol
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        updates = ", ".join(f"{k}=excluded.{k}" for k in kwargs if k != "symbol")
        conn.execute(
            f"INSERT INTO position_state ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {updates}",
            list(kwargs.values()),
        )
        conn.commit()
        conn.close()

    def get_all_positions(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM position_state ORDER BY opened_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def delete_position(self, symbol: str):
        conn = self._conn()
        conn.execute("DELETE FROM position_state WHERE symbol=?", (symbol,))
        conn.commit()
        conn.close()

    # ── Agent Events ──

    def log_event(self, agent_name: str, event_type: str, symbol: str = None, payload: dict = None):
        conn = self._conn()
        conn.execute(
            "INSERT INTO agent_events (timestamp, agent_name, event_type, symbol, payload) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), agent_name, event_type, symbol, json.dumps(payload) if payload else None),
        )
        conn.commit()
        conn.close()

    def get_recent_events(self, limit: int = 50) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Market Snapshots ──

    def save_snapshot(self, symbol: str, **kwargs):
        conn = self._conn()
        kwargs["symbol"] = symbol
        kwargs["timestamp"] = kwargs.get("timestamp", datetime.now().isoformat())
        if "data_json" in kwargs and isinstance(kwargs["data_json"], dict):
            kwargs["data_json"] = json.dumps(kwargs["data_json"])
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn.execute(f"INSERT INTO market_snapshots ({cols}) VALUES ({placeholders})", list(kwargs.values()))
        conn.commit()
        conn.close()

    # ── Scoring Weights ──

    def get_latest_weights(self) -> dict:
        conn = self._conn()
        row = conn.execute("SELECT weights FROM scoring_weights ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            return json.loads(row["weights"])
        # Default weights from spec
        return {
            "technical": 0.35,
            "sentiment": 0.15,
            "on_chain": 0.10,
            "ai_analyst": 0.40,
        }

    def save_weights(self, weights: dict, training_trades: int = 0, sharpe_improvement: float = 0):
        conn = self._conn()
        conn.execute(
            "INSERT INTO scoring_weights (timestamp, weights, training_window_trades, sharpe_improvement) VALUES (?,?,?,?)",
            (datetime.now().isoformat(), json.dumps(weights), training_trades, sharpe_improvement),
        )
        conn.commit()
        conn.close()

    # ── Performance Stats ──

    def get_performance_stats(self, days: int = 30) -> dict:
        conn = self._conn()
        from datetime import timedelta
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND closed_at > ?", (since,)
        ).fetchall()
        conn.close()

        trades = [dict(r) for r in rows]
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0}

        wins = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
        total_pnl = sum(t.get("pnl_usd") or 0 for t in trades)
        avg_pnl = sum(t.get("pnl_pct") or 0 for t in trades) / len(trades)

        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(trades) - len(wins),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "avg_pnl": avg_pnl,
            "total_pnl": total_pnl,
        }
