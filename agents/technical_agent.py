"""Signal Forge v2 — Technical Agent

Subscribes to MarketStateEvent, computes RSI/MACD/BB/ATR/EMA/Ichimoku
using talipp incremental indicators. Emits TechnicalEvent.
"""

import math
from datetime import datetime
from loguru import logger

from agents.event_bus import EventBus
from agents.events import MarketStateEvent, TechnicalEvent
from talipp.indicators import EMA, SMA, RSI, BB, MACD, ATR


class TechnicalAgent:
    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._indicators: dict[str, dict] = {}
        self.bus.subscribe(MarketStateEvent, self._on_market_state)

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
