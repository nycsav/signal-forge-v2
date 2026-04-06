"""Signal Forge v2 — Nansen On-Chain Intelligence Client

Smart money tracking, wallet labels, token flows.
Requires NANSEN_API_KEY in .env ($150+/mo).

Nansen's edge: labels 300M+ wallets as "smart money", "whale", "fund",
"exchange", etc. Shows what the smartest traders are buying/selling.
"""

from loguru import logger
import httpx
from config.settings import settings

NANSEN_BASE = "https://api.nansen.ai/v1"


class NansenClient:
    def __init__(self):
        self.api_key = settings.nansen_api_key
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.info("Nansen: not configured (add NANSEN_API_KEY to .env)")

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def get_smart_money_signals(self, token_address: str = None) -> dict:
        """Get smart money flow signals — what are labeled whales/funds doing?"""
        if not self.enabled:
            return {"enabled": False, "note": "Add NANSEN_API_KEY to .env ($150+/mo)"}

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"{NANSEN_BASE}/smart-money/signals",
                    headers=self._headers(),
                    params={"token": token_address} if token_address else {},
                )
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 401:
                    logger.warning("Nansen: invalid API key")
                    self.enabled = False
            except Exception as e:
                logger.debug(f"Nansen smart money failed: {e}")
        return {}

    async def get_token_flows(self, symbol: str) -> dict:
        """Get token inflow/outflow to/from exchanges and smart money wallets."""
        if not self.enabled:
            return {"enabled": False}

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"{NANSEN_BASE}/token/{symbol}/flows",
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    return r.json()
            except Exception as e:
                logger.debug(f"Nansen token flows failed for {symbol}: {e}")
        return {}

    async def get_whale_transactions(self, min_usd: float = 500000) -> list[dict]:
        """Get recent large transactions from labeled wallets."""
        if not self.enabled:
            return []

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"{NANSEN_BASE}/transactions/whale",
                    headers=self._headers(),
                    params={"min_usd_value": min_usd, "limit": 20},
                )
                if r.status_code == 200:
                    return r.json().get("transactions", [])
            except Exception as e:
                logger.debug(f"Nansen whale transactions failed: {e}")
        return []

    async def get_exchange_flows(self, token: str = "ETH") -> dict:
        """Track net flow to/from exchanges — inflow = selling pressure, outflow = accumulation."""
        if not self.enabled:
            return {"enabled": False}

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"{NANSEN_BASE}/exchange-flows/{token}",
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    return r.json()
            except Exception as e:
                logger.debug(f"Nansen exchange flows failed: {e}")
        return {}

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "api_key_set": bool(self.api_key),
            "cost": "$150+/month",
            "features": [
                "Smart money wallet labels (300M+ wallets)",
                "Token flow tracking (exchange in/outflow)",
                "Whale transaction alerts",
                "Fund/DAO treasury tracking",
            ],
        }
