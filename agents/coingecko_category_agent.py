"""
CoinGecko Category Momentum Agent — Signal Forge V2
Detects narrative-driven category rotation signals
Scans every 30min via scripts/run_category_watcher.py
"""

import requests
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DB_PATH = Path(__file__).parent.parent / "db" / "category_signals.db"

# === Tuning Parameters ===
CATEGORY_THRESHOLD = 10.0   # Min % 24h category gain to investigate
COIN_MIN_GAIN = 15.0        # Min % 24h coin gain to fire signal
MIN_MCAP = 500_000          # $500K floor — filter dust
MAX_MCAP = 100_000_000      # $100M ceiling — sweet spot
VOL_MCAP_MIN = 0.3          # Organic movement filter
COOLDOWN_HOURS = 6          # Dedup window per symbol/category


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            name TEXT,
            category TEXT,
            category_change_24h REAL,
            coin_change_24h REAL,
            coin_change_1h REAL,
            market_cap REAL,
            volume_24h REAL,
            vol_mcap_ratio REAL,
            phase TEXT,
            confidence REAL,
            price_usd REAL,
            fired INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def already_fired(symbol: str, category: str) -> bool:
    """Prevent duplicate signals within cooldown window"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        SELECT id FROM category_signals
        WHERE symbol = ? AND category = ?
        AND datetime(timestamp) > datetime('now', ? || ' hours')
        AND fired = 1
    """, (symbol, category, f"-{COOLDOWN_HOURS}"))
    result = cur.fetchone()
    conn.close()
    return result is not None


def save_signal(signal: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO category_signals
        (timestamp, symbol, name, category, category_change_24h, coin_change_24h,
         coin_change_1h, market_cap, volume_24h, vol_mcap_ratio, phase, confidence,
         price_usd, fired)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
    """, (
        signal["timestamp"], signal["symbol"], signal["name"],
        signal["category"], signal["category_change_24h"],
        signal["coin_change_24h"], signal["coin_change_1h"],
        signal["market_cap"], signal["volume_24h"], signal["vol_mcap_ratio"],
        signal["phase"], signal["confidence"], signal["price_usd"]
    ))
    conn.commit()
    conn.close()


def get_category_leaders() -> list:
    r = requests.get(
        f"{COINGECKO_BASE}/coins/categories",
        params={"order": "market_cap_change_24h_desc"},
        timeout=15
    )
    r.raise_for_status()
    return r.json()


def get_coins_in_category(category_id: str) -> list:
    r = requests.get(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "category": category_id,
            "order": "price_change_percentage_24h_desc",
            "per_page": 5,
            "page": 1,
            "sparkline": False,
            "price_change_percentage": "1h,24h"
        },
        timeout=15
    )
    r.raise_for_status()
    return r.json()


def scan() -> list:
    """Main scan — returns list of new signals fired this run"""
    init_db()
    signals = []
    now = datetime.utcnow().isoformat()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning CoinGecko categories...")
    categories = get_category_leaders()

    for cat in categories[:25]:
        change_24h = cat.get("market_cap_change_24h") or 0
        if change_24h < CATEGORY_THRESHOLD:
            continue

        print(f"  {cat['name']} +{change_24h:.1f}% — checking top coins...")

        try:
            coins = get_coins_in_category(cat["id"])
        except Exception as e:
            print(f"    Skipped ({e})")
            time.sleep(1)
            continue

        for coin in coins:
            mcap = coin.get("market_cap") or 0
            vol = coin.get("total_volume") or 0
            chg_24h = coin.get("price_change_percentage_24h") or 0
            chg_1h = coin.get("price_change_percentage_1h_in_currency") or 0
            price = coin.get("current_price") or 0
            symbol = coin.get("symbol", "").upper()

            if not (MIN_MCAP < mcap < MAX_MCAP):
                continue
            if chg_24h < COIN_MIN_GAIN:
                continue

            vol_mcap = round(vol / mcap, 2) if mcap > 0 else 0
            if vol_mcap < VOL_MCAP_MIN:
                continue

            if already_fired(symbol, cat["name"]):
                continue

            phase = "1" if chg_1h > 3 else "2"
            confidence = round(min(0.92, 0.50 + (chg_24h / 120)), 2)

            signal = {
                "timestamp": now,
                "symbol": symbol,
                "name": coin.get("name", ""),
                "category": cat["name"],
                "category_change_24h": round(change_24h, 2),
                "coin_change_24h": round(chg_24h, 2),
                "coin_change_1h": round(chg_1h, 2),
                "market_cap": mcap,
                "volume_24h": vol,
                "vol_mcap_ratio": vol_mcap,
                "phase": phase,
                "confidence": confidence,
                "price_usd": price,
                "direction": "LONG"
            }

            save_signal(signal)
            signals.append(signal)
            print(f"  SIGNAL: {symbol} | {cat['name']} +{change_24h:.1f}% | "
                  f"Coin +{chg_24h:.1f}% | Phase {phase} | {confidence:.0%} conf")

        time.sleep(0.8)  # CoinGecko free tier: 30 req/min

    print(f"  {len(signals)} new signals fired\n")
    return signals


def cross_reference_email_tokens(email_tokens: list[str]) -> list[dict]:
    """Cross-reference tokens from CoinGecko email with category signals DB.

    If a token appears in both the daily email AND the category momentum scan,
    it's a high-conviction day trade candidate.

    Args:
        email_tokens: list of ticker symbols from CoinGecko email (e.g. ["HYPE", "SOL"])

    Returns:
        list of cross-validated signals with boosted confidence
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cross_validated = []

    for symbol in email_tokens:
        cur = conn.execute("""
            SELECT symbol, category, coin_change_24h, phase, confidence, price_usd, timestamp
            FROM category_signals
            WHERE symbol = ? AND datetime(timestamp) > datetime('now', '-24 hours')
            ORDER BY timestamp DESC LIMIT 1
        """, (symbol.upper(),))
        row = cur.fetchone()
        if row:
            cross_validated.append({
                "symbol": row[0],
                "category": row[1],
                "coin_change_24h": row[2],
                "phase": row[3],
                "confidence": min(0.95, row[4] + 0.15),  # boost for cross-validation
                "price_usd": row[5],
                "cross_validated": True,
                "source": "coingecko_email + category_agent",
            })

    conn.close()
    return cross_validated


if __name__ == "__main__":
    results = scan()
    if results:
        print(json.dumps(results, indent=2))
