"""
altFINS Shadow Signal Logger
============================
Runs ALONGSIDE your existing system — never touches live.py or main.py.

What it does every hour:
  1. Connects to altFINS MCP server
  2. Pulls screener data (trend + RSI) for your watchlist
  3. Pulls bullish/bearish signals feed for your watchlist
  4. Saves everything to altfins_shadow table in your existing SQLite DB

Purpose: compare altFINS pre-computed signals vs your internal signals
         to decide whether to migrate or keep your own signal engine.

Run with:
    source venv/bin/activate
    python altfins_shadow.py

Stop with: Ctrl+C
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ALTFINS_API_KEY = os.getenv("ALTFINS_API_KEY", "")
ALTFINS_MCP_URL = "https://mcp.altfins.com/mcp"

# Same watchlist as your main system — edit to match yours
WATCHLIST = ["BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "LINK", "DOT"]

# How often to poll (seconds) — 3600 = 1 hour
POLL_INTERVAL_SECONDS = 3600

# Your existing SQLite DB path
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "signals.db")
# Fallback: try live_trades.db if signals.db doesn't exist
if not os.path.exists(DB_PATH):
    DB_PATH = os.path.join(os.path.dirname(__file__), "live_trades.db")

# ── Database setup ────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS altfins_shadow (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    -- Screener fields
    short_term_trend TEXT,
    medium_term_trend TEXT,
    long_term_trend  TEXT,
    rsi_14           REAL,
    price            REAL,
    market_cap       REAL,
    price_change_1d  REAL,
    -- Signals feed fields
    signal_key       TEXT,
    signal_name      TEXT,
    signal_direction TEXT,
    signal_timestamp TEXT,
    -- Raw response for debugging
    screener_raw     TEXT,
    signals_raw      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shadow_symbol
    ON altfins_shadow(symbol, captured_at);
CREATE INDEX IF NOT EXISTS idx_shadow_direction
    ON altfins_shadow(signal_direction, captured_at);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    print(f"[DB] Connected to {path}")
    return conn


def save_screener_row(conn: sqlite3.Connection, captured_at: str,
                      symbol: str, data: dict, raw: str):
    conn.execute("""
        INSERT INTO altfins_shadow
            (captured_at, symbol,
             short_term_trend, medium_term_trend, long_term_trend,
             rsi_14, price, market_cap, price_change_1d,
             screener_raw)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        captured_at, symbol,
        data.get("SHORT_TERM_TREND"),
        data.get("MEDIUM_TERM_TREND"),
        data.get("LONG_TERM_TREND"),
        data.get("RSI14"),
        data.get("PRICE"),
        data.get("MARKET_CAP"),
        data.get("PRICE_CHANGE_1D"),
        raw,
    ))
    conn.commit()


def save_signal_row(conn: sqlite3.Connection, captured_at: str,
                    symbol: str, signal: dict, raw: str):
    conn.execute("""
        INSERT INTO altfins_shadow
            (captured_at, symbol,
             signal_key, signal_name, signal_direction, signal_timestamp,
             signals_raw)
        VALUES (?,?,?,?,?,?,?)
    """, (
        captured_at, symbol,
        signal.get("signalKey"),
        signal.get("signalName"),
        signal.get("direction"),
        signal.get("timestamp"),
        raw,
    ))
    conn.commit()


# ── altFINS MCP calls ─────────────────────────────────────────────────────────

async def fetch_screener(session: ClientSession, symbols: list[str]) -> dict:
    """Pull trend + RSI data for all watchlist symbols in one call."""
    result = await session.call_tool(
        "screener_getAltfinsScreenerData",
        arguments={
            "coins": symbols,
            "displayTypes": [
                "SHORT_TERM_TREND",
                "MEDIUM_TERM_TREND",
                "LONG_TERM_TREND",
                "RSI14",
                "PRICE",
                "MARKET_CAP",
                "PRICE_CHANGE_1D",
            ],
            "coinTypeFilter": "REGULAR",
            "size": len(symbols),
        },
    )
    raw = "\n".join(
        item.text for item in result.content if hasattr(item, "text")
    )
    # Parse the text response into a dict keyed by symbol
    parsed: dict[str, dict] = {}
    try:
        for item in result.content:
            if hasattr(item, "text"):
                data = json.loads(item.text)
                if isinstance(data, list):
                    for row in data:
                        sym = row.get("symbol", "")
                        parsed[sym] = row.get("additionalData", {})
                elif isinstance(data, dict):
                    sym = data.get("symbol", "")
                    parsed[sym] = data.get("additionalData", {})
    except Exception:
        pass
    return {"parsed": parsed, "raw": raw}


