"""Signal Forge v2 — Arkham Intelligence Client

Free on-chain intelligence: 800M+ labeled wallets, entity tracking,
large transaction monitoring, smart money flows.

API requires key — apply at https://intel.arkm.com/api (free).
Auth: API-Key header. Rate limit: 20 req/s (1 req/s for heavy endpoints).
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from config.settings import settings

ARKHAM_BASE = "https://api.arkhamintelligence.com"


class ArkhamClient:
    """Arkham Intelligence API — smart money tracking + entity labels."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or getattr(settings, "arkham_api_key", "")
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.info("Arkham: not configured (apply at https://intel.arkm.com/api — free)")

    def _headers(self):
        return {"API-Key": self.api_key, "Accept": "application/json"}

    # ── Entity & Address Lookup ──

    async def lookup_address(self, address: str, chain: str = None) -> dict:
        """Get entity labels and metadata for a wallet address."""
        if not self.enabled:
            return {"enabled": False}
        endpoint = f"/intelligence/address/{address}/all"
        params = {"chain": chain} if chain else {}
        return await self._get(endpoint, params)

    async def lookup_entity(self, entity: str) -> dict:
        """Get entity info (e.g., 'binance', 'wintermute', 'jump-trading')."""
        if not self.enabled:
            return {"enabled": False}
        return await self._get(f"/intelligence/entity/{entity}")

    async def search(self, query: str) -> dict:
        """Search addresses, entities, or tokens by name."""
        if not self.enabled:
            return {"enabled": False}
        return await self._get("/intelligence/search", {"query": query})

    # ── Large Transfers (Whale Tracking) ──

    async def get_whale_transfers(
        self,
        entity_or_address: str = None,
        min_usd: float = 500_000,
        time_last: str = "4h",
        chains: str = "ethereum,bitcoin,solana",
        flow: str = "all",
        limit: int = 20,
    ) -> list[dict]:
        """Get large transfers — the core whale tracking signal.

        Args:
            entity_or_address: Filter by entity (e.g., 'binance') or address
            min_usd: Minimum USD value (default $500K)
            time_last: Lookback period (e.g., '1h', '4h', '24h', '7d')
            chains: Comma-separated chains
            flow: 'in', 'out', or 'all'
            limit: Max results (up to 10,000)
        """
        if not self.enabled:
            return []

        params = {
            "usdGte": str(int(min_usd)),
            "timeLast": time_last,
            "chains": chains,
            "flow": flow,
            "sortKey": "usd",
            "sortDir": "desc",
            "limit": str(limit),
        }
        if entity_or_address:
            params["base"] = entity_or_address

        data = await self._get("/transfers", params)
        transfers = data.get("transfers", []) if isinstance(data, dict) else []

        return [
            {
                "from": t.get("fromAddress", {}).get("arkhamEntity", {}).get("id", t.get("fromAddress", {}).get("address", "")[:10]),
                "to": t.get("toAddress", {}).get("arkhamEntity", {}).get("id", t.get("toAddress", {}).get("address", "")[:10]),
                "token": t.get("tokenSymbol", ""),
                "usd_value": t.get("usdValue", 0),
                "chain": t.get("chain", ""),
                "timestamp": t.get("timestamp", ""),
                "from_label": t.get("fromAddress", {}).get("arkhamEntity", {}).get("name", "Unknown"),
                "to_label": t.get("toAddress", {}).get("arkhamEntity", {}).get("name", "Unknown"),
            }
            for t in transfers[:limit]
        ]

    async def get_exchange_flows(
        self,
        exchange: str = "binance",
        token: str = None,
        time_last: str = "24h",
    ) -> dict:
        """Track net flow to/from an exchange — inflow = sell pressure, outflow = accumulation."""
        if not self.enabled:
            return {"enabled": False}

        params_in = {"base": exchange, "flow": "in", "timeLast": time_last, "sortKey": "usd", "sortDir": "desc", "limit": "50"}
        params_out = {"base": exchange, "flow": "out", "timeLast": time_last, "sortKey": "usd", "sortDir": "desc", "limit": "50"}
        if token:
            params_in["tokens"] = token
            params_out["tokens"] = token

        inflows = await self._get("/transfers", params_in)
        await asyncio.sleep(0.1)
        outflows = await self._get("/transfers", params_out)

        in_transfers = inflows.get("transfers", []) if isinstance(inflows, dict) else []
        out_transfers = outflows.get("transfers", []) if isinstance(outflows, dict) else []

        total_in = sum(t.get("usdValue", 0) for t in in_transfers)
        total_out = sum(t.get("usdValue", 0) for t in out_transfers)

        return {
            "exchange": exchange,
            "period": time_last,
            "inflow_usd": total_in,
            "outflow_usd": total_out,
            "net_flow_usd": total_in - total_out,
            "signal": "selling_pressure" if total_in > total_out * 1.2 else "accumulation" if total_out > total_in * 1.2 else "neutral",
            "inflow_count": len(in_transfers),
            "outflow_count": len(out_transfers),
        }

    # ── Smart Money Signals ──

    async def get_smart_money_moves(self, time_last: str = "4h", min_usd: float = 1_000_000) -> list[dict]:
        """Get large transfers from known smart money entities (funds, market makers)."""
        if not self.enabled:
            return []

        smart_entities = [
            "wintermute", "jump-trading", "galaxy-digital", "alameda-research",
            "three-arrows-capital", "paradigm", "a16z", "polychain",
        ]

        all_transfers = []
        for entity in smart_entities[:5]:  # Rate limit friendly
            transfers = await self.get_whale_transfers(
                entity_or_address=entity, min_usd=min_usd, time_last=time_last, limit=5
            )
            for t in transfers:
                t["smart_entity"] = entity
            all_transfers.extend(transfers)
            await asyncio.sleep(0.2)

        all_transfers.sort(key=lambda t: t.get("usd_value", 0), reverse=True)
        return all_transfers[:20]

    # ── Token Holder Analysis ──

    async def get_token_holders(self, token_id: str) -> dict:
        """Get top holders for a token (by CoinGecko ID)."""
        if not self.enabled:
            return {"enabled": False}
        return await self._get(f"/token/holders/{token_id}")

    # ── Market Data ──

    async def get_network_status(self) -> dict:
        """Get all chain status: price, volume, market cap, gas."""
        if not self.enabled:
            return {"enabled": False}
        return await self._get("/networks/status")

    async def get_entity_balance_changes(self, time_last: str = "24h", limit: int = 20) -> list:
        """Ranked entities by balance change — shows who's accumulating/distributing."""
        if not self.enabled:
            return []
        data = await self._get("/intelligence/entity_balance_changes", {"timeLast": time_last, "limit": str(limit)})
        return data if isinstance(data, list) else data.get("results", []) if isinstance(data, dict) else []

    # ── HTTP Helper ──

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"{ARKHAM_BASE}{endpoint}",
                    headers=self._headers(),
                    params=params or {},
                )
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 401:
                    logger.warning("Arkham: invalid API key")
                    self.enabled = False
                    return {"error": "invalid_api_key"}
                elif r.status_code == 429:
                    logger.warning("Arkham: rate limited")
                    return {"error": "rate_limited"}
                else:
                    logger.debug(f"Arkham {endpoint}: HTTP {r.status_code}")
                    return {"error": f"http_{r.status_code}"}
            except Exception as e:
                logger.debug(f"Arkham {endpoint} failed: {e}")
                return {"error": str(e)}

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "api_key_set": bool(self.api_key),
            "base_url": ARKHAM_BASE,
            "cost": "Free (apply at intel.arkm.com/api)",
            "rate_limit": "20 req/s standard, 1 req/s heavy endpoints",
            "features": [
                "800M+ labeled wallet addresses",
                "Entity tracking (exchanges, funds, whales)",
                "Large transfer monitoring ($500K+)",
                "Exchange inflow/outflow analysis",
                "Smart money movement detection",
                "Token holder analysis",
                "13 chains: ETH, BTC, SOL, BSC, Polygon, Arbitrum, Optimism, Base, Tron, etc.",
            ],
        }
