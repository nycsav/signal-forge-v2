"""
altFINS Shadow Signal Logger
============================
Runs ALONGSIDE your existing system — never touches live.py or main.py.

What it does every hour:
  1. Connects to altFINS MCP server
  2. Pulls screener data (trend + RSI) for your watchlist in ONE call
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

# Poll every 60 minutes — keeps us well within rate limits
POLL_INTERVAL_SECONDS = 600

# DB path — uses live_trades.db which already exists in your project
DB_PATH = os.path.join(os.path.dirname(__file__), "live_trades.db")

# ── Correct tool names (discovered via altfins_discover.py) ───────────────
SCREENER_TOOL = "screener_getAltfinsScreenerData"
SIGNALS_TOOL  = "signal_feed_data"   # fixed: was signals_getSignalsFeed

# ── Database setup ────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS altfins_shadow (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at       TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    short_term_trend  TEXT,
    medium_term_trend TEXT,
    long_term_trend   TEXT,
    rsi_14            REAL,
    price             REAL,
    market_cap        REAL,
    price_change_1d   REAL,
    signal_key        TEXT,
    signal_name       TEXT,
    signal_direction  TEXT,
    signal_timestamp  TEXT,
    screener_raw      TEXT,
    signals_raw       TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
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


def save_screener_row(conn, captured_at, symbol, data, raw):
    conn.execute("""
        INSERT INTO altfins_shadow
            (captured_at, symbol,
             short_term_trend, medium_term_trend, long_term_trend,
             rsi_14, price, market_cap, price_change_1d, screener_raw)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        captured_at, symbol,
        data.get("SHORT_TERM_TREND") or data.get("shortTermTrend"),
        data.get("MEDIUM_TERM_TREND") or data.get("mediumTermTrend"),
        data.get("LONG_TERM_TREND") or data.get("longTermTrend"),
        data.get("RSI14") or data.get("rsi14"),
        data.get("PRICE") or data.get("price"),
        data.get("MARKET_CAP") or data.get("marketCap"),
        data.get("PRICE_CHANGE_1D") or data.get("priceChange1d"),
        raw[:4000],  # cap raw storage
    ))
    conn.commit()


def save_signal_row(conn, captured_at, symbol, signal, raw):
    conn.execute("""
        INSERT INTO altfins_shadow
            (captured_at, symbol,
             signal_key, signal_name, signal_direction,
             signal_timestamp, signals_raw)
        VALUES (?,?,?,?,?,?,?)
    """, (
        captured_at, symbol,
        signal.get("signalKey") or signal.get("signal_key"),
        signal.get("signalName") or signal.get("signal_name") or signal.get("name"),
        signal.get("direction"),
        signal.get("timestamp") or signal.get("date"),
        raw[:4000],
    ))
    conn.commit()


# ── altFINS MCP calls ─────────────────────────────────────────────────────────

def parse_mcp_response(result) -> tuple[list, str]:
    """Extract list of items and raw text from any MCP tool result."""
    items = []
    raw_parts = []

    for item in result.content:
        if not hasattr(item, 'text'):
            continue
        raw_parts.append(item.text)
        text = item.text.strip()

        # Rate limit check
        if "429" in text or "TOO_MANY_REQUESTS" in text or "Rate limit" in text:
            raise Exception(f"Rate limited by altFINS: {text[:100]}")

        # Try JSON parse
        try:
            data = json.loads(text)
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                # Paginated response: {content: [...], ...}
                if "content" in data and isinstance(data["content"], list):
                    items.extend(data["content"])
                # Single item response
                elif "symbol" in data or "signalKey" in data:
                    items.append(data)
                # Nested under another key
                else:
                    for v in data.values():
                        if isinstance(v, list):
                            items.extend(v)
                            break
        except json.JSONDecodeError:
            # Plain text response — store as-is for debugging
            pass

    return items, "\n".join(raw_parts)


