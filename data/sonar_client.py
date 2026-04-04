"""Signal Forge v2 — Perplexity Sonar API Client

Real-time web search + synthesis for crypto sentiment.
Cost: $1/M input, $1/M output (sonar model).
Daily limit: $5.00 hard cap.
"""

import asyncio
from datetime import datetime
from dataclasses import dataclass, field
from loguru import logger
import httpx

from config.settings import settings

SONAR_API_BASE = "https://api.perplexity.ai"


@dataclass
class SonarUsageTracker:
    daily_input_tokens: int = 0
    daily_output_tokens: int = 0
    daily_cost_usd: float = 0.0
    reset_date: str = field(default_factory=lambda: datetime.utcnow().date().isoformat())
    DAILY_COST_LIMIT_USD: float = 5.00

    def add_usage(self, input_tokens: int, output_tokens: int, model: str = "sonar"):
        today = datetime.utcnow().date().isoformat()
        if today != self.reset_date:
            self.daily_input_tokens = 0
            self.daily_output_tokens = 0
            self.daily_cost_usd = 0.0
            self.reset_date = today

        self.daily_input_tokens += input_tokens
        self.daily_output_tokens += output_tokens
        rate_in = 3.0 if model == "sonar-pro" else 1.0
        rate_out = 15.0 if model == "sonar-pro" else 1.0
        cost = (input_tokens / 1_000_000 * rate_in) + (output_tokens / 1_000_000 * rate_out)
        self.daily_cost_usd += cost
        return cost

    def is_limit_reached(self) -> bool:
        return self.daily_cost_usd >= self.DAILY_COST_LIMIT_USD


class SonarClient:
    def __init__(self):
        self.api_key = settings.perplexity_api_key
        self.usage = SonarUsageTracker()
        self._semaphore = asyncio.Semaphore(3)
        self._last_request = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.usage.is_limit_reached()

    async def query(self, query: str, model: str = "sonar", max_tokens: int = 512,
                    recency: str = "day") -> dict:
        if not self.api_key:
            return {"content": "", "citations": [], "error": "no_api_key"}
        if self.usage.is_limit_reached():
            return {"content": "", "citations": [], "error": "daily_limit_reached"}

        async with self._semaphore:
            # Rate limit: 2s between requests
            now = asyncio.get_event_loop().time()
            if now - self._last_request < 2.0:
                await asyncio.sleep(2.0 - (now - self._last_request))
            self._last_request = asyncio.get_event_loop().time()

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": (
                        "You are a precise financial intelligence analyst. "
                        "Return concise, factual responses about crypto markets. "
                        "Focus on breaking news, whale activity, regulatory changes, and macro events."
                    )},
                    {"role": "user", "content": query},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "return_citations": True,
            }
            if recency:
                payload["search_recency_filter"] = recency

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        f"{SONAR_API_BASE}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        citations = data.get("citations", [])
                        usage = data.get("usage", {})
                        cost = self.usage.add_usage(
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            model,
                        )
                        return {"content": content, "citations": citations, "usage": usage, "cost_usd": cost}
                    else:
                        logger.error(f"Sonar API error: {r.status_code} {r.text[:200]}")
                        return {"content": "", "citations": [], "error": f"http_{r.status_code}"}
            except Exception as e:
                logger.error(f"Sonar query failed: {e}")
                return {"content": "", "citations": [], "error": str(e)}

    async def get_crypto_sentiment(self, symbol: str) -> dict:
        """Get real-time sentiment for a specific crypto asset."""
        base = symbol.replace("-USD", "").replace("/USD", "")
        query = (
            f"What is the latest news and sentiment for {base} cryptocurrency in the last 4 hours? "
            f"Include: 1) Breaking news, 2) Social media sentiment (bullish/bearish), "
            f"3) Whale activity, 4) Key price levels. "
            f"Return as JSON with fields: sentiment_score (-1 to 1), key_narratives (list), "
            f"bullish_factors (list), bearish_factors (list)."
        )
        return await self.query(query, recency="day")

    def get_usage_stats(self) -> dict:
        return {
            "daily_cost_usd": round(self.usage.daily_cost_usd, 4),
            "daily_input_tokens": self.usage.daily_input_tokens,
            "daily_output_tokens": self.usage.daily_output_tokens,
            "limit_reached": self.usage.is_limit_reached(),
        }
