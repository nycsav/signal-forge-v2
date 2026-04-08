"""Signal Forge v2 — CoinMarketCap Client

CMC gives us: rankings (the industry standard), trending, gainers/losers,
new listings, category data, and global metrics.

API key: CMC_API_KEY in .env
Base: https://pro-api.coinmarketcap.com
Free tier: 10,000 credits/month, 30 calls/min
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from config.settings import settings

CMC_BASE = "https://pro-api.coinmarketcap.com"


class CoinMarketCapClient:
    def __init__(self):
        self.api_key = getattr(settings, "cmc_api_key", "") or ""
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.info("CoinMarketCap: no API key configured")

    def _headers(self):
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    async def get_latest_listings(self, limit: int = 50, sort: str = "market_cap") -> list[dict]:
        """Top coins by market cap — the CMC ranking everyone watches."""
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{CMC_BASE}/v1/cryptocurrency/listings/latest",
                    headers=self._headers(),
                    params={"limit": limit, "sort": sort, "convert": "USD"})
                if r.status_code == 200:
                    coins = r.json().get("data", [])
                    return [
                        {
                            "rank": c.get("cmc_rank"),
                            "symbol": c.get("symbol"),
                            "name": c.get("name"),
                            "price": c.get("quote", {}).get("USD", {}).get("price", 0),
                            "market_cap": c.get("quote", {}).get("USD", {}).get("market_cap", 0),
                            "volume_24h": c.get("quote", {}).get("USD", {}).get("volume_24h", 0),
                            "change_1h": c.get("quote", {}).get("USD", {}).get("percent_change_1h", 0),
                            "change_24h": c.get("quote", {}).get("USD", {}).get("percent_change_24h", 0),
                            "change_7d": c.get("quote", {}).get("USD", {}).get("percent_change_7d", 0),
                            "volume_change_24h": c.get("quote", {}).get("USD", {}).get("volume_change_24h", 0),
                        }
                        for c in coins
                    ]
                else:
                    logger.debug(f"CMC listings: HTTP {r.status_code}")
            except Exception as e:
                logger.debug(f"CMC listings failed: {e}")
        return []

    async def get_trending(self) -> list[dict]:
        """CMC trending — most searched/viewed coins."""
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{CMC_BASE}/v1/cryptocurrency/trending/latest",
                    headers=self._headers())
                if r.status_code == 200:
                    return r.json().get("data", [])
                # Trending may not be available on free tier
                logger.debug(f"CMC trending: HTTP {r.status_code}")
            except Exception as e:
                logger.debug(f"CMC trending failed: {e}")
        return []

    async def get_gainers_losers(self, time_period: str = "24h", limit: int = 20) -> dict:
        """Top gainers and losers — momentum signal."""
        if not self.enabled:
            return {"gainers": [], "losers": []}
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{CMC_BASE}/v1/cryptocurrency/trending/gainers-losers",
                    headers=self._headers(),
                    params={"time_period": time_period, "limit": limit, "convert": "USD"})
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    gainers = [d for d in data if d.get("quote", {}).get("USD", {}).get(f"percent_change_{time_period}", 0) > 0]
                    losers = [d for d in data if d.get("quote", {}).get("USD", {}).get(f"percent_change_{time_period}", 0) < 0]
                    return {
                        "gainers": [{"symbol": c["symbol"], "name": c["name"],
                                     "change": c["quote"]["USD"].get(f"percent_change_{time_period}", 0),
                                     "volume": c["quote"]["USD"].get("volume_24h", 0)}
                                    for c in gainers[:10]],
                        "losers": [{"symbol": c["symbol"], "name": c["name"],
                                    "change": c["quote"]["USD"].get(f"percent_change_{time_period}", 0),
                                    "volume": c["quote"]["USD"].get("volume_24h", 0)}
                                   for c in losers[:10]],
                    }
            except Exception as e:
                logger.debug(f"CMC gainers/losers failed: {e}")
        return {"gainers": [], "losers": []}

    async def get_global_metrics(self) -> dict:
        """Global crypto market data — total market cap, BTC dominance, volume."""
        if not self.enabled:
            return {}
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{CMC_BASE}/v1/global-metrics/quotes/latest",
                    headers=self._headers())
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    usd = d.get("quote", {}).get("USD", {})
                    return {
                        "total_market_cap": usd.get("total_market_cap", 0),
                        "total_volume_24h": usd.get("total_volume_24h", 0),
                        "btc_dominance": d.get("btc_dominance", 0),
                        "eth_dominance": d.get("eth_dominance", 0),
                        "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
                        "total_market_cap_yesterday_pct_change": usd.get("total_market_cap_yesterday_percentage_change", 0),
                    }
            except Exception as e:
                logger.debug(f"CMC global metrics failed: {e}")
        return {}

    async def get_new_listings(self, limit: int = 20) -> list[dict]:
        """Recently added coins on CMC — new listing detection."""
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{CMC_BASE}/v1/cryptocurrency/listings/new",
                    headers=self._headers(), params={"limit": limit, "convert": "USD"})
                if r.status_code == 200:
                    return [
                        {
                            "symbol": c.get("symbol"),
                            "name": c.get("name"),
                            "date_added": c.get("date_added"),
                            "price": c.get("quote", {}).get("USD", {}).get("price", 0),
                            "market_cap": c.get("quote", {}).get("USD", {}).get("market_cap", 0),
                            "volume_24h": c.get("quote", {}).get("USD", {}).get("volume_24h", 0),
                            "change_24h": c.get("quote", {}).get("USD", {}).get("percent_change_24h", 0),
                        }
                        for c in r.json().get("data", [])
                    ]
            except Exception as e:
                logger.debug(f"CMC new listings failed: {e}")
        return []

    async def get_volume_spikes(self, limit: int = 50) -> list[dict]:
        """Find coins with unusual volume — potential breakout signal."""
        listings = await self.get_latest_listings(limit=limit, sort="volume_24h")
        spikes = []
        for c in listings:
            vol_change = c.get("volume_change_24h", 0) or 0
            if vol_change > 100:  # Volume more than doubled
                spikes.append({**c, "signal": f"Volume spike: +{vol_change:.0f}% vs yesterday"})
        return sorted(spikes, key=lambda x: x.get("volume_change_24h", 0), reverse=True)

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "api_key_set": bool(self.api_key),
            "cost": "Free tier: 10K credits/month",
            "features": ["CMC rankings", "Trending", "Gainers/losers", "Global metrics",
                          "New listings", "Volume spikes", "Category data"],
        }
