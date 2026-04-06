"""Signal Forge v2 — CoinGecko Client

Market intelligence layer: trending coins, categories, market cap, metadata.
NOT used for OHLCV (Binance/Coinbase are better for that).
"""

import time
from loguru import logger
import httpx

CG_BASE = "https://api.coingecko.com/api/v3"

# Rate limiter: 30 calls/min on free tier
_last_call = 0
_MIN_INTERVAL = 2.5  # seconds between calls


async def _rate_limit():
    global _last_call
    now = time.time()
    if now - _last_call < _MIN_INTERVAL:
        import asyncio
        await asyncio.sleep(_MIN_INTERVAL - (now - _last_call))
    _last_call = time.time()


async def get_trending() -> list[dict]:
    """Get trending coins (top searches in last 24h)."""
    await _rate_limit()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{CG_BASE}/search/trending")
            if r.status_code == 200:
                coins = r.json().get("coins", [])
                return [
                    {
                        "id": c["item"]["id"],
                        "name": c["item"]["name"],
                        "symbol": c["item"]["symbol"],
                        "market_cap_rank": c["item"].get("market_cap_rank"),
                        "price_btc": c["item"].get("price_btc", 0),
                    }
                    for c in coins[:10]
                ]
        except Exception as e:
            logger.debug(f"CoinGecko trending failed: {e}")
    return []


async def get_top_gainers_losers() -> dict:
    """Get top gainers and losers in last 24h."""
    await _rate_limit()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{CG_BASE}/coins/markets",
                params={"vs_currency": "usd", "order": "volume_desc", "per_page": 50, "page": 1,
                         "sparkline": "false", "price_change_percentage": "24h"},
            )
            if r.status_code == 200:
                coins = r.json()
                sorted_by_change = sorted(coins, key=lambda c: c.get("price_change_percentage_24h") or 0)
                return {
                    "gainers": [
                        {"symbol": c["symbol"].upper(), "name": c["name"],
                         "change_24h": c.get("price_change_percentage_24h", 0),
                         "volume": c.get("total_volume", 0)}
                        for c in sorted_by_change[-5:]
                    ][::-1],
                    "losers": [
                        {"symbol": c["symbol"].upper(), "name": c["name"],
                         "change_24h": c.get("price_change_percentage_24h", 0),
                         "volume": c.get("total_volume", 0)}
                        for c in sorted_by_change[:5]
                    ],
                }
        except Exception as e:
            logger.debug(f"CoinGecko gainers/losers failed: {e}")
    return {"gainers": [], "losers": []}


async def get_global_market() -> dict:
    """Get global crypto market data."""
    await _rate_limit()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{CG_BASE}/global")
            if r.status_code == 200:
                d = r.json().get("data", {})
                return {
                    "total_market_cap_usd": d.get("total_market_cap", {}).get("usd", 0),
                    "total_volume_24h_usd": d.get("total_volume", {}).get("usd", 0),
                    "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
                    "eth_dominance": d.get("market_cap_percentage", {}).get("eth", 0),
                    "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
                    "market_cap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd", 0),
                }
        except Exception as e:
            logger.debug(f"CoinGecko global market failed: {e}")
    return {}


async def get_categories() -> list[dict]:
    """Get top crypto categories by market cap."""
    await _rate_limit()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{CG_BASE}/coins/categories",
                                 params={"order": "market_cap_desc"})
            if r.status_code == 200:
                return [
                    {"name": c["name"], "market_cap": c.get("market_cap", 0),
                     "market_cap_change_24h": c.get("market_cap_change_24h", 0),
                     "volume_24h": c.get("volume_24h", 0),
                     "top_coins": [coin.split("/")[-1] for coin in (c.get("top_3_coins") or [])[:3]]}
                    for c in r.json()[:15]
                ]
        except Exception as e:
            logger.debug(f"CoinGecko categories failed: {e}")
    return []
