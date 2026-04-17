"""Signal Forge v2 — Dynamic Watchlist Builder

Fetches top 300 tokens by market cap from CoinMarketCap, filters to
Coinbase-tradeable USD pairs, and caches the result.

Refreshes every 6 hours. Falls back to cached list on API failure.

Usage:
    from data.watchlist_builder import get_dynamic_watchlist
    watchlist = await get_dynamic_watchlist()  # returns ["BTC-USD", "ETH-USD", ...]
"""

import json
import time
from pathlib import Path
from loguru import logger
import httpx

CACHE_PATH = Path(__file__).parent.parent / "data" / "watchlist_cache.json"
CACHE_TTL_SECONDS = 6 * 3600  # 6 hours
CMC_LIMIT = 300  # Top 300 by market cap


async def _fetch_coinbase_pairs() -> set[str]:
    """Fetch all active USD trading pairs from Coinbase Exchange."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.exchange.coinbase.com/products")
            if r.status_code == 200:
                products = r.json()
                pairs = set()
                for p in products:
                    if (p.get("quote_currency") == "USD"
                            and p.get("status") == "online"
                            and not p.get("trading_disabled", False)):
                        pairs.add(p["id"])  # e.g. "BTC-USD"
                logger.info(f"Coinbase: {len(pairs)} active USD pairs")
                return pairs
    except Exception as e:
        logger.warning(f"Coinbase products fetch failed: {e}")
    return set()


async def _fetch_cmc_top_tokens(api_key: str, limit: int = CMC_LIMIT) -> list[str]:
    """Fetch top N tokens by market cap from CoinMarketCap. Returns symbols."""
    if not api_key:
        logger.warning("CMC API key not set — using fallback watchlist")
        return []

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
                headers={"X-CMC_PRO_API_KEY": api_key},
                params={"start": "1", "limit": str(limit), "convert": "USD",
                        "sort": "market_cap", "sort_dir": "desc"},
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                symbols = [coin["symbol"] for coin in data]
                logger.info(f"CMC: fetched top {len(symbols)} tokens by market cap")
                return symbols
    except Exception as e:
        logger.warning(f"CMC fetch failed: {e}")
    return []


async def _fetch_coingecko_top_tokens(limit: int = CMC_LIMIT) -> list[str]:
    """Fallback: fetch top tokens from CoinGecko (no API key needed)."""
    symbols = []
    per_page = 250
    pages = (limit // per_page) + 1

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for page in range(1, pages + 1):
                r = await client.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc",
                            "per_page": per_page, "page": page},
                )
                if r.status_code == 200:
                    for coin in r.json():
                        symbols.append(coin["symbol"].upper())
                else:
                    break
                if len(symbols) >= limit:
                    break
                time.sleep(1.5)  # CoinGecko rate limit
    except Exception as e:
        logger.warning(f"CoinGecko fetch failed: {e}")

    logger.info(f"CoinGecko: fetched {len(symbols)} tokens")
    return symbols[:limit]


def _load_cache() -> tuple[list[str], float]:
    """Load cached watchlist. Returns (watchlist, timestamp)."""
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text())
            return data.get("watchlist", []), data.get("timestamp", 0)
    except Exception:
        pass
    return [], 0


def _save_cache(watchlist: list[str]):
    """Save watchlist to cache."""
    try:
        CACHE_PATH.write_text(json.dumps({
            "watchlist": watchlist,
            "timestamp": time.time(),
            "count": len(watchlist),
        }, indent=2))
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")


async def get_dynamic_watchlist(cmc_api_key: str = "", force_refresh: bool = False) -> list[str]:
    """Get top 300 Coinbase-tradeable tokens by market cap.

    Strategy:
    1. Check cache (6h TTL)
    2. Fetch Coinbase active USD pairs
    3. Fetch CMC top 300 (or CoinGecko fallback)
    4. Intersect: only tokens tradeable on Coinbase
    5. Return sorted by CMC rank (market cap descending)
    """
    # Check cache
    if not force_refresh:
        cached, ts = _load_cache()
        if cached and (time.time() - ts) < CACHE_TTL_SECONDS:
            logger.info(f"Watchlist: using cached list ({len(cached)} tokens, "
                        f"{(time.time() - ts) / 3600:.1f}h old)")
            return cached

    # Fetch Coinbase pairs
    coinbase_pairs = await _fetch_coinbase_pairs()
    if not coinbase_pairs:
        # Fall back to cache even if stale
        cached, _ = _load_cache()
        if cached:
            logger.warning(f"Coinbase fetch failed — using stale cache ({len(cached)} tokens)")
            return cached
        logger.error("No Coinbase pairs and no cache — using hardcoded top 50")
        return []  # caller will use settings.watchlist as fallback

    # Fetch top tokens by market cap
    top_symbols = await _fetch_cmc_top_tokens(cmc_api_key)
    if not top_symbols:
        top_symbols = await _fetch_coingecko_top_tokens()

    if not top_symbols:
        # Last resort: use all Coinbase pairs
        watchlist = sorted(coinbase_pairs)
        _save_cache(watchlist)
        return watchlist

    # Intersect: CMC tokens that are tradeable on Coinbase, preserving CMC rank order
    coinbase_symbols = {p.split("-")[0] for p in coinbase_pairs}
    watchlist = []
    seen = set()
    for sym in top_symbols:
        if sym in coinbase_symbols and sym not in seen:
            watchlist.append(f"{sym}-USD")
            seen.add(sym)

    # Add any Coinbase pairs not in CMC top 300 (smaller tokens) at the end
    # This ensures we don't miss newly listed tokens
    for pair in sorted(coinbase_pairs):
        if pair not in seen and pair.endswith("-USD"):
            base = pair.split("-")[0]
            if base not in seen:
                watchlist.append(pair)
                seen.add(base)

    _save_cache(watchlist)
    logger.info(f"Watchlist built: {len(watchlist)} tokens "
                f"({len([w for w in watchlist if w.split('-')[0] in {s for s in top_symbols[:300]}])} "
                f"from CMC top 300, rest from Coinbase)")
    return watchlist
