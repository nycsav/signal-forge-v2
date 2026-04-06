"""Signal Forge v2 — Sentiment Agent

Fetches Fear & Greed, DEXScreener trending, and Perplexity Sonar (if configured).
Emits SentimentEvent for each tracked asset.
"""

import asyncio
import json
import re
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import SentimentEvent
from data import fear_greed_client
from config.settings import settings


class SentimentAgent:
    def __init__(self, event_bus: EventBus, config: dict):
        self.bus = event_bus
        self.perplexity_key = config.get("perplexity_api_key", "")
        self.watchlist = config.get("watchlist", [])
        self._last_fg: int = 50
        self._trending: list = []
        self._sonar_cache: dict[str, dict] = {}  # symbol → {score, narratives, fetched_at}
        self._sonar_cycle: int = 0

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

        # Fetch DEXScreener trending
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://api.dexscreener.com/token-boosts/latest/v1")
                if r.status_code == 200 and isinstance(r.json(), list):
                    self._trending = r.json()[:10]
        except Exception:
            pass

        # Perplexity Sonar — query top coins every 4th cycle (~1 hour)
        self._sonar_cycle += 1
        if self.perplexity_key and self._sonar_cycle % 4 == 1:
            await self._fetch_sonar_sentiment()

        # Emit sentiment for each symbol
        for symbol in self.watchlist:
            base = symbol.replace("-USD", "").upper()
            is_trending = any(base.lower() in str(t).lower() for t in self._trending)

            # Sonar data if available
            sonar = self._sonar_cache.get(symbol, {})
            sonar_score = sonar.get("score", 0)
            sonar_narratives = sonar.get("narratives", [])
            sonar_sources = sonar.get("sources", [])

            # Blend F&G + Sonar
            fg_sentiment = self._fg_to_sentiment(self._last_fg)
            if sonar_score != 0:
                blended = fg_sentiment * 0.4 + sonar_score * 0.6  # Sonar weighted higher
            else:
                blended = fg_sentiment

            narratives = sonar_narratives or [fg.get("classification", "Unknown")]

            event = SentimentEvent(
                timestamp=datetime.now(),
                symbol=symbol,
                sentiment_score=blended,
                fear_greed=self._last_fg,
                social_volume_change_pct=20.0 if is_trending else 0.0,
                key_narratives=narratives[:3],
                sonar_summary=sonar.get("summary", ""),
                sources=sonar_sources[:3],
            )
            await self.bus.publish(event)

        sonar_status = f", Sonar active ({len(self._sonar_cache)} cached)" if self.perplexity_key else ""
        logger.info(f"SentimentAgent: F&G={self._last_fg}, {len(self._trending)} trending{sonar_status}")

    async def _fetch_sonar_sentiment(self):
        """Query Perplexity Sonar for top coins sentiment."""
        # Only query coins we hold positions in + top 3 by market cap
        top_coins = self.watchlist[:5]  # BTC, ETH, SOL, XRP, BNB

        async with httpx.AsyncClient(timeout=30) as client:
            for symbol in top_coins:
                base = symbol.replace("-USD", "")
                query = (
                    f"What is the current market sentiment for {base} cryptocurrency? "
                    f"Include: 1) Latest news in last 4 hours, 2) Social sentiment (bullish/bearish), "
                    f"3) Key price levels. Be concise. "
                    f"Return JSON: {{\"sentiment\": -1 to 1, \"narratives\": [\"...\"], \"bullish\": [\"...\"], \"bearish\": [\"...\"]}}"
                )
                try:
                    r = await client.post(
                        "https://api.perplexity.ai/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.perplexity_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "sonar",
                            "messages": [
                                {"role": "system", "content": "You are a crypto market analyst. Return concise JSON."},
                                {"role": "user", "content": query},
                            ],
                            "max_tokens": 300,
                            "temperature": 0.1,
                            "search_recency_filter": "day",
                            "return_citations": True,
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        citations = data.get("citations", [])

                        # Parse JSON from response
                        score = 0
                        narratives = []
                        try:
                            matches = re.findall(r'\{[^{}]*\}', content)
                            for m in matches:
                                parsed = json.loads(m)
                                if "sentiment" in parsed:
                                    score = float(parsed["sentiment"])
                                    narratives = parsed.get("narratives", [])
                                    break
                        except Exception:
                            pass

                        self._sonar_cache[symbol] = {
                            "score": score,
                            "narratives": narratives,
                            "summary": content[:200],
                            "sources": citations[:3],
                            "fetched_at": datetime.now().isoformat(),
                        }
                        logger.info(f"Sonar: {base} sentiment={score:+.2f} narratives={len(narratives)}")
                    elif r.status_code == 401:
                        logger.warning("Sonar: invalid API key")
                        self.perplexity_key = ""  # Disable further calls
                        break
                    else:
                        logger.debug(f"Sonar: {r.status_code} for {base}")

                    await asyncio.sleep(2)  # Rate limit
                except Exception as e:
                    logger.error(f"Sonar query failed for {base}: {e}")

    def _fg_to_sentiment(self, fg: int) -> float:
        return (fg - 50) / 50
