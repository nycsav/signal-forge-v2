"""Signal Forge v2 — Trending Token Day Trader

Monitors CoinGecko trending + GeckoTerminal trending for same-day trade opportunities.
Focuses on tokens tradeable on Alpaca with high momentum or oversold bounces.

Strategy:
  - Momentum: Trending + >5% move in 24h + high volume → ride the wave
  - Oversold bounce: Trending + declining >5% + established coin → contrarian entry
  - Exit: TP at +3-5%, stop at -3%, or end-of-day close

Scans every 15 minutes. Trades via existing live.py infrastructure.
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from config.settings import settings

# Coins tradeable on Alpaca (symbol → Alpaca format)
ALPACA_CRYPTO = {
    "BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "AVAX": "AVAX/USD",
    "LINK": "LINK/USD", "UNI": "UNI/USD", "AAVE": "AAVE/USD", "DOT": "DOT/USD",
    "DOGE": "DOGE/USD", "SHIB": "SHIB/USD", "ADA": "ADA/USD", "XRP": "XRP/USD",
    "LTC": "LTC/USD", "ATOM": "ATOM/USD", "NEAR": "NEAR/USD", "ARB": "ARB/USD",
    "OP": "OP/USD", "FIL": "FIL/USD", "INJ": "INJ/USD", "SUI": "SUI/USD",
    "APT": "APT/USD", "GRT": "GRT/USD", "CRV": "CRV/USD", "MKR": "MKR/USD",
    "COMP": "COMP/USD", "SNX": "SNX/USD", "MATIC": "MATIC/USD", "HYPE": "HYPE/USD",
    "TAO": "TAO/USD", "PENGU": "PENGU/USD", "ASTER": "ASTER/USD",
}


class TrendingTrader:
    """Scans trending lists and identifies same-day trade setups."""

    def __init__(self):
        self._last_trending: list = []

    async def scan(self) -> list[dict]:
        """Scan CoinGecko + GeckoTerminal trending and return actionable signals."""
        signals = []

        # CoinGecko trending
        cg_trending = await self._fetch_coingecko_trending()
        for coin in cg_trending:
            signal = self._evaluate_trending_coin(coin)
            if signal:
                signals.append(signal)

        # GeckoTerminal trending pools (for tokens with DEX pairs)
        gt_trending = await self._fetch_geckoterminal_trending()
        for pool in gt_trending:
            signal = self._evaluate_trending_pool(pool)
            if signal:
                signals.append(signal)

        # Sort by score
        signals.sort(key=lambda s: s["score"], reverse=True)

        self._last_trending = signals
        return signals

    async def _fetch_coingecko_trending(self) -> list[dict]:
        """Fetch CoinGecko trending coins with full data."""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.coingecko.com/api/v3/search/trending")
                if r.status_code != 200:
                    return []

                results = []
                for c in r.json().get("coins", []):
                    item = c.get("item", {})
                    if not isinstance(item, dict):
                        continue

                    data = item.get("data", {}) or {}
                    sym = (item.get("symbol") or "").upper()

                    # Parse price change safely
                    pct = data.get("price_change_percentage_24h", {})
                    if isinstance(pct, dict):
                        change_24h = float(pct.get("usd", 0) or 0)
                    else:
                        change_24h = float(pct or 0)

                    # Parse volume safely
                    vol = data.get("total_volume", 0)
                    if isinstance(vol, dict):
                        vol = vol.get("usd", 0)
                    if isinstance(vol, str):
                        vol = vol.replace("$", "").replace(",", "")
                    vol = float(vol or 0)

                    # Parse market cap
                    mcap = data.get("market_cap", 0)
                    if isinstance(mcap, str):
                        mcap = mcap.replace("$", "").replace(",", "")
                    elif isinstance(mcap, dict):
                        mcap = mcap.get("usd", 0)
                    mcap = float(mcap or 0)

                    # Parse price
                    price = data.get("price", 0)
                    if isinstance(price, str):
                        price = price.replace("$", "").replace(",", "")
                    price = float(price or 0)

                    results.append({
                        "symbol": sym,
                        "name": item.get("name", ""),
                        "rank": item.get("market_cap_rank") or data.get("market_cap_rank"),
                        "change_24h": change_24h,
                        "volume_24h": vol,
                        "market_cap": mcap,
                        "price": price,
                        "source": "coingecko_trending",
                        "tradeable": sym in ALPACA_CRYPTO,
                        "alpaca_symbol": ALPACA_CRYPTO.get(sym, ""),
                    })

                return results
            except Exception as e:
                logger.debug(f"CoinGecko trending fetch error: {e}")
                return []

    async def _fetch_geckoterminal_trending(self) -> list[dict]:
        """Fetch GeckoTerminal trending pools."""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get("https://api.geckoterminal.com/api/v2/networks/trending_pools")
                if r.status_code != 200:
                    return []

                results = []
                for p in r.json().get("data", [])[:15]:
                    a = p.get("attributes", {})
                    name = a.get("name", "")
                    base_sym = name.split("/")[0].strip().upper() if "/" in name else name[:10].upper()
                    chain = p.get("relationships", {}).get("network", {}).get("data", {}).get("id", "")
                    liq = float(a.get("reserve_in_usd") or 0)
                    vol = float(a.get("volume_usd", {}).get("h24", 0) or 0)
                    changes = a.get("price_change_percentage", {})
                    change_1h = float(changes.get("h1", 0) or 0)
                    change_24h = float(changes.get("h24", 0) or 0)
                    txns = a.get("transactions", {}).get("h1", {})
                    buys = int(txns.get("buys", 0) or 0)
                    sells = int(txns.get("sells", 0) or 0)

                    results.append({
                        "symbol": base_sym,
                        "name": name,
                        "chain": chain,
                        "liquidity": liq,
                        "volume_24h": vol,
                        "change_1h": change_1h,
                        "change_24h": change_24h,
                        "buys_1h": buys,
                        "sells_1h": sells,
                        "source": "geckoterminal_trending",
                        "tradeable": base_sym in ALPACA_CRYPTO,
                        "alpaca_symbol": ALPACA_CRYPTO.get(base_sym, ""),
                    })

                return results
            except Exception as e:
                logger.debug(f"GeckoTerminal trending error: {e}")
                return []

    def _evaluate_trending_coin(self, coin: dict) -> dict | None:
        """Evaluate a CoinGecko trending coin for a same-day trade."""
        sym = coin["symbol"]
        change = coin["change_24h"]
        vol = coin["volume_24h"]
        tradeable = coin["tradeable"]

        if not tradeable:
            return None  # Can't trade it on Alpaca

        score = 50
        strategy = "hold"
        signals = [f"CoinGecko trending — {coin['name']}"]
        reasons = []

        # Momentum play: strong upward move with volume
        if change > 10:
            score += 20
            strategy = "momentum_long"
            reasons.append(f"+{change:.1f}% in 24h — strong momentum")
        elif change > 5:
            score += 10
            strategy = "momentum_long"
            reasons.append(f"+{change:.1f}% in 24h — moderate momentum")

        # Oversold bounce: established coin declining while trending (people searching = interest)
        elif change < -5 and coin.get("rank") and int(coin["rank"]) < 100:
            score += 15
            strategy = "oversold_bounce"
            reasons.append(f"{change:.1f}% decline on top-100 coin while trending — bounce setup")

        # Volume confirmation
        if vol > 100_000_000:
            score += 10
            reasons.append(f"${vol/1e6:.0f}M volume — institutional interest")
        elif vol > 10_000_000:
            score += 5

        # Rank bonus (established = safer)
        rank = coin.get("rank")
        if rank and int(rank) < 50:
            score += 5
            signals.append(f"Top 50 by market cap (#{rank})")

        if score < 60 or strategy == "hold":
            return None

        return {
            "symbol": sym,
            "alpaca_symbol": coin["alpaca_symbol"],
            "name": coin["name"],
            "price": coin["price"],
            "change_24h": change,
            "volume_24h": vol,
            "market_cap": coin["market_cap"],
            "rank": rank,
            "score": score,
            "strategy": strategy,
            "signals": signals + reasons,
            "source": "coingecko_trending",
            "suggested_entry": coin["price"],
            "suggested_stop": coin["price"] * 0.97 if strategy == "momentum_long" else coin["price"] * 0.96,
            "suggested_tp": coin["price"] * 1.05 if strategy == "momentum_long" else coin["price"] * 1.04,
        }

    def _evaluate_trending_pool(self, pool: dict) -> dict | None:
        """Evaluate a GeckoTerminal trending pool."""
        sym = pool["symbol"]
        if not pool["tradeable"]:
            return None

        score = 50
        strategy = "hold"
        signals = [f"GeckoTerminal trending — {pool['name']}"]
        reasons = []

        change_1h = pool["change_1h"]
        change_24h = pool["change_24h"]
        buys = pool["buys_1h"]
        sells = pool["sells_1h"]

        # Strong 1h momentum
        if change_1h > 10:
            score += 15
            strategy = "momentum_long"
            reasons.append(f"+{change_1h:.1f}% in 1h — hot right now")
        elif change_1h > 3:
            score += 8
            strategy = "momentum_long"

        # Buy pressure
        if buys > 0 and sells > 0:
            ratio = buys / (buys + sells)
            if ratio > 0.65:
                score += 10
                reasons.append(f"Strong buy pressure: {buys}b vs {sells}s ({ratio:.0%})")
            elif ratio < 0.35:
                score -= 10
                reasons.append(f"Sell pressure: {buys}b vs {sells}s")

        # Liquidity
        if pool["liquidity"] > 100_000:
            score += 5
        elif pool["liquidity"] < 20_000:
            score -= 5

        if score < 60 or strategy == "hold":
            return None

        price = 0  # GeckoTerminal doesn't give us price directly for Alpaca
        return {
            "symbol": sym,
            "alpaca_symbol": pool["alpaca_symbol"],
            "name": pool["name"],
            "price": price,
            "change_1h": change_1h,
            "change_24h": change_24h,
            "volume_24h": pool["volume_24h"],
            "liquidity": pool["liquidity"],
            "buys_1h": buys,
            "sells_1h": sells,
            "score": score,
            "strategy": strategy,
            "signals": signals + reasons,
            "source": "geckoterminal_trending",
        }

    def get_dashboard_data(self) -> dict:
        return {
            "scan_time": datetime.now().isoformat(),
            "signals": self._last_trending,
            "total": len(self._last_trending),
            "tradeable": sum(1 for s in self._last_trending if s.get("alpaca_symbol")),
        }
