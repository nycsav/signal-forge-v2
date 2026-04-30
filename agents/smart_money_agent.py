"""Signal Forge v2 — Smart Money Agent (CMC DexScan Integration)

Polls CoinMarketCap's DEX API every 15 minutes for:
  - Trending tokens (smart money is chasing)
  - Top gainers (momentum already started)
  - Holder trend changes (accumulation vs distribution)
  - Liquidity changes (LPs adding = conviction)
  - Security checks (filter honeypots/rugs)

Cross-validates with existing whale triggers and email signals.
Publishes SmartMoneyEvent to EventBus at HIGH priority.

Schedule: every 15 minutes (aligned with whale per-asset scan).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from agents.event_bus import EventBus, Priority
from agents.events import SmartMoneyEvent
from config.settings import settings


# ── Constants ─────────────────────────────────────────────────

CMC_DEX_BASE = "https://pro-api.coinmarketcap.com"
SCAN_INTERVAL = 900  # 15 minutes
REQUEST_TIMEOUT = 30
MAX_TOKENS_PER_SCAN = 50

# Chains we care about (mapped to CMC network slugs)
PRIORITY_CHAINS = ["ethereum", "solana", "base", "arbitrum", "bsc"]

# Minimum thresholds to publish a signal
MIN_HOLDER_CHANGE_PCT = 5.0      # 5% holder count change to flag
MIN_LIQUIDITY_CHANGE_PCT = 10.0  # 10% liquidity change to flag
MIN_PRICE_CHANGE_PCT = 15.0      # 15% price move for gainers


class SmartMoneyAgent:
    """Polls CMC DEX API for smart money signals and publishes to EventBus."""

    def __init__(self, event_bus: EventBus, config: dict | None = None):
        self.bus = event_bus
        config = config or {}
        self.api_key = config.get("cmc_api_key", settings.cmc_api_key)
        self.enabled = bool(self.api_key)
        self.scan_interval = config.get("smart_money_scan_interval", SCAN_INTERVAL)

        # Track what we've already signaled to avoid spam
        self._signaled_tokens: dict[str, float] = {}  # token_address -> last_signal_ts
        self._signal_cooldown = 3600  # 1 hour cooldown per token

        # Latest scan results for external queries
        self._latest_trending: list[dict] = []
        self._latest_gainers: list[dict] = []
        self._latest_holder_alerts: list[dict] = []
        self._latest_scan_ts: float = 0

        if not self.enabled:
            logger.warning("SmartMoneyAgent: CMC API key not configured, running disabled")

    # ── HTTP Client ───────────────────────────────────────────

    async def _request(self, method: str, path: str, retries: int = 2, **kwargs) -> dict | list | None:
        """Make an authenticated request to CMC DEX API with retry on 500."""
        headers = {
            "X-CMC_PRO_API_KEY": self.api_key,
            "Accept": "application/json",
        }
        url = f"{CMC_DEX_BASE}{path}"

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    if method.upper() == "POST":
                        resp = await client.post(url, headers=headers, json=kwargs.get("json", {}))
                    else:
                        resp = await client.get(url, headers=headers, params=kwargs.get("params", {}))

                    if resp.status_code == 429:
                        logger.warning("SmartMoneyAgent: CMC rate limited, backing off 60s")
                        await asyncio.sleep(60)
                        return None

                    if resp.status_code != 200:
                        logger.debug(f"SmartMoneyAgent: CMC API {resp.status_code} on {path}")
                        return None

                    body = resp.json()

                    # CMC wraps responses in {"status": {...}, "data": ...}
                    status = body.get("status", {})
                    error_code = str(status.get("error_code", "0"))

                    if error_code == "500":
                        # "System is busy" — retry after backoff
                        if attempt < retries:
                            wait = 5 * (attempt + 1)
                            logger.debug(f"SmartMoneyAgent: CMC busy on {path}, retry in {wait}s ({attempt+1}/{retries})")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"SmartMoneyAgent: CMC busy on {path} after {retries} retries")
                        return None

                    if error_code not in ("0", "200"):
                        msg = status.get("error_message", "unknown")
                        logger.debug(f"SmartMoneyAgent: CMC error {error_code} on {path}: {msg}")
                        return None

                    return body.get("data", body)

            except Exception as e:
                logger.error(f"SmartMoneyAgent: request failed {path}: {e}")
                return None

        return None

    # ── Data Fetchers ─────────────────────────────────────────

    async def _fetch_trending(self) -> list[dict]:
        """Fetch trending DEX tokens across priority chains."""
        results = []
        data = await self._request("POST", "/v4/dex/token/trending", json={
            "limit": MAX_TOKENS_PER_SCAN,
        })
        if not data:
            return results

        tokens = data if isinstance(data, list) else data.get("tokens", data.get("results", []))
        for token in tokens[:MAX_TOKENS_PER_SCAN]:
            results.append({
                "name": token.get("name", ""),
                "symbol": token.get("symbol", ""),
                "address": token.get("address", token.get("contractAddress", "")),
                "chain": token.get("network", token.get("chain", "")),
                "price_usd": float(token.get("priceUsd", token.get("price", 0)) or 0),
                "price_change_24h": float(token.get("priceChange24h", token.get("percent_change_24h", 0)) or 0),
                "volume_24h": float(token.get("volume24h", token.get("volumeUsd24h", 0)) or 0),
                "market_cap": float(token.get("fdv", token.get("marketCap", 0)) or 0),
                "tx_count_24h": int(token.get("txCount24h", token.get("txns24h", 0)) or 0),
                "source": "cmc_trending",
            })

        logger.info(f"SmartMoneyAgent: {len(results)} trending tokens fetched")
        return results

    async def _fetch_gainers(self) -> list[dict]:
        """Fetch top gainers on DEXes."""
        results = []
        data = await self._request("POST", "/v4/dex/token/gainers-losers", json={
            "sort": "gainers",
            "limit": 30,
        })
        if not data:
            return results

        tokens = data if isinstance(data, list) else data.get("tokens", data.get("results", []))
        for token in tokens[:30]:
            pct = float(token.get("priceChange24h", token.get("percent_change_24h", 0)) or 0)
            if abs(pct) < MIN_PRICE_CHANGE_PCT:
                continue
            results.append({
                "name": token.get("name", ""),
                "symbol": token.get("symbol", ""),
                "address": token.get("address", token.get("contractAddress", "")),
                "chain": token.get("network", token.get("chain", "")),
                "price_usd": float(token.get("priceUsd", token.get("price", 0)) or 0),
                "price_change_24h": pct,
                "volume_24h": float(token.get("volume24h", token.get("volumeUsd24h", 0)) or 0),
                "market_cap": float(token.get("fdv", token.get("marketCap", 0)) or 0),
                "source": "cmc_gainer",
            })

        logger.info(f"SmartMoneyAgent: {len(results)} top gainers (>{MIN_PRICE_CHANGE_PCT}%)")
        return results

    async def _fetch_holder_trend(self, address: str, chain: str) -> dict | None:
        """Fetch holder count trend for a specific token."""
        data = await self._request("GET", "/v4/dex/token/holder/trend", params={
            "address": address,
            "network": chain,
        })
        if not data:
            return None
        return data

    async def _fetch_holder_count(self, address: str, chain: str) -> dict | None:
        """Fetch holder count and distribution for a token."""
        data = await self._request("GET", "/v4/dex/token/holder/count", params={
            "address": address,
            "network": chain,
        })
        if not data:
            return None
        return data

    async def _fetch_security(self, address: str, chain: str) -> dict | None:
        """Fetch Go+ security scan for a token contract."""
        data = await self._request("GET", "/v4/dex/token/security", params={
            "address": address,
            "network": chain,
        })
        if not data:
            return None
        return data

    async def _fetch_liquidity_changes(self) -> list[dict]:
        """Fetch recent significant liquidity changes."""
        results = []
        data = await self._request("GET", "/v4/dex/token/liquidity-change", params={
            "limit": 30,
            "sort": "change_desc",
        })
        if not data:
            return results

        items = data if isinstance(data, list) else data.get("tokens", data.get("results", []))
        for item in items[:30]:
            change_pct = float(item.get("liquidityChange", item.get("change_pct", 0)) or 0)
            if abs(change_pct) < MIN_LIQUIDITY_CHANGE_PCT:
                continue
            results.append({
                "name": item.get("name", ""),
                "symbol": item.get("symbol", ""),
                "address": item.get("address", ""),
                "chain": item.get("network", item.get("chain", "")),
                "liquidity_change_pct": change_pct,
                "liquidity_usd": float(item.get("liquidity", item.get("liquidityUsd", 0)) or 0),
                "source": "cmc_liquidity",
            })

        logger.info(f"SmartMoneyAgent: {len(results)} liquidity change alerts")
        return results

    async def _fetch_new_tokens(self) -> list[dict]:
        """Fetch newly listed tokens for early discovery."""
        results = []
        data = await self._request("POST", "/v4/dex/token/new", json={
            "limit": 20,
        })
        if not data:
            return results

        tokens = data if isinstance(data, list) else data.get("tokens", data.get("results", []))
        for token in tokens[:20]:
            vol = float(token.get("volume24h", token.get("volumeUsd24h", 0)) or 0)
            if vol < 50_000:  # Skip dust-volume tokens
                continue
            results.append({
                "name": token.get("name", ""),
                "symbol": token.get("symbol", ""),
                "address": token.get("address", token.get("contractAddress", "")),
                "chain": token.get("network", token.get("chain", "")),
                "price_usd": float(token.get("priceUsd", token.get("price", 0)) or 0),
                "price_change_24h": float(token.get("priceChange24h", 0) or 0),
                "volume_24h": vol,
                "market_cap": float(token.get("fdv", token.get("marketCap", 0)) or 0),
                "source": "cmc_new_token",
            })

        logger.info(f"SmartMoneyAgent: {len(results)} new tokens (vol > $50K)")
        return results

    # ── Signal Classification ─────────────────────────────────

    def _classify_signal(self, token: dict, security: dict | None = None,
                         holder_data: dict | None = None) -> SmartMoneyEvent | None:
        """Classify a token into a SmartMoneyEvent based on available data."""
        symbol = token.get("symbol", "").upper()
        address = token.get("address", "")
        chain = token.get("chain", "")
        source = token.get("source", "")
        now = datetime.now(timezone.utc)

        # Cooldown check
        cache_key = f"{chain}:{address}" if address else symbol
        last_signal = self._signaled_tokens.get(cache_key, 0)
        if time.time() - last_signal < self._signal_cooldown:
            return None

        # Security filter — reject if honeypot or dangerous contract
        is_safe = True
        security_flags = []
        if security:
            if security.get("is_honeypot") or security.get("honeypot"):
                security_flags.append("honeypot")
                is_safe = False
            if security.get("can_take_back_ownership") or security.get("owner_change_balance"):
                security_flags.append("owner_risk")
                is_safe = False
            if security.get("cannot_sell_all") or security.get("sell_tax", 0) > 10:
                security_flags.append("high_sell_tax")
                is_safe = False

        if not is_safe:
            logger.debug(f"SmartMoneyAgent: {symbol} rejected — security flags: {security_flags}")
            return None

        # Determine signal type and direction
        price_change = token.get("price_change_24h", 0)
        volume = token.get("volume_24h", 0)
        liq_change = token.get("liquidity_change_pct", 0)

        # Signal type classification
        if source == "cmc_liquidity" and liq_change > MIN_LIQUIDITY_CHANGE_PCT:
            signal_type = "liquidity_surge"
            direction = "bullish"
            confidence = min(0.85, 0.5 + (liq_change / 100))
            reason = f"Liquidity surged {liq_change:+.1f}% — LPs adding conviction"
        elif source == "cmc_gainer" and price_change > 50:
            signal_type = "momentum_breakout"
            direction = "bullish"
            confidence = min(0.80, 0.5 + (price_change / 200))
            reason = f"Price {price_change:+.1f}% in 24h — strong momentum"
        elif source == "cmc_gainer" and price_change > MIN_PRICE_CHANGE_PCT:
            signal_type = "momentum_move"
            direction = "bullish"
            confidence = min(0.70, 0.4 + (price_change / 150))
            reason = f"Price {price_change:+.1f}% in 24h"
        elif source == "cmc_trending" and volume > 1_000_000:
            signal_type = "smart_money_trending"
            direction = "bullish" if price_change > 0 else "bearish" if price_change < -5 else "neutral"
            confidence = 0.55 if volume > 5_000_000 else 0.45
            reason = f"Trending with ${volume/1e6:.1f}M volume"
        elif source == "cmc_new_token" and volume > 500_000:
            signal_type = "early_discovery"
            direction = "bullish" if price_change > 0 else "neutral"
            confidence = 0.40  # Low confidence — new and unproven
            reason = f"New token with ${volume/1e6:.1f}M volume — early discovery"
        else:
            return None

        # Holder trend boosts
        holder_change_pct = 0
        if holder_data:
            count = holder_data.get("holderCount", holder_data.get("count", 0))
            change = holder_data.get("holderChange24h", holder_data.get("change_24h", 0))
            if count and change:
                holder_change_pct = (change / count) * 100
                if holder_change_pct > MIN_HOLDER_CHANGE_PCT:
                    confidence = min(0.90, confidence + 0.10)
                    reason += f" | Holders +{holder_change_pct:.1f}%"
                    if signal_type == "smart_money_trending":
                        signal_type = "accumulation"
                elif holder_change_pct < -MIN_HOLDER_CHANGE_PCT:
                    if direction == "bullish":
                        confidence = max(0.20, confidence - 0.15)
                        reason += f" | WARNING: Holders {holder_change_pct:.1f}%"
                    else:
                        signal_type = "distribution"
                        direction = "bearish"
                        confidence = min(0.75, confidence + 0.10)
                        reason += f" | Holders declining {holder_change_pct:.1f}%"

        # Score bonus for cross-referencing with watchlist
        score_bonus = 0.0
        normalized_sym = symbol.replace("-USD", "").replace("USDT", "").replace("WETH", "ETH")
        watchlist_match = any(
            normalized_sym in w.replace("-USD", "")
            for w in settings.watchlist
        )
        if watchlist_match:
            score_bonus = 5.0
            confidence = min(0.95, confidence + 0.10)
            reason += " | WATCHLIST MATCH"

        self._signaled_tokens[cache_key] = time.time()

        return SmartMoneyEvent(
            timestamp=now,
            source="cmc_dexscan",
            signal_type=signal_type,
            symbols=[symbol],
            direction=direction,
            confidence=confidence,
            score_bonus=score_bonus,
            chain=chain,
            token_address=address,
            price_usd=token.get("price_usd", 0),
            price_change_24h=price_change,
            volume_24h=volume,
            market_cap=token.get("market_cap", 0),
            liquidity_change_pct=liq_change,
            holder_change_pct=holder_change_pct,
            security_flags=security_flags,
            reason=reason,
        )

    # ── Main Scan Loop ────────────────────────────────────────

    async def _scan(self):
        """Run a full smart money scan cycle."""
        if not self.enabled:
            return

        logger.info("SmartMoneyAgent: scan started")
        scan_start = time.time()
        events_published = 0

        # Fetch all data sources concurrently
        trending, gainers, liq_changes, new_tokens = await asyncio.gather(
            self._fetch_trending(),
            self._fetch_gainers(),
            self._fetch_liquidity_changes(),
            self._fetch_new_tokens(),
        )

        # Store for external queries
        self._latest_trending = trending
        self._latest_gainers = gainers
        self._latest_holder_alerts = liq_changes
        self._latest_scan_ts = time.time()

        # Combine all tokens, prioritizing gainers and liquidity changes
        all_tokens = liq_changes + gainers + trending + new_tokens

        # Deduplicate by address (keep first occurrence = highest priority)
        seen_addresses: set[str] = set()
        unique_tokens: list[dict] = []
        for token in all_tokens:
            addr = token.get("address", "")
            key = addr if addr else token.get("symbol", "")
            if key and key not in seen_addresses:
                seen_addresses.add(key)
                unique_tokens.append(token)

        # Classify and publish signals (limit security checks to top candidates)
        security_checks = 0
        for token in unique_tokens:
            # Only run security + holder checks on high-potential signals
            security = None
            holder_data = None
            address = token.get("address", "")
            chain = token.get("chain", "")

            if address and chain and security_checks < 10:
                security = await self._fetch_security(address, chain)
                holder_data = await self._fetch_holder_count(address, chain)
                security_checks += 1
                await asyncio.sleep(0.5)  # Rate limit courtesy

            event = self._classify_signal(token, security, holder_data)
            if event:
                await self.bus.publish(event, priority=Priority.HIGH)
                events_published += 1
                logger.info(
                    f"SMART MONEY [{event.direction.upper()}]: {event.symbols} "
                    f"type={event.signal_type} conf={event.confidence:.2f} — {event.reason}"
                )

        elapsed = time.time() - scan_start
        logger.info(
            f"SmartMoneyAgent: scan complete — {events_published} signals published "
            f"from {len(unique_tokens)} tokens in {elapsed:.1f}s"
        )

    async def run_forever(self):
        """Main loop: scan every 15 minutes."""
        if not self.enabled:
            logger.warning("SmartMoneyAgent: disabled (no CMC API key)")
            return

        logger.info(f"SmartMoneyAgent: started (interval={self.scan_interval}s)")

        # Initial scan on startup
        await self._safe_scan()

        while True:
            await asyncio.sleep(self.scan_interval)
            await self._safe_scan()

    async def _safe_scan(self):
        """Run scan with error handling."""
        try:
            await self._scan()
        except Exception as e:
            logger.error(f"SmartMoneyAgent: scan error: {e}")

    # ── External Query API ────────────────────────────────────

    def get_trending(self) -> list[dict]:
        """Return latest trending tokens for other agents to query."""
        return self._latest_trending

    def get_gainers(self) -> list[dict]:
        """Return latest top gainers."""
        return self._latest_gainers

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "scan_interval": self.scan_interval,
            "last_scan": datetime.fromtimestamp(self._latest_scan_ts).isoformat() if self._latest_scan_ts else "never",
            "trending_count": len(self._latest_trending),
            "gainers_count": len(self._latest_gainers),
            "signaled_tokens": len(self._signaled_tokens),
        }
