"""Signal Forge v2 — Sentiment Agent

Fetches Fear & Greed, DEXScreener trending, and optional Perplexity Sonar.
Emits SentimentEvent for each tracked asset.
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import SentimentEvent
from data import fear_greed_client


class SentimentAgent:
    def __init__(self, event_bus: EventBus, config: dict):
        self.bus = event_bus
        self.perplexity_key = config.get("perplexity_api_key", "")
        self.watchlist = config.get("watchlist", [])
        self._last_fg: int = 50
        self._trending: list = []

    async def run_forever(self, interval_seconds: int = 900):
        while True:
            try:
                await self._scan()
            except Exception as e:
                logger.error(f"SentimentAgent error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _scan(self):
        # Fetch Fear & Greed
        fg = await fear_greed_client.get_fear_greed()
        self._last_fg = fg.get("value", 50)

        # Fetch DEXScreener trending (free, no auth)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://api.dexscreener.com/token-boosts/latest/v1")
                if r.status_code == 200 and isinstance(r.json(), list):
                    self._trending = r.json()[:10]
        except Exception:
            pass

        # Emit sentiment for each watchlist symbol
        for symbol in self.watchlist:
            base = symbol.replace("-USD", "").upper()

            # Check if this coin is trending on DEX
            is_trending = any(
                base.lower() in str(t).lower() for t in self._trending
            )

            event = SentimentEvent(
                timestamp=datetime.now(),
                symbol=symbol,
                sentiment_score=self._fg_to_sentiment(self._last_fg),
                fear_greed=self._last_fg,
                social_volume_change_pct=20.0 if is_trending else 0.0,
                key_narratives=[fg.get("classification", "Unknown")],
            )
            await self.bus.publish(event)

        logger.info(f"SentimentAgent: F&G={self._last_fg}, {len(self._trending)} trending tokens")

    def _fg_to_sentiment(self, fg: int) -> float:
        """Convert Fear & Greed (0-100) to sentiment score (-1 to +1)."""
        return (fg - 50) / 50  # 0 → -1, 50 → 0, 100 → +1
