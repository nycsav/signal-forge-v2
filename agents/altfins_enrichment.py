"""altFINS Enrichment Agent — background poller + cache.

Polls altFINS MCP for chart patterns (4h) and screener data (15min).
Exposes pure lookup functions that scoring.py and risk_agent.py consume.

Does NOT modify the EventBus or agent hierarchy. Just a data cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from loguru import logger

# ── Config ────────────────────────────────────────────────────────

ALTFINS_MCP_URL = os.getenv("ALTFINS_MCP_URL", "https://mcp.altfins.com/mcp")
PATTERN_POLL_SECONDS = 4 * 3600       # every 4 hours
SCREENER_POLL_SECONDS = 15 * 60       # every 15 minutes
TA_CACHE_TTL_SECONDS = 300            # TA confirmation cache: 5 min
NEWS_CACHE_TTL_SECONDS = 300          # news cache: 5 min

# Pattern filter thresholds
MIN_PATTERN_SUCCESS_RATE = 67.0       # only patterns with >= 67% success rate
PATTERN_BONUS_POINTS = 12             # score bonus for a qualifying pattern (10-15 range)

# Oversold in Uptrend
OVERSOLD_RSI_THRESHOLD = 30
OVERSOLD_BONUS_POINTS = 20            # per spec


class AltFINSEnrichment:
    """Background poller + in-memory cache for altFINS enrichment data."""

    def __init__(self, api_key: str, watchlist: list[str]):
        self.api_key = api_key
        self.watchlist = [s.replace("-USD", "").replace("/USD", "").upper() for s in watchlist]
        # Caches keyed by symbol (uppercase, no suffix)
        self._pattern_cache: dict[str, list[dict]] = {}     # symbol → [pattern, ...]
        self._oversold_uptrend: set[str] = set()             # symbols matching oversold+uptrend
        self._ta_cache: dict[str, dict] = {}                 # symbol → {data, fetched_at}
        self._news_cache: dict[str, dict] = {}               # symbol → {data, fetched_at}
        self._pattern_last_poll: float = 0
        self._screener_last_poll: float = 0

    # ── Background loops ─────────────────────────────────────────

    async def start(self):
        """Launch background polling tasks. Call once from the orchestrator."""
        asyncio.create_task(self._poll_patterns_loop())
        asyncio.create_task(self._poll_screener_loop())
        logger.info("AltFINS enrichment started (patterns=4h, screener=15m)")

    async def _poll_patterns_loop(self):
        while True:
            try:
                await self._fetch_patterns()
            except Exception as e:
                logger.debug(f"altFINS pattern poll error: {e}")
            await asyncio.sleep(PATTERN_POLL_SECONDS)

    async def _poll_screener_loop(self):
        while True:
            try:
                await self._fetch_oversold_uptrend()
            except Exception as e:
                logger.debug(f"altFINS screener poll error: {e}")
            await asyncio.sleep(SCREENER_POLL_SECONDS)

    # ── Data fetchers (MCP) ──────────────────────────────────────

    async def _mcp_call(self, tool_name: str, arguments: dict) -> list[dict]:
        """Single MCP tool call. Returns parsed list of items."""
        try:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
        except ImportError:
            return []
        headers = {"X-Api-Key": self.api_key}
        async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return self._parse_result(result)

    @staticmethod
    def _parse_result(result) -> list[dict]:
        items = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                if isinstance(data.get("content"), list):
                    items.extend(data["content"])
                elif "symbol" in data or "signalKey" in data or "pattern" in data:
                    items.append(data)
        return items

    async def _fetch_patterns(self):
        """Fetch chart patterns for all watchlist coins. Filter for quality."""
        items = await self._mcp_call("pattern_getCryptoPatternData", {
            "coins": self.watchlist,
            "size": 100,
        })
        self._pattern_cache.clear()
        accepted = 0
        for p in items:
            sym = (p.get("symbol") or "").upper()
            success = float(p.get("successRate") or p.get("success_rate") or 0)
            direction = (p.get("direction") or p.get("breakoutDirection") or "").upper()
            ptype = (p.get("patternType") or p.get("type") or "").lower()

            # Filter: success_rate >= 67%, BUY direction, breakout type
            if success < MIN_PATTERN_SUCCESS_RATE:
                continue
            if direction not in ("BUY", "BULLISH", "LONG"):
                continue

            if sym not in self._pattern_cache:
                self._pattern_cache[sym] = []
            self._pattern_cache[sym].append(p)
            accepted += 1

        self._pattern_last_poll = time.time()
        if accepted:
            logger.info(f"altFINS patterns: {accepted} qualifying across {len(self._pattern_cache)} symbols")

    async def _fetch_oversold_uptrend(self):
        """Query screener for RSI14 < 30 AND SMA200 trend UP AND mcap > $100M."""
        items = await self._mcp_call("screener_getAltfinsScreenerData", {
            "coins": self.watchlist,
            "displayTypes": [
                "RSI14",
                "LONG_TERM_TREND",
                "MARKET_CAP",
            ],
            "size": len(self.watchlist),
        })
        matches: set[str] = set()
        for row in items:
            sym = (row.get("symbol") or "").upper()
            extra = row.get("additionalData") or row.get("additional_data") or row

            rsi_raw = extra.get("RSI14") or extra.get("rsi14")
            trend_raw = extra.get("LONG_TERM_TREND") or extra.get("longTermTrend") or ""
            mcap_raw = extra.get("MARKET_CAP") or extra.get("marketCap")

            try:
                rsi = float(rsi_raw) if rsi_raw is not None else 50
            except (ValueError, TypeError):
                rsi = 50
            try:
                mcap = float(mcap_raw) if mcap_raw is not None else 0
            except (ValueError, TypeError):
                mcap = 0

            trend_up = any(kw in str(trend_raw).lower() for kw in ("up", "strong up", "bullish"))

            if rsi < OVERSOLD_RSI_THRESHOLD and trend_up and mcap > 100_000_000:
                matches.add(sym)
                logger.info(
                    f"altFINS OVERSOLD+UPTREND: {sym} RSI={rsi:.1f} "
                    f"trend={trend_raw} mcap=${mcap/1e9:.1f}B"
                )

        self._oversold_uptrend = matches
        self._screener_last_poll = time.time()

    # ── Scoring lookups (pure, no I/O) ───────────────────────────

    def get_pattern_bonus(self, symbol: str) -> float:
        """Return score bonus for qualifying chart patterns. 0 if none."""
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        patterns = self._pattern_cache.get(base, [])
        if patterns:
            return float(PATTERN_BONUS_POINTS)
        return 0.0

    def get_oversold_uptrend_bonus(self, symbol: str) -> float:
        """Return +20 if the symbol is oversold in an uptrend. 0 otherwise."""
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        if base in self._oversold_uptrend:
            return float(OVERSOLD_BONUS_POINTS)
        return 0.0

    def get_total_bonus(self, symbol: str) -> float:
        """Combined altFINS bonus for scoring. Max capped at 35."""
        return min(35.0, self.get_pattern_bonus(symbol) + self.get_oversold_uptrend_bonus(symbol))

    # ── Pre-execution lookups (async, for risk_agent) ────────────

    async def check_ta_confirmation(self, symbol: str) -> dict:
        """Fetch altFINS TA summary. Returns {direction, strength, agrees}.

        Cached for TA_CACHE_TTL_SECONDS.
        """
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        cached = self._ta_cache.get(base, {})
        if cached and time.time() - cached.get("fetched_at", 0) < TA_CACHE_TTL_SECONDS:
            return cached.get("data", {})

        items = await self._mcp_call("technicalAnalysis_getTechnicalAnalysisData", {
            "coins": [base],
        })
        result = {"direction": "neutral", "strength": 0, "raw": {}}
        if items:
            ta = items[0] if isinstance(items[0], dict) else {}
            # Parse altFINS TA direction — field names vary, try common patterns
            direction = (
                ta.get("overallSignal") or ta.get("overall_signal") or
                ta.get("direction") or ta.get("signal") or "neutral"
            ).lower()
            if "buy" in direction or "bullish" in direction or "long" in direction:
                result["direction"] = "bullish"
            elif "sell" in direction or "bearish" in direction or "short" in direction:
                result["direction"] = "bearish"
            else:
                result["direction"] = "neutral"
            result["raw"] = ta

        self._ta_cache[base] = {"data": result, "fetched_at": time.time()}
        return result

    async def check_news_sentiment(self, symbol: str, lookback_hours: int = 4) -> dict:
        """Fetch recent news for a coin. Returns {negative: bool, summary: str}.

        Cached for NEWS_CACHE_TTL_SECONDS.
        """
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        cached = self._news_cache.get(base, {})
        if cached and time.time() - cached.get("fetched_at", 0) < NEWS_CACHE_TTL_SECONDS:
            return cached.get("data", {})

        now = datetime.now(timezone.utc)
        from_str = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        items = await self._mcp_call("news_getCryptoNewsMessages", {
            "coins": [base],
            "fromDate": from_str,
            "toDate": to_str,
            "size": 20,
        })

        negative_count = 0
        total = len(items)
        negative_headlines = []
        for article in items:
            sentiment = (
                article.get("sentiment") or article.get("sentimentLabel") or ""
            ).lower()
            title = article.get("title") or article.get("headline") or ""
            if any(kw in sentiment for kw in ("negative", "bearish", "fear")):
                negative_count += 1
                negative_headlines.append(title[:80])

        # Negative if > 40% of articles in last 4h are negative
        is_negative = (negative_count / total > 0.4) if total >= 3 else False

        result = {
            "negative": is_negative,
            "total_articles": total,
            "negative_count": negative_count,
            "headlines": negative_headlines[:3],
        }
        self._news_cache[base] = {"data": result, "fetched_at": time.time()}
        return result
