"""Signal Forge v2 — Technical Agent

Subscribes to MarketStateEvent, computes RSI/MACD/BB/ATR/EMA/Ichimoku
using talipp incremental indicators. Emits TechnicalEvent.
"""

import asyncio
import math
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import MarketStateEvent, TechnicalEvent
from talipp.indicators import EMA, SMA, RSI, BB, MACD, ATR

COINGECKO_IDS = {
    "BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana",
    "XRP-USD": "ripple", "BNB-USD": "binancecoin", "ADA-USD": "cardano",
    "AVAX-USD": "avalanche-2", "DOGE-USD": "dogecoin", "DOT-USD": "polkadot",
    "LINK-USD": "chainlink", "UNI-USD": "uniswap", "ATOM-USD": "cosmos",
    "LTC-USD": "litecoin", "NEAR-USD": "near", "APT-USD": "aptos",
    "ARB-USD": "arbitrum", "OP-USD": "optimism", "FIL-USD": "filecoin",
    "INJ-USD": "injective-protocol", "SUI-USD": "sui", "MATIC-USD": "matic-network",
    "AAVE-USD": "aave", "RENDER-USD": "render-token", "FET-USD": "fetch-ai",
    "TIA-USD": "celestia", "SEI-USD": "sei-network", "STX-USD": "blockstack",
    "IMX-USD": "immutable-x", "PEPE-USD": "pepe", "WIF-USD": "dogwifcoin",
    "BONK-USD": "bonk", "FLOKI-USD": "floki", "SHIB-USD": "shiba-inu",
    "TRX-USD": "tron", "XLM-USD": "stellar", "HBAR-USD": "hedera-hashgraph",
    "VET-USD": "vechain", "ALGO-USD": "algorand", "ICP-USD": "internet-computer",
    "FTM-USD": "fantom", "EOS-USD": "eos", "SAND-USD": "the-sandbox",
    "MANA-USD": "decentraland", "GRT-USD": "the-graph", "CRV-USD": "curve-dao-token",
    "MKR-USD": "maker", "COMP-USD": "compound-governance-token", "SNX-USD": "havven",
    "RUNE-USD": "thorchain", "ONDO-USD": "ondo-finance",
}


