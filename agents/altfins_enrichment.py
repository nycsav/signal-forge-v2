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

# Crossover signal types → bonus points
CROSSOVER_POLL_SECONDS = 15 * 60      # every 15 minutes
CROSSOVER_SIGNALS = {
    "SIGNALS_SUMMARY_SMA_50_200":       12,  # Golden/Death Cross
    "SIGNALS_SUMMARY_EMA_12_50":         8,
    "SIGNALS_SUMMARY_EMA_100_200":      10,
    "SIGNALS_SUMMARY_MACD_SIGNAL":       6,
    "SIGNALS_SUMMARY_RSI_14_CROSS_30":   8,  # RSI exits oversold
}
# Also match partial/lowercase key variants from altFINS response
CROSSOVER_KEY_MAP = {
    "sma_50_200":       12,
    "golden_cross":     12,
    "death_cross":      12,
    "ema_12_50":         8,
    "ema_100_200":      10,
    "macd_signal":       6,
    "rsi_14_cross_30":   8,
    "rsi_cross_30":      8,
}


class AltFINSEnrichment:
    """Background poller + in-memory cache for altFINS enrichment data."""

    def __init__(self, api_key: str, watchlist: list[str]):
        self.api_key = api_key
        self.watchlist = [s.replace("-USD", "").replace("/USD", "").upper() for s in watchlist]
        # Caches keyed by symbol (uppercase, no suffix)
        self._pattern_cache: dict[str, list[dict]] = {}     # symbol → [pattern, ...]
        self._oversold_uptrend: set[str] = set()             # symbols matching oversold+uptrend
        self._crossover_cache: dict[str, float] = {}         # symbol → total crossover bonus pts
        self._ta_cache: dict[str, dict] = {}                 # symbol → {data, fetched_at}
        self._news_cache: dict[str, dict] = {}               # symbol → {data, fetched_at}
        self._pattern_last_poll: float = 0
        self._screener_last_poll: float = 0
        self._crossover_last_poll: float = 0
        self._mcp_cache: dict[str, list[dict]] = {}  # backoff fallback cache

    # ── Background loops ─────────────────────────────────────────

    async def start(self):
        """Launch background polling tasks. Call once from the orchestrator."""
        asyncio.create_task(self._poll_patterns_loop())
        asyncio.create_task(self._poll_screener_loop())
        asyncio.create_task(self._poll_crossover_loop())
        logger.info("AltFINS enrichment started (patterns=4h, screener=15m, crossovers=15m)")

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

    async def _poll_crossover_loop(self):
        while True:
            try:
                await self._fetch_crossover_signals()
            except Exception as e:
                logger.debug(f"altFINS crossover poll error: {e}")
            await asyncio.sleep(CROSSOVER_POLL_SECONDS)

    # ── Data fetchers (MCP) ──────────────────────────────────────

    async def _mcp_call(self, tool_name: str, arguments: dict, max_retries: int = 3) -> list[dict]:
        """Single MCP tool call with exponential backoff on rate limits."""
        try:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
        except ImportError:
            return []

        last_error = None
        for attempt in range(max_retries):
            try:
                headers = {"X-Api-Key": self.api_key}
                async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments=arguments)
                        parsed = self._parse_result(result)
                        # Cache successful results for fallback
                        self._mcp_cache[f"{tool_name}_{hash(str(arguments))}"] = parsed
                        return parsed
            except Exception as e:
                last_error = e
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                if "rate" in str(e).lower() or "429" in str(e) or "limit" in str(e).lower():
                    logger.warning(f"altFINS rate limited ({tool_name}), retry {attempt+1}/{max_retries} in {wait}s")
                else:
                    logger.warning(f"altFINS error ({tool_name}): {e}, retry {attempt+1}/{max_retries} in {wait}s")
                await asyncio.sleep(wait)

        # All retries failed — return cached data if available
        cache_key = f"{tool_name}_{hash(str(arguments))}"
        cached = self._mcp_cache.get(cache_key, [])
        if cached:
            logger.warning(f"altFINS {tool_name}: retries exhausted, using cached ({len(cached)} items)")
            return cached
        logger.error(f"altFINS {tool_name}: retries exhausted, no cache: {last_error}")
        return []

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
            "size": 100,
        })
        if not items:
            logger.info("altFINS patterns: 0 results (endpoint may require paid tier)")
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
            "symbols": self.watchlist,
            "displayTypes": [
                "RSI14",
                "LONG_TERM_TREND",
                "MARKET_CAP",
            ],
            "numericFilters": [
                {"numericFilterType": "RSI14", "lteFilter": OVERSOLD_RSI_THRESHOLD},
                {"numericFilterType": "MARKET_CAP", "gteFilter": 100_000_000},
            ],
            "size": len(self.watchlist),
        })
        matches: set[str] = set()
        for row in items:
            sym = (row.get("symbol") or "").upper()
            extra = row.get("additionalData") or row.get("additional_data") or row

            rsi_raw = extra.get("RSI14") or extra.get("rsi14")
            trend_raw = str(extra.get("LONG_TERM_TREND") or extra.get("longTermTrend") or "").lower()
            mcap_raw = extra.get("MARKET_CAP") or extra.get("marketCap")

            try:
                rsi = float(rsi_raw) if rsi_raw is not None else 50
            except (ValueError, TypeError):
                rsi = 50
            try:
                mcap = float(str(mcap_raw).replace(",", "")) if mcap_raw is not None else 0
            except (ValueError, TypeError):
                mcap = 0

            # Trend format from altFINS: "Up (7/10)", "Strong Up (10/10)", etc.
            trend_up = "up" in trend_raw and "down" not in trend_raw

            if rsi < OVERSOLD_RSI_THRESHOLD and trend_up and mcap > 100_000_000:
                matches.add(sym)
                logger.info(
                    f"altFINS OVERSOLD+UPTREND: {sym} RSI={rsi:.1f} "
                    f"trend={trend_raw} mcap=${mcap/1e9:.1f}B"
                )

        if not matches:
            logger.info(f"altFINS screener: {len(items)} results, 0 match oversold+uptrend filter")
        self._oversold_uptrend = matches
        self._screener_last_poll = time.time()

    async def _fetch_crossover_signals(self):
        """Fetch signal_feed_data for BULLISH crossover signals (top 20 coins)."""
        from datetime import datetime as _dt
        now = _dt.now(timezone.utc)
        from_str = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        top20 = self.watchlist[:20]

        items = await self._mcp_call("signal_feed_data", {
            "symbols": top20,
            "signals": [
                "FRESH_MOMENTUM_MACD_SIGNAL_LINE_CROSSOVER",
                "EMA_12_50_CROSSOVERS",
                "MOMENTUM_RSI_CONFIRMATION",
            ],
            "direction": "BULLISH",
            "fromDate": from_str,
            "toDate": to_str,
            "size": 200,
        })

        new_cache: dict[str, float] = {}
        matched = 0
        for sig in items:
            sym = (sig.get("symbol") or "").upper()
            sig_key = (sig.get("signalKey") or sig.get("signal_key") or "").upper()
            sig_name = (sig.get("signalName") or sig.get("name") or "").lower()

            # Skip bearish signals (API may return both despite direction filter)
            if "bearish" in sig_name:
                continue

            # Match against known crossover signal types
            bonus = 0
            if sig_key in CROSSOVER_SIGNALS:
                bonus = CROSSOVER_SIGNALS[sig_key]
            else:
                # Try partial key matching
                for partial, pts in CROSSOVER_KEY_MAP.items():
                    if partial in sig_key.lower() or partial in sig_name:
                        bonus = pts
                        break

            if bonus > 0 and sym:
                new_cache[sym] = new_cache.get(sym, 0) + bonus
                matched += 1

        self._crossover_cache = new_cache
        self._crossover_last_poll = time.time()
        if matched:
            logger.info(f"altFINS crossovers: {matched} signals matched for {len(new_cache)} symbols")
        else:
            logger.info(f"altFINS crossovers: {len(items)} raw signals, 0 matched known crossover types")

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

    def get_crossover_bonus(self, symbol: str) -> float:
        """Return accumulated crossover signal bonus for this symbol."""
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        return self._crossover_cache.get(base, 0.0)

    def get_total_bonus(self, symbol: str) -> float:
        """Combined altFINS bonus for scoring. Max capped at 35."""
        total = (
            self.get_pattern_bonus(symbol) +
            self.get_oversold_uptrend_bonus(symbol) +
            self.get_crossover_bonus(symbol)
        )
        return min(35.0, total)

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
