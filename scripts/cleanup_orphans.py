#!/usr/bin/env python3
"""
Signal Forge V2 — Orphan Trade Cleanup

Reconciles the trades DB against actual Alpaca positions.
- If a DB trade is 'open' but no Alpaca position exists → mark closed
- If a DB trade is 'open' AND an Alpaca position exists → close it on Alpaca, then mark closed
- Logs everything to logs/cleanup.log

Usage:
    python scripts/cleanup_orphans.py           # dry run (show what would happen)
    python scripts/cleanup_orphans.py --execute  # actually close and update
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "trades.db"
LOG_PATH = ROOT / "logs" / "cleanup.log"

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "") or os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json",
}


def log(msg: str):
    """Print and append to log file."""
    print(msg)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {msg}\n")


def get_alpaca_positions() -> dict:
    """Fetch all open positions from Alpaca. Returns {symbol: position_dict}."""
    resp = requests.get(f"{ALPACA_BASE}/v2/positions", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        log(f"ERROR: Alpaca positions API returned {resp.status_code}")
        return {}
    positions = resp.json()
    # Key by symbol (BTCUSD format)
    return {p["symbol"]: p for p in positions}


def close_alpaca_position(symbol: str) -> dict:
    """Close a position on Alpaca via DELETE. Returns response dict."""
    resp = requests.delete(
        f"{ALPACA_BASE}/v2/positions/{symbol}",
        headers=HEADERS,
        timeout=10,
    )
    if resp.status_code in (200, 204):
        return resp.json() if resp.text else {"status": "closed"}
    return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}


def run_cleanup(execute: bool = False):
    now = datetime.now().isoformat()
    mode = "EXECUTE" if execute else "DRY RUN"

    log(f"")
    log(f"{'='*70}")
    log(f"  ORPHAN CLEANUP — {mode}")
    log(f"  {now}")
    log(f"{'='*70}")

    # 1. Get all DB open trades
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    db_open = conn.execute(
        "SELECT id, symbol, entry_price, size_usd, quantity, opened_at FROM trades WHERE status='open' ORDER BY opened_at DESC"
    ).fetchall()
    db_open = [dict(r) for r in db_open]

    log(f"  DB open trades: {len(db_open)}")

    # 2. Get Alpaca positions
    alpaca_positions = get_alpaca_positions()
    log(f"  Alpaca positions: {len(alpaca_positions)}")

    # 3. Reconcile
    results = []
    closed_on_alpaca = 0
    marked_no_position = 0

    for trade in db_open:
        trade_id = trade["id"]
        symbol = trade["symbol"]
        entry = trade["entry_price"] or 0
        qty = trade["quantity"] or 0
        size = trade["size_usd"] or 0

        # Convert DB symbol (BTC-USD) to Alpaca format (BTCUSD)
        alpaca_sym = symbol.replace("-", "")

        if alpaca_sym in alpaca_positions:
            # Position EXISTS on Alpaca — close it
            pos = alpaca_positions[alpaca_sym]
            current_price = float(pos.get("current_price", 0))
            pnl = (current_price - entry) * qty if entry > 0 and qty > 0 else 0

            if execute:
                close_result = close_alpaca_position(alpaca_sym)
                if "error" in close_result:
                    log(f"  ERROR closing {symbol}: {close_result['error']}")
                    continue

                conn.execute("""
                    UPDATE trades SET status='closed', exit_price=?, pnl_usd=?,
                    closed_at=?, close_reason='orphan_cleanup'
                    WHERE id=?
                """, (current_price, round(pnl, 2), now, trade_id))
                conn.commit()

            reason = "orphan_cleanup"
            closed_on_alpaca += 1
            results.append({
                "symbol": symbol, "entry": entry, "exit": current_price,
                "pnl": round(pnl, 2), "reason": reason,
            })
            # Remove from dict so we don't double-close
            del alpaca_positions[alpaca_sym]

        else:
            # Position does NOT exist on Alpaca — already closed
            if execute:
                conn.execute("""
                    UPDATE trades SET status='closed', closed_at=?,
                    close_reason='orphan_no_position'
                    WHERE id=?
                """, (now, trade_id))
                conn.commit()

            reason = "orphan_no_position"
            marked_no_position += 1
            results.append({
                "symbol": symbol, "entry": entry, "exit": 0,
                "pnl": 0, "reason": reason,
            })

    conn.close()

    # 4. Print summary
    log(f"")
    log(f"  {'Symbol':14} {'Entry':>12} {'Exit':>12} {'P&L':>10} {'Reason'}")
    log(f"  {'-'*62}")
    for r in results:
        exit_str = f"${r['exit']:,.4f}" if r['exit'] > 0 else "n/a"
        pnl_str = f"${r['pnl']:+,.2f}" if r['pnl'] != 0 else "$0.00"
        log(f"  {r['symbol']:14} ${r['entry']:>11,.4f} {exit_str:>12} {pnl_str:>10} {r['reason']}")

    log(f"")
    log(f"  SUMMARY")
    log(f"  Closed on Alpaca:    {closed_on_alpaca}")
    log(f"  Marked (no position): {marked_no_position}")
    log(f"  Total cleaned:       {len(results)}")

    if not execute:
        log(f"")
        log(f"  [DRY RUN] No changes made. Use --execute to apply.")

    log(f"{'='*70}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up orphaned trades")
    parser.add_argument("--execute", action="store_true", help="Actually close positions and update DB")
    args = parser.parse_args()

    if not ALPACA_KEY or not ALPACA_SECRET:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        sys.exit(1)

    run_cleanup(execute=args.execute)
