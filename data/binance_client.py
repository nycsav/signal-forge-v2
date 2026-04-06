"""Signal Forge v2 — Binance Data Client

Primary OHLCV source. 16 granularities (1s to 1M), 1000 candles/request, free.
Uses ccxt for clean abstraction across exchanges.
"""

import asyncio
from datetime import datetime, timedelta
from loguru import logger

try:
    import ccxt.async_support as ccxt_async
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False
    logger.warning("ccxt not installed — Binance client disabled")


class BinanceClient:
    TIMEFRAMES = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h",
        "12h": "12h", "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
    }

    def __init__(self):
        if HAS_CCXT:
            self.exchange = ccxt_async.binance({"enableRateLimit": True})
        else:
            self.exchange = None

    async def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[dict]:
        """Fetch OHLCV candles. Symbol format: BTC/USDT or BTC-USD (auto-converted)."""
        if not self.exchange:
            return []

        # Convert Signal Forge format to Binance format
        binance_sym = symbol.replace("-USD", "/USDT").replace("-", "/")
        if not binance_sym.endswith("/USDT") and "/" not in binance_sym:
            binance_sym = f"{binance_sym}/USDT"

        try:
            ohlcv = await self.exchange.fetch_ohlcv(binance_sym, timeframe, limit=limit)
            return [
                {
                    "timestamp": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                }
                for c in ohlcv
            ]
        except Exception as e:
            logger.debug(f"Binance OHLCV failed for {binance_sym}: {e}")
            return []

    async def get_multi_timeframe(self, symbol: str, timeframes: list[str] = None, limit: int = 100) -> dict:
        """Fetch candles across multiple timeframes for one symbol."""
        timeframes = timeframes or ["15m", "1h", "4h", "1d"]
        result = {}
        for tf in timeframes:
            candles = await self.get_ohlcv(symbol, tf, limit)
            if candles:
                result[tf] = candles
            await asyncio.sleep(0.1)  # Rate limit courtesy
        return result

    async def warmup_indicators(self, symbols: list[str], timeframe: str = "4h", limit: int = 100) -> dict:
        """Fetch historical candles for multiple symbols — for technical indicator warmup."""
        result = {}
        for sym in symbols:
            candles = await self.get_ohlcv(sym, timeframe, limit)
            if candles:
                result[sym] = candles
                logger.debug(f"Binance warmup: {sym} {len(candles)} candles ({timeframe})")
            await asyncio.sleep(0.15)
        return result

    async def get_orderbook(self, symbol: str, limit: int = 20) -> dict:
        """Fetch order book depth."""
        if not self.exchange:
            return {}
        binance_sym = symbol.replace("-USD", "/USDT").replace("-", "/")
        if not binance_sym.endswith("/USDT") and "/" not in binance_sym:
            binance_sym = f"{binance_sym}/USDT"
        try:
            book = await self.exchange.fetch_order_book(binance_sym, limit)
            return {
                "bids": book.get("bids", [])[:limit],
                "asks": book.get("asks", [])[:limit],
                "spread_pct": (book["asks"][0][0] - book["bids"][0][0]) / book["bids"][0][0] * 100 if book.get("bids") and book.get("asks") else 0,
            }
        except Exception as e:
            logger.debug(f"Binance orderbook failed for {binance_sym}: {e}")
            return {}

    async def close(self):
        if self.exchange:
            await self.exchange.close()
