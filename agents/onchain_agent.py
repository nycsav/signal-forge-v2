"""Signal Forge v2 — On-Chain Agent

Monitors whale alerts, exchange flows, smart money signals.
Uses free APIs where available, stubs for premium (Nansen, CryptoQuant).
Emits OnChainEvent.
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import OnChainEvent


class OnChainAgent:
    def __init__(self, event_bus: EventBus, config: dict):
        self.bus = event_bus
        self.watchlist = config.get("watchlist", [])
        self.whale_alert_key = config.get("whale_alert_api_key", "")
        self._whale_data: dict = {}

    async def run_forever(self, interval_seconds: int = 3600):
        while True:
            try:
                await self._scan()
            except Exception as e:
                logger.error(f"OnChainAgent error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _scan(self):
        # Fetch whale alerts (free tier: 10 req/min)
        if self.whale_alert_key:
            await self._fetch_whale_alerts()

        # Emit events for watchlist
        for symbol in self.watchlist:
            base = symbol.replace("-USD", "").upper()
            whale = self._whale_data.get(base, {})

            event = OnChainEvent(
                timestamp=datetime.now(),
                symbol=symbol,
                whale_net_flow=whale.get("net_flow", 0),
                large_tx_count_1h=whale.get("large_tx", 0),
            )
            await self.bus.publish(event)

        logger.info(f"OnChainAgent: emitted {len(self.watchlist)} events")

    async def _fetch_whale_alerts(self):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://api.whale-alert.io/v1/transactions",
                    params={
                        "api_key": self.whale_alert_key,
                        "min_value": 500000,
                        "limit": 20,
                    },
                )
                if r.status_code == 200:
                    txs = r.json().get("transactions", [])
                    # Aggregate by symbol
                    for tx in txs:
                        sym = tx.get("symbol", "").upper()
                        if sym not in self._whale_data:
                            self._whale_data[sym] = {"net_flow": 0, "large_tx": 0}
                        self._whale_data[sym]["large_tx"] += 1
                        amount = float(tx.get("amount", 0))
                        # If going to exchange = selling, from exchange = buying
                        if tx.get("to", {}).get("owner_type") == "exchange":
                            self._whale_data[sym]["net_flow"] -= amount
                        else:
                            self._whale_data[sym]["net_flow"] += amount
        except Exception as e:
            logger.debug(f"Whale Alert fetch: {e}")
