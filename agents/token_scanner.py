"""Signal Forge v2 — New Token Launch Scanner

Detects new token launches, filters scams, and identifies early opportunities.
Uses free APIs only: DEXScreener, GeckoTerminal, GoPlus Security, CoinGecko.

Pipeline: Detect → Filter (GoPlus) → Score → Signal
Runs every 30 seconds for new token detection.
"""

import asyncio
import json
import time
from datetime import datetime
from dataclasses import dataclass, field
from loguru import logger
import httpx

from db.live_repository import LiveRepository


@dataclass
class TokenOpportunity:
    symbol: str
    name: str
    address: str
    chain: str
    price: float
    liquidity_usd: float
    volume_24h: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    pair_age_hours: float
    buy_count: int
    sell_count: int
    source: str  # "dexscreener", "geckoterminal", "coingecko_trending"
    security_score: float  # 0-100 from GoPlus
    is_honeypot: bool
    signals: list[str] = field(default_factory=list)
    score: int = 0  # 0-100 composite score


class TokenScanner:
    """Scans for new token launches and filters for tradeable opportunities."""

    # Minimum requirements to consider a token
    MIN_LIQUIDITY_USD = 5_000
    MIN_HOLDERS = 10
    MAX_TOKEN_AGE_HOURS = 24
    MAX_SELL_TAX_PCT = 10
    MAX_BUY_TAX_PCT = 10
    MIN_SCORE = 60

    def __init__(self):
        self.repo = LiveRepository()
        self._seen_tokens: set[str] = set()  # Track already-seen addresses
        self._last_scan: float = 0

    async def scan_all_sources(self) -> list[TokenOpportunity]:
        """Run all detection sources and return filtered opportunities."""
        all_tokens = []

        # 1. DEXScreener — boosted tokens (teams paying for promotion = some legitimacy)
        boosted = await self._scan_dexscreener_boosted()
        all_tokens.extend(boosted)

        # 2. DEXScreener — latest token profiles
        profiles = await self._scan_dexscreener_profiles()
        all_tokens.extend(profiles)

        # 3. GeckoTerminal — new pools AND trending pools
        new_pools = await self._scan_geckoterminal_new()
        all_tokens.extend(new_pools)

        trending_pools = await self._scan_geckoterminal_trending()
        all_tokens.extend(trending_pools)

        # 4. CoinGecko — trending (catches tokens going viral before they peak)
        trending = await self._scan_coingecko_trending()
        all_tokens.extend(trending)

        # Deduplicate by address
        seen = set()
        unique = []
        for t in all_tokens:
            key = f"{t.chain}:{t.address}"
            if key not in seen and key not in self._seen_tokens:
                seen.add(key)
                unique.append(t)

        # Filter through GoPlus security (soft filter — no data = pass with lower score)
        filtered = []
        for token in unique[:15]:
            security = await self._check_goplus_security(token)
            if security:
                token.security_score = security.get("score", 0)
                token.is_honeypot = security.get("is_honeypot", False)
                if token.is_honeypot:
                    logger.debug(f"HONEYPOT: {token.symbol} ({token.chain}) — skipped")
                    continue
            else:
                # GoPlus returned no data — pass with lower confidence
                token.security_score = 40
                token.signals.append("GoPlus: no data (unverified — trade with caution)")
            filtered.append(token)
            self._score_token(token)
            await asyncio.sleep(0.7)

        # Sort by score
        filtered.sort(key=lambda t: t.score, reverse=True)

        # Log findings
        for t in filtered[:5]:
            self.repo.log("token_scan", f"NEW: {t.symbol} ({t.chain}) score={t.score} liq=${t.liquidity_usd:,.0f} {t.signals}")
            logger.info(f"Token opportunity: {t.symbol} ({t.chain}) score={t.score} liq=${t.liquidity_usd:,.0f} age={t.pair_age_hours:.1f}h")

        # Track seen tokens
        for t in unique:
            self._seen_tokens.add(f"{t.chain}:{t.address}")
        # Cap seen list
        if len(self._seen_tokens) > 5000:
            self._seen_tokens = set(list(self._seen_tokens)[-2500:])

        return filtered

    # ── DEXScreener ──

    async def _scan_dexscreener_boosted(self) -> list[TokenOpportunity]:
        """Boosted tokens with full pair data enrichment."""
        tokens = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.dexscreener.com/token-boosts/top/v1")
                if r.status_code != 200:
                    return tokens

                for item in r.json()[:10]:
                    chain = item.get("chainId", "")
                    addr = item.get("tokenAddress", "")
                    if not chain or not addr:
                        continue

                    # Enrich with pair data
                    try:
                        r2 = await client.get(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{addr}")
                        if r2.status_code == 200 and r2.json():
                            p = r2.json()[0]
                            base = p.get("baseToken", {})
                            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)

                            if liq < self.MIN_LIQUIDITY_USD:
                                continue

                            # Parse pair age
                            created_ms = p.get("pairCreatedAt", 0)
                            age_hours = (time.time() * 1000 - created_ms) / 3600000 if created_ms else 0

                            if age_hours > self.MAX_TOKEN_AGE_HOURS:
                                continue

                            txns = p.get("txns", {}).get("h1", {})
                            changes = p.get("priceChange", {})

                            token = TokenOpportunity(
                                symbol=base.get("symbol", addr[:8]),
                                name=base.get("name", "Unknown"),
                                address=addr,
                                chain=chain,
                                price=float(p.get("priceUsd", 0) or 0),
                                liquidity_usd=liq,
                                volume_24h=float(p.get("volume", {}).get("h24", 0) or 0),
                                price_change_5m=float(changes.get("m5", 0) or 0),
                                price_change_1h=float(changes.get("h1", 0) or 0),
                                price_change_24h=float(changes.get("h24", 0) or 0),
                                pair_age_hours=age_hours,
                                buy_count=int(txns.get("buys", 0) or 0),
                                sell_count=int(txns.get("sells", 0) or 0),
                                source="dexscreener_boost",
                                security_score=0, is_honeypot=False,
                            )
                            boosts = item.get("totalAmount", 0)
                            token.signals.append(f"DEXScreener boosted ({boosts} boosts)")
                            if token.buy_count > token.sell_count * 1.5:
                                token.signals.append(f"Buy pressure: {token.buy_count} buys vs {token.sell_count} sells (1h)")
                            tokens.append(token)
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)

            except Exception as e:
                logger.debug(f"DEXScreener boosted scan failed: {e}")
        return tokens

    async def _scan_dexscreener_profiles(self) -> list[TokenOpportunity]:
        """Latest token profiles — new tokens setting up their page."""
        tokens = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
                if r.status_code == 200:
                    for item in r.json()[:20]:
                        token = self._parse_dexscreener_profile(item)
                        if token:
                            token.signals.append("DEXScreener new profile")
                            tokens.append(token)
            except Exception as e:
                logger.debug(f"DEXScreener profiles scan failed: {e}")
        return tokens

    def _parse_dexscreener_boost(self, item: dict) -> TokenOpportunity | None:
        try:
            return TokenOpportunity(
                symbol=item.get("tokenAddress", "")[:8],
                name=item.get("description", "Unknown"),
                address=item.get("tokenAddress", ""),
                chain=item.get("chainId", "unknown"),
                price=0, liquidity_usd=0, volume_24h=0,
                price_change_5m=0, price_change_1h=0, price_change_24h=0,
                pair_age_hours=0, buy_count=0, sell_count=0,
                source="dexscreener_boost", security_score=0, is_honeypot=False,
            )
        except Exception:
            return None

    def _parse_dexscreener_profile(self, item: dict) -> TokenOpportunity | None:
        try:
            return TokenOpportunity(
                symbol=item.get("tokenAddress", "")[:8],
                name=item.get("description", "Unknown"),
                address=item.get("tokenAddress", ""),
                chain=item.get("chainId", "unknown"),
                price=0, liquidity_usd=0, volume_24h=0,
                price_change_5m=0, price_change_1h=0, price_change_24h=0,
                pair_age_hours=0, buy_count=0, sell_count=0,
                source="dexscreener_profile", security_score=0, is_honeypot=False,
            )
        except Exception:
            return None

    # ── GeckoTerminal ──

    async def _scan_geckoterminal_new(self) -> list[TokenOpportunity]:
        """New pools across all networks — catches fresh launches."""
        tokens = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.geckoterminal.com/api/v2/networks/new_pools",
                                     params={"page": 1})
                if r.status_code == 200:
                    pools = r.json().get("data", [])
                    for pool in pools[:15]:
                        token = self._parse_geckoterminal_pool(pool)
                        if token and token.liquidity_usd >= self.MIN_LIQUIDITY_USD:
                            token.signals.append("GeckoTerminal new pool")
                            tokens.append(token)
            except Exception as e:
                logger.debug(f"GeckoTerminal new pools failed: {e}")
        await asyncio.sleep(2)  # Rate limit (30/min)
        return tokens

    def _parse_geckoterminal_pool(self, pool: dict) -> TokenOpportunity | None:
        try:
            attrs = pool.get("attributes", {})
            name = attrs.get("name", "Unknown")
            address = attrs.get("address", "")
            chain = pool.get("relationships", {}).get("network", {}).get("data", {}).get("id", "unknown")

            # Parse price and volume
            price = float(attrs.get("base_token_price_usd") or 0)
            volume = float(attrs.get("volume_usd", {}).get("h24", 0) or 0)
            liquidity = float(attrs.get("reserve_in_usd") or 0)

            # Price changes
            changes = attrs.get("price_change_percentage", {})
            change_5m = float(changes.get("m5", 0) or 0)
            change_1h = float(changes.get("h1", 0) or 0)
            change_24h = float(changes.get("h24", 0) or 0)

            # Age
            created = attrs.get("pool_created_at", "")
            age_hours = 0
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_hours = (datetime.now(created_dt.tzinfo) - created_dt).total_seconds() / 3600
                except Exception:
                    pass

            # Buy/sell counts
            txns = attrs.get("transactions", {}).get("h1", {})
            buys = int(txns.get("buys", 0) or 0)
            sells = int(txns.get("sells", 0) or 0)

            if age_hours > self.MAX_TOKEN_AGE_HOURS:
                return None

            return TokenOpportunity(
                symbol=name.split("/")[0] if "/" in name else name[:10],
                name=name,
                address=address,
                chain=chain,
                price=price,
                liquidity_usd=liquidity,
                volume_24h=volume,
                price_change_5m=change_5m,
                price_change_1h=change_1h,
                price_change_24h=change_24h,
                pair_age_hours=age_hours,
                buy_count=buys,
                sell_count=sells,
                source="geckoterminal",
                security_score=0,
                is_honeypot=False,
            )
        except Exception:
            return None

    async def _scan_geckoterminal_trending(self) -> list[TokenOpportunity]:
        """Trending pools — higher quality than new pools, actively traded."""
        tokens = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.geckoterminal.com/api/v2/networks/trending_pools",
                                     params={"page": 1})
                if r.status_code == 200:
                    pools = r.json().get("data", [])
                    for pool in pools[:15]:
                        token = self._parse_geckoterminal_pool(pool)
                        if token and token.liquidity_usd >= self.MIN_LIQUIDITY_USD:
                            token.signals.append("GeckoTerminal trending pool")
                            if token.volume_24h > token.liquidity_usd * 5:
                                token.signals.append(f"High volume/liquidity ratio ({token.volume_24h/max(token.liquidity_usd,1):.0f}x)")
                            tokens.append(token)
            except Exception as e:
                logger.debug(f"GeckoTerminal trending failed: {e}")
        await asyncio.sleep(2)
        return tokens

    # ── CoinGecko Trending ──

    async def _scan_coingecko_trending(self) -> list[TokenOpportunity]:
        """CoinGecko trending — catches tokens going viral."""
        tokens = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.coingecko.com/api/v3/search/trending")
                if r.status_code == 200:
                    coins = r.json().get("coins", [])
                    for c in coins[:10]:
                        item = c.get("item", {})
                        if not isinstance(item, dict):
                            continue
                        data = item.get("data", {})
                        if not isinstance(data, dict):
                            data = {}

                        # Handle nested price change (can be dict or float)
                        pct_24h = data.get("price_change_percentage_24h", 0)
                        if isinstance(pct_24h, dict):
                            pct_24h = pct_24h.get("usd", 0)
                        pct_24h = float(pct_24h or 0)

                        vol = data.get("total_volume", 0)
                        if isinstance(vol, dict):
                            vol = vol.get("usd", 0)
                        if isinstance(vol, str):
                            vol = vol.replace("$", "").replace(",", "")
                        vol = float(vol or 0)

                        token = TokenOpportunity(
                            symbol=item.get("symbol", "???"),
                            name=item.get("name", "Unknown"),
                            address=item.get("id", ""),
                            chain="multi",
                            price=float(data.get("price", 0) or 0),
                            liquidity_usd=0,
                            volume_24h=vol,
                            price_change_5m=0, price_change_1h=0,
                            price_change_24h=pct_24h,
                            pair_age_hours=0,
                            buy_count=0, sell_count=0,
                            source="coingecko_trending",
                            security_score=70,
                            is_honeypot=False,
                        )
                        score_val = item.get("score", "?")
                        rank_str = str(score_val + 1) if isinstance(score_val, int) else "?"
                        token.signals.append(f"CoinGecko trending #{rank_str}")
                        mcr = data.get("market_cap_rank")
                        if mcr:
                            token.signals.append(f"Market cap rank #{mcr}")
                        tokens.append(token)
            except Exception as e:
                logger.debug(f"CoinGecko trending failed: {e}")
        return tokens

    # ── GoPlus Security ──

    async def _check_goplus_security(self, token: TokenOpportunity) -> dict | None:
        """Check token contract security via GoPlus API."""
        if not token.address or token.chain == "multi":
            return {"score": 50, "is_honeypot": False}  # CoinGecko trending tokens skip contract check

        chain_map = {
            "solana": "solana", "ethereum": "1", "eth": "1", "bsc": "56",
            "base": "8453", "arbitrum": "42161", "polygon": "137",
            "avalanche": "43114", "optimism": "10",
        }
        chain_id = chain_map.get(token.chain.lower(), token.chain)

        # GoPlus Solana endpoint is different
        if chain_id == "solana":
            endpoint = f"https://api.gopluslabs.io/api/v1/solana/token_security"
        else:
            endpoint = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(
                    endpoint,
                    params={"contract_addresses": token.address},
                )
                if r.status_code == 200:
                    data = r.json().get("result", {})
                    info = data.get(token.address.lower(), data.get(token.address, {}))
                    if not info:
                        return {"score": 30, "is_honeypot": False}

                    is_honeypot = info.get("is_honeypot", "1") == "1"
                    buy_tax = float(info.get("buy_tax", "0") or "0") * 100
                    sell_tax = float(info.get("sell_tax", "0") or "0") * 100
                    is_open_source = info.get("is_open_source", "0") == "1"
                    is_mintable = info.get("is_mintable", "1") == "1"
                    owner_change = info.get("can_take_back_ownership", "0") == "1"

                    # Score: start at 100, deduct for red flags
                    score = 100
                    if is_honeypot: score = 0
                    if sell_tax > self.MAX_SELL_TAX_PCT: score -= 30
                    if buy_tax > self.MAX_BUY_TAX_PCT: score -= 20
                    if not is_open_source: score -= 15
                    if is_mintable: score -= 10
                    if owner_change: score -= 15

                    if not is_honeypot:
                        if sell_tax <= 5:
                            token.signals.append(f"GoPlus: sell tax {sell_tax:.1f}% (OK)")
                        if is_open_source:
                            token.signals.append("GoPlus: source verified")

                    return {"score": max(0, score), "is_honeypot": is_honeypot,
                            "buy_tax": buy_tax, "sell_tax": sell_tax}
            except Exception as e:
                logger.debug(f"GoPlus check failed for {token.symbol}: {e}")
        return None

    # ── Scoring ──

    def _score_token(self, token: TokenOpportunity):
        """Score a token opportunity 0-100."""
        score = 40  # Base

        # Liquidity
        if token.liquidity_usd >= 50_000: score += 15
        elif token.liquidity_usd >= 20_000: score += 10
        elif token.liquidity_usd >= 10_000: score += 5

        # Volume
        if token.volume_24h >= 100_000: score += 10
        elif token.volume_24h >= 50_000: score += 5

        # Price momentum
        if token.price_change_1h > 20: score += 10
        elif token.price_change_1h > 5: score += 5
        if token.price_change_1h < -20: score -= 10

        # Buy/sell ratio
        if token.buy_count > 0 and token.sell_count > 0:
            ratio = token.buy_count / (token.buy_count + token.sell_count)
            if ratio > 0.6: score += 8  # More buyers than sellers
            elif ratio < 0.4: score -= 8

        # Security
        score += int(token.security_score * 0.1)  # 0-10 from security

        # Source bonus
        if token.source == "coingecko_trending": score += 10
        if token.source == "dexscreener_boost": score += 5

        # Age penalty (older = less opportunity)
        if token.pair_age_hours > 12: score -= 5
        if token.pair_age_hours > 18: score -= 5

        token.score = max(0, min(100, score))

    # ── Dashboard Data ──

    def get_dashboard_data(self, opportunities: list[TokenOpportunity]) -> dict:
        return {
            "scan_time": datetime.now().isoformat(),
            "total_found": len(opportunities),
            "opportunities": [
                {
                    "symbol": t.symbol,
                    "name": t.name,
                    "chain": t.chain,
                    "price": t.price,
                    "liquidity": t.liquidity_usd,
                    "volume_24h": t.volume_24h,
                    "change_1h": t.price_change_1h,
                    "change_24h": t.price_change_24h,
                    "age_hours": round(t.pair_age_hours, 1),
                    "buys": t.buy_count,
                    "sells": t.sell_count,
                    "security_score": t.security_score,
                    "score": t.score,
                    "source": t.source,
                    "signals": t.signals,
                }
                for t in opportunities[:20]
            ],
        }