async def fetch_signals(session: ClientSession, symbols: list[str]) -> dict:
    """Pull bullish + bearish signals for watchlist from the last 24h."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)

    results = []
    raw_parts = []

    for direction in ["BULLISH", "BEARISH"]:
        result = await session.call_tool(
            "signals_getSignalsFeed",
            arguments={
                "coins": symbols,
                "direction": direction,
                "fromDate": yesterday.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "toDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": 50,
            },
        )
        raw = "\n".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        raw_parts.append(raw)
        try:
            for item in result.content:
                if hasattr(item, "text"):
                    data = json.loads(item.text)
                    content = data.get("content", data if isinstance(data, list) else [])
                    results.extend(content)
        except Exception:
            pass

    return {"signals": results, "raw": "\n---\n".join(raw_parts)}


# ── Main poll loop ────────────────────────────────────────────────────────────

async def poll_once(conn: sqlite3.Connection):
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"X-Api-Key": ALTFINS_API_KEY}

    print(f"\n[{captured_at}] Polling altFINS MCP...")

    async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── Screener ──────────────────────────────────────────────────────
            try:
                screener = await fetch_screener(session, WATCHLIST)
                parsed = screener["parsed"]
                raw = screener["raw"]

                saved = 0
                for symbol in WATCHLIST:
                    data = parsed.get(symbol, {})
                    save_screener_row(conn, captured_at, symbol, data, raw)
                    saved += 1

                    trend = data.get("SHORT_TERM_TREND", "n/a")
                    rsi = data.get("RSI14", "n/a")
                    print(f"  [SCREENER] {symbol}: trend={trend}  RSI={rsi}")

                print(f"  ✓ Screener: {saved} symbols saved")
            except Exception as e:
                print(f"  ✗ Screener error: {e}")

            # ── Signals feed ──────────────────────────────────────────────────
            try:
                signals_data = await fetch_signals(session, WATCHLIST)
                signals = signals_data["signals"]
                raw = signals_data["raw"]

                saved = 0
                for sig in signals:
                    symbol = sig.get("symbol", "UNKNOWN")
                    save_signal_row(conn, captured_at, symbol, sig, raw)
                    saved += 1
                    print(f"  [SIGNAL]  {symbol}: "
                          f"{sig.get('signalName','?')} "
                          f"({sig.get('direction','?')})")

                if not signals:
                    print("  [SIGNAL]  No new signals in last 24h")
                else:
                    print(f"  ✓ Signals: {saved} entries saved")
            except Exception as e:
                print(f"  ✗ Signals error: {e}")

    print(f"[{captured_at}] Poll complete. Next in {POLL_INTERVAL_SECONDS//60} min.")


async def main():
    if not ALTFINS_API_KEY:
        print("ERROR: ALTFINS_API_KEY not set in .env")
        return

    print("=" * 60)
    print("altFINS Shadow Signal Logger")
    print(f"Watchlist : {', '.join(WATCHLIST)}")
    print(f"DB        : {DB_PATH}")
    print(f"Interval  : every {POLL_INTERVAL_SECONDS // 60} minutes")
    print(f"MCP URL   : {ALTFINS_MCP_URL}")
    print("=" * 60)
    print("Running alongside your live system — no trades, no risk.")
    print("Stop with Ctrl+C\n")

    conn = init_db(DB_PATH)

    # Run immediately on start, then every hour
    while True:
        try:
            await poll_once(conn)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] Poll failed: {e} — retrying next cycle")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