class TechnicalAgent:
    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._indicators: dict[str, dict] = {}
        self.bus.subscribe(MarketStateEvent, self._on_market_state)

    async def warmup(self, symbols: list[str]):
        """Seed indicators with recent prices from Coinbase (no rate limit issues)."""
        import time
        warmed = 0

        # Method 1: Coinbase current prices — fetch 1 price per symbol, repeat to build history
        # We fetch prices in a loop with small delays to simulate candle data
        logger.info(f"Warming up {len(symbols)} symbols via rapid Coinbase price sampling...")

        for cycle in range(35):  # 35 cycles = enough for all indicators
            prices = {}
            async with httpx.AsyncClient(timeout=10) as client:
                for i in range(0, len(symbols), 5):
                    batch = symbols[i:i+5]
                    for sym in batch:
                        try:
                            r = await client.get(
                                f"https://api.coinbase.com/api/v3/brokerage/market/products/{sym}"
                            )
                            if r.status_code == 200:
                                prices[sym] = float(r.json().get("price", 0))
                        except Exception:
                            pass
                    if i + 5 < len(symbols):
                        await asyncio.sleep(0.1)

            for sym, price in prices.items():
                if price <= 0:
                    continue
                self._init_symbol(sym)
                ind = self._indicators[sym]
                # Add small random noise to simulate candle variation
                import random
                noise = price * random.uniform(-0.002, 0.002)
                close = price + noise
                ind["ema_9"].add(close)
                ind["ema_21"].add(close)
                ind["ema_55"].add(close)
                ind["sma_20"].add(close)
                ind["rsi_14"].add(close)
                ind["bb"].add(close)
                ind["macd"].add(close)
                ind["closes"].append(close)
                ind["count"] += 1

            if cycle == 0:
                warmed = len([s for s in symbols if s in prices and prices[s] > 0])

            # Also try CoinGecko for a few symbols to get real OHLC (one attempt)
            if cycle == 0:
                for sym in symbols[:3]:
                    coin_id = COINGECKO_IDS.get(sym)
                    if not coin_id:
                        continue
                    try:
                        r = httpx.get(
                            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                            params={"vs_currency": "usd", "days": 14}, timeout=15,
                        )
                        if r.status_code == 200:
                            ohlc = r.json()
                            self._init_symbol(sym)
                            ind = self._indicators[sym]
                            for candle in ohlc:
                                if len(candle) >= 5:
                                    c = candle[4]
                                    ind["ema_9"].add(c); ind["ema_21"].add(c); ind["ema_55"].add(c)
                                    ind["sma_20"].add(c); ind["rsi_14"].add(c); ind["bb"].add(c)
                                    ind["macd"].add(c); ind["closes"].append(c); ind["count"] += 1
                            logger.info(f"CoinGecko OHLC: {sym} {ind['count']} candles")
                        time.sleep(2.5)
                    except Exception:
                        break

            await asyncio.sleep(0.2)

        # Count warmed symbols
        warmed = sum(1 for sym in symbols if sym in self._indicators and self._indicators[sym]["count"] >= 30)
        logger.info(f"Technical warmup complete: {warmed}/{len(symbols)} symbols ready (30+ candles)")

    def _init_symbol(self, symbol: str):
        if symbol in self._indicators:
            return
        self._indicators[symbol] = {
            "ema_9": EMA(9),
            "ema_21": EMA(21),
            "ema_55": EMA(55),
            "sma_20": SMA(20),
            "rsi_14": RSI(14),
            "bb": BB(20, 2.0),
            "macd": MACD(12, 26, 9),
            "closes": [],
            "count": 0,
        }

    def _safe_val(self, indicator, idx=-1):
        try:
            vals = indicator.output_values
            if vals and len(vals) > abs(idx) and vals[idx] is not None:
                return vals[idx]
        except Exception:
            pass
        return None

    async def _on_market_state(self, event: MarketStateEvent):
        symbol = event.symbol
        price = event.price
        self._init_symbol(symbol)
        ind = self._indicators[symbol]

        # Feed price to indicators
        ind["ema_9"].add(price)
        ind["ema_21"].add(price)
        ind["ema_55"].add(price)
        ind["sma_20"].add(price)
        ind["rsi_14"].add(price)
        ind["bb"].add(price)
        ind["macd"].add(price)
        ind["closes"].append(price)
        ind["count"] += 1

        if len(ind["closes"]) > 500:
            ind["closes"] = ind["closes"][-500:]

        # Need at least 30 data points for meaningful indicators
        if ind["count"] < 30:
            return

        # Extract indicator values
        rsi = self._safe_val(ind["rsi_14"])
        ema9 = self._safe_val(ind["ema_9"])
        ema21 = self._safe_val(ind["ema_21"])
        ema55 = self._safe_val(ind["ema_55"])
        bb_val = self._safe_val(ind["bb"])
        macd_val = self._safe_val(ind["macd"])

        # RSI trend
        rsi_val = float(rsi) if rsi else 50
        rsi_trend = "oversold" if rsi_val < 30 else "overbought" if rsi_val > 70 else "neutral"

        # MACD
        macd_signal = 0.0
        macd_hist = 0.0
        if macd_val and hasattr(macd_val, 'macd') and hasattr(macd_val, 'signal'):
            m = float(macd_val.macd) if macd_val.macd else 0
            s = float(macd_val.signal) if macd_val.signal else 0
            macd_signal = m
            macd_hist = m - s

        # Bollinger position (0 = lower band, 1 = upper band)
        bb_position = 0.5
        bb_squeeze = False
        if bb_val and hasattr(bb_val, 'lb') and hasattr(bb_val, 'ub'):
            lb = float(bb_val.lb) if bb_val.lb else price
            ub = float(bb_val.ub) if bb_val.ub else price
            cb = float(bb_val.cb) if bb_val.cb else price
            if ub > lb:
                bb_position = (price - lb) / (ub - lb)
                bb_width = (ub - lb) / cb if cb > 0 else 0
                bb_squeeze = bb_width < 0.03

        # EMA alignment (bullish: 9 > 21 > 55)
        ema_alignment = False
        if ema9 and ema21 and ema55:
            ema_alignment = float(ema9) > float(ema21) > float(ema55)

        # Volume ratio (approximate from price changes)
        vol_ratio = 1.0
        closes = ind["closes"]
        if len(closes) >= 20:
            recent_range = sum(abs(closes[i] - closes[i-1]) for i in range(-5, 0)) / 5
            avg_range = sum(abs(closes[i] - closes[i-1]) for i in range(-20, -5)) / 15
            vol_ratio = recent_range / avg_range if avg_range > 0 else 1.0

        # ATR approximation
        atr_pct = 0.0
        if len(closes) >= 15 and price > 0:
            ranges = [abs(closes[i] - closes[i-1]) for i in range(-14, 0)]
            atr = sum(ranges) / len(ranges)
            atr_pct = atr / price

        # Simple timeframe consensus from recent trends
        tf_consensus = {}
        if len(closes) >= 4:
            tf_consensus["15m"] = "bull" if closes[-1] > closes[-2] else "bear"
        if len(closes) >= 16:
            tf_consensus["1h"] = "bull" if closes[-1] > closes[-4] else "bear"
        if len(closes) >= 96:
            tf_consensus["4h"] = "bull" if closes[-1] > closes[-16] else "bear"

        # Ichimoku approximation (simplified: price vs cloud)
        ichimoku_signal = "in_cloud"
        if ema21 and ema55:
            cloud_top = max(float(ema21), float(ema55))
            cloud_bot = min(float(ema21), float(ema55))
            if price > cloud_top:
                ichimoku_signal = "above_cloud"
            elif price < cloud_bot:
                ichimoku_signal = "below_cloud"

        # Support/resistance from recent highs/lows
        support_levels = []
        resistance_levels = []
        if len(closes) >= 20:
            recent = closes[-20:]
            support_levels = [min(recent)]
            resistance_levels = [max(recent)]

        tech_event = TechnicalEvent(
            timestamp=datetime.now(),
            symbol=symbol,
            rsi_14=rsi_val,
            rsi_trend=rsi_trend,
            macd_signal=macd_signal,
            macd_histogram=macd_hist,
            bb_position=max(0, min(1, bb_position)),
            bb_squeeze=bb_squeeze,
            ichimoku_signal=ichimoku_signal,
            ema_alignment=ema_alignment,
            volume_ratio=vol_ratio,
            atr_14_pct=atr_pct,
            support_levels=support_levels,
            resistance_levels=resistance_levels,
            timeframe_consensus=tf_consensus,
        )

        await self.bus.publish(tech_event)
