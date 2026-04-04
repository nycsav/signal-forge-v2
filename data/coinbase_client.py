"""Coinbase Advanced API client — price data, no auth needed for public endpoints."""

import httpx
from loguru import logger

COINBASE_BASE = "https://api.coinbase.com/api/v3/brokerage/market/products"


async def get_price(symbol: str) -> float:
    """Get current price. Symbol format: BTC-USD."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{COINBASE_BASE}/{symbol}")
            if r.status_code == 200:
                return float(r.json().get("price", 0))
        except Exception as e:
            logger.debug(f"Coinbase price fetch failed for {symbol}: {e}")
    return 0.0


async def get_all_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch prices for all symbols concurrently."""
    import asyncio
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        for sym in symbols:
            tasks.append(_fetch_one(client, sym))
        prices = await asyncio.gather(*tasks)
        for sym, price in zip(symbols, prices):
            results[sym] = price
    return results


async def _fetch_one(client: httpx.AsyncClient, symbol: str) -> float:
    try:
        r = await client.get(f"{COINBASE_BASE}/{symbol}")
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0
