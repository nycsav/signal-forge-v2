"""Signal Forge v2 — Live Trading Repository

Separate database for real money trades. Clean accounting.
"""

import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from loguru import logger

LIVE_DB_PATH = Path(__file__).parent.parent / "data" / "live_trades.db"
LIVE_SCHEMA = Path(__file__).parent / "live_schema.sql"


class LiveRepository:
    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or LIVE_DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = self._conn()
        conn.executescript(LIVE_SCHEMA.read_text())
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # ── Trades ──

    def open_trade(self, **kwargs) -> str:
        conn = self._conn()
        trade_id = kwargs.get("trade_id", f"live_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        kwargs["trade_id"] = trade_id
        kwargs.setdefault("status", "open")
        kwargs.setdefault("opened_at", datetime.now().isoformat())
        cols = ", ".join(kwargs.keys())
        vals = ", ".join("?" for _ in kwargs)
        conn.execute(f"INSERT OR REPLACE INTO live_trades ({cols}) VALUES ({vals})", list(kwargs.values()))
        conn.commit()
        conn.close()
        self.log("trade_opened", f"Opened {kwargs.get('symbol')} {kwargs.get('side')} ${kwargs.get('size_usd',0):.2f}", trade_id)
        return trade_id

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str, fee_usd: float = 0):
        conn = self._conn()
        trade = conn.execute("SELECT * FROM live_trades WHERE trade_id=?", (trade_id,)).fetchone()
        if not trade:
            conn.close()
            return
        trade = dict(trade)
        entry = trade["entry_price"]
        qty = trade["quantity"]
        pnl_usd = (exit_price - entry) * qty if trade["side"] == "buy" else (entry - exit_price) * qty
        pnl_pct = (exit_price - entry) / entry if entry > 0 else 0
        pnl_after_fees = pnl_usd - fee_usd - (trade.get("fee_usd") or 0)
        hold_min = 0
        try:
            opened = datetime.fromisoformat(trade["opened_at"])
            hold_min = (datetime.now() - opened).total_seconds() / 60
        except Exception:
            pass

        conn.execute("""UPDATE live_trades SET
            exit_price=?, pnl_usd=?, pnl_pct=?, pnl_after_fees=?, fee_usd=COALESCE(fee_usd,0)+?,
            exit_reason=?, hold_minutes=?, status='closed', closed_at=?
            WHERE trade_id=?""",
            (exit_price, pnl_usd, pnl_pct, pnl_after_fees, fee_usd, exit_reason, hold_min, datetime.now().isoformat(), trade_id))
        conn.commit()
        conn.close()
        self.log("trade_closed", f"Closed {trade['symbol']} {exit_reason} P&L=${pnl_after_fees:+.2f} ({pnl_pct*100:+.2f}%)", trade_id)

    def get_open_trades(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM live_trades WHERE status='open'").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_all_trades(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM live_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_closed_trades(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM live_trades WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Daily P&L ──

    def snapshot_daily(self, balance: float, realized_pnl: float = 0, unrealized_pnl: float = 0,
                        fees: float = 0, opened: int = 0, closed: int = 0, wins: int = 0, losses: int = 0):
        today = date.today().isoformat()
        win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        conn = self._conn()
        conn.execute("""INSERT OR REPLACE INTO live_daily_pnl
            (date, starting_balance, ending_balance, realized_pnl, unrealized_pnl, total_fees,
             trades_opened, trades_closed, wins, losses, win_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (today, balance - realized_pnl, balance, realized_pnl, unrealized_pnl, fees, opened, closed, wins, losses, win_rate))
        conn.commit()
        conn.close()

    def get_daily_history(self, limit: int = 30) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM live_daily_pnl ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_total_pnl(self) -> dict:
        conn = self._conn()
        row = conn.execute("""SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_after_fees > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_after_fees <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(pnl_after_fees), 0) as total_pnl,
            COALESCE(SUM(fee_usd), 0) as total_fees,
            COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
            FROM live_trades WHERE status='closed'""").fetchone()
        conn.close()
        r = dict(row)
        r["win_rate"] = r["wins"] / r["total_trades"] * 100 if r["total_trades"] > 0 else 0
        return r

    # ── Halt Check ──

    def check_daily_halt(self, daily_limit_usd: float) -> tuple[bool, str]:
        today = date.today().isoformat()
        conn = self._conn()
        row = conn.execute("""SELECT COALESCE(SUM(pnl_after_fees), 0) as daily_pnl
            FROM live_trades WHERE status='closed' AND DATE(closed_at)=?""", (today,)).fetchone()
        conn.close()
        daily_pnl = row["daily_pnl"] if row else 0
        if daily_pnl <= -daily_limit_usd:
            return True, f"Daily loss limit: ${daily_pnl:.2f} <= -${daily_limit_usd:.2f}"
        return False, ""

    # ── Journal ──

    def log(self, category: str, message: str, trade_id: str = None, data: dict = None):
        conn = self._conn()
        conn.execute("INSERT INTO live_journal (timestamp, category, message, trade_id, data_json) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), category, message, trade_id, json.dumps(data) if data else None))
        conn.commit()
        conn.close()

    def get_journal(self, limit: int = 50) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM live_journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