async def fetch_screener(session: ClientSession) -> tuple[dict, str]:
    """ONE call for all watchlist symbols — avoids rate limits."""
    result = await session.call_tool(
        SCREENER_TOOL,
        arguments={
            "coins": WATCHLIST,
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
            "size": len(WATCHLIST),
        },
    )

    rows, raw = parse_mcp_response(result)

    # Build dict keyed by symbol
    by_symbol: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol", "")
        if not sym:
            continue
        # additionalData holds the display fields
        extra = row.get("additionalData") or row.get("additional_data") or row
        by_symbol[sym] = extra

    return by_symbol, raw


async def fetch_signals(session: ClientSession) -> tuple[list, str]:
    """Pull last 24h signals for entire watchlist in two calls (bull + bear)."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)
    from_str = yesterday.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_signals = []
    all_raw = []

    for direction in ["BULLISH", "BEARISH"]:
        result = await session.call_tool(
            SIGNALS_TOOL,
            arguments={
                "coins": WATCHLIST,
                "direction": direction,
                "fromDate": from_str,
                "toDate": to_str,
                "size": 50,
            },
        )
        sigs, raw = parse_mcp_response(result)
        all_signals.extend(sigs)
        all_raw.append(raw)

    return all_signals, "\n---\n".join(all_raw)


# ── Main poll loop ────────────────────────────────────────────────────────────

async def poll_once(conn: sqlite3.Connection):
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"X-Api-Key": ALTFINS_API_KEY}
    print(f"\n[{captured_at}] Polling altFINS...")

    async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ─ Screener (1 call for all 8 symbols) ──────────────────────────
            try:
                by_symbol, raw = await fetch_screener(session)

                for symbol in WATCHLIST:
                    data = by_symbol.get(symbol, {})
                    save_screener_row(conn, captured_at, symbol, data, raw)
                    trend = data.get("SHORT_TERM_TREND") or data.get("shortTermTrend", "n/a")
                    rsi   = data.get("RSI14") or data.get("rsi14", "n/a")
                    print(f"  [SCREENER] {symbol:6s}: trend={trend}  RSI={rsi}")

                print(f"  ✓ Screener: {len(WATCHLIST)} symbols saved")

            except Exception as e:
                print(f"  ✗ Screener error: {e}")

            # ─ Small pause between calls to be respectful of rate limits ────
            await asyncio.sleep(2)

            # ─ Signals (2 calls: bullish + bearish) ──────────────────────
            try:
                signals, raw = await fetch_signals(session)

                if not signals:
                    print("  [SIGNALS]  No new signals in last 24h")
                else:
                    for sig in signals:
                        symbol = sig.get("symbol", "UNKNOWN")
                        save_signal_row(conn, captured_at, symbol, sig, raw)
                        name = sig.get("signalName") or sig.get("name", "?")
                        direction = sig.get("direction", "?")
                        print(f"  [SIGNAL]   {symbol:6s}: {name} ({direction})")
                    print(f"  ✓ Signals: {len(signals)} entries saved")

            except Exception as e:
                print(f"  ✗ Signals error: {e}")

    print(f"  Next poll in {POLL_INTERVAL_SECONDS // 60} min.")


async def main():
    if not ALTFINS_API_KEY:
        print("ERROR: ALTFINS_API_KEY not set in .env")
        return

    print("=" * 60)
    print("altFINS Shadow Signal Logger  v2")
    print(f"Watchlist : {', '.join(WATCHLIST)}")
    print(f"DB        : {DB_PATH}")
    print(f"Interval  : every {POLL_INTERVAL_SECONDS // 60} minutes")
    print(f"Tools     : {SCREENER_TOOL} | {SIGNALS_TOOL}")
    print("=" * 60)
    print("Running alongside your live system — no trades, no risk.")
    print("Stop anytime with Ctrl+C\n")

    conn = init_db(DB_PATH)

    while True:
        try:
            await poll_once(conn)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"[ERROR] {e} — retrying next cycle")

        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    asyncio.run(main())
