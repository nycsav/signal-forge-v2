"""Signal Forge v2 — Composite Signal Scorer

Combines technical, sentiment, on-chain, and AI analyst scores
using configurable weights. Produces a 0-100 composite score.
"""

import json
from pathlib import Path
from loguru import logger
from agents.events import (
    MarketStateEvent, TechnicalEvent, SentimentEvent, OnChainEvent, Direction
)

WEIGHTS_PATH = Path(__file__).parent.parent / "config" / "weights.json"


class SignalScorer:
    def __init__(self):
        self.weights = self._load_weights()

    def _load_weights(self) -> dict:
        try:
            return json.loads(WEIGHTS_PATH.read_text())
        except Exception:
            logger.warning("Failed to load weights.json, using defaults")
            return {
                "components": {
                    "technical": {"weight": 0.35},
                    "sentiment": {"weight": 0.15},
                    "on_chain": {"weight": 0.10},
                    "ai_analyst": {"weight": 0.40},
                },
                "thresholds": {"min_score_to_propose": 55},
            }

    def reload_weights(self, new_weights: dict = None):
        if new_weights:
            self.weights["components"] = new_weights
        else:
            self.weights = self._load_weights()

    def score_technical(self, tech: TechnicalEvent) -> float:
        """Score technical indicators. Returns 0-100."""
        score = 50.0  # neutral baseline
        sw = self.weights["components"]["technical"].get("sub_weights", {})

        # RSI (0-100 input, score higher when oversold for longs)
        rsi = tech.rsi_14
        if rsi < 25:
            rsi_score = 85
        elif rsi < 30:
            rsi_score = 75
        elif rsi < 40:
            rsi_score = 60
        elif 40 <= rsi <= 60:
            rsi_score = 50
        elif rsi > 80:
            rsi_score = 15
        elif rsi > 70:
            rsi_score = 25
        else:
            rsi_score = 40

        # MACD
        macd_score = 50 + min(max(tech.macd_histogram * 500, -40), 40)

        # Bollinger position (0=lower band, 1=upper band)
        if tech.bb_position < 0.1:
            bb_score = 80  # near lower band = oversold
        elif tech.bb_position > 0.9:
            bb_score = 20  # near upper band = overbought
        else:
            bb_score = 50
        if tech.bb_squeeze:
            bb_score += 10  # squeeze = big move coming

        # EMA alignment
        ema_score = 75 if tech.ema_alignment else 35

        # Volume
        if tech.volume_ratio > 3:
            vol_score = 85
        elif tech.volume_ratio > 2:
            vol_score = 70
        elif tech.volume_ratio > 1.5:
            vol_score = 60
        elif tech.volume_ratio < 0.5:
            vol_score = 30
        else:
            vol_score = 50

        # Ichimoku
        ichi_map = {"above_cloud": 75, "in_cloud": 50, "below_cloud": 25}
        ichi_score = ichi_map.get(tech.ichimoku_signal, 50)

        # Timeframe consensus
        consensus = tech.timeframe_consensus
        bull_count = sum(1 for v in consensus.values() if v == "bull")
        tf_score = 50 + bull_count * 12.5 - (len(consensus) - bull_count) * 12.5
        tf_score = max(0, min(100, tf_score))

        # Weighted combination
        score = (
            rsi_score * sw.get("rsi", 0.20) +
            macd_score * sw.get("macd", 0.15) +
            bb_score * sw.get("bollinger", 0.15) +
            ema_score * sw.get("ema_alignment", 0.15) +
            vol_score * sw.get("volume", 0.15) +
            ichi_score * sw.get("ichimoku", 0.10) +
            tf_score * sw.get("timeframe_consensus", 0.10)
        )

        return max(0, min(100, score))

    def score_sentiment(self, sent: SentimentEvent) -> float:
        """Score sentiment data. Returns 0-100."""
        if not sent:
            return 50.0
        sw = self.weights["components"]["sentiment"].get("sub_weights", {})

        # Fear & Greed (contrarian: extreme fear = bullish)
        fg = sent.fear_greed
        if fg < 20:
            fg_score = 75  # extreme fear → contrarian buy
        elif fg < 35:
            fg_score = 60
        elif fg > 80:
            fg_score = 25  # extreme greed → sell signal
        elif fg > 65:
            fg_score = 40
        else:
            fg_score = 50

        # Sonar sentiment (-1 to 1)
        sonar_score = 50 + sent.sentiment_score * 40

        # Social volume change
        if sent.social_volume_change_pct > 50:
            social_score = 70
        elif sent.social_volume_change_pct > 20:
            social_score = 60
        elif sent.social_volume_change_pct < -30:
            social_score = 35
        else:
            social_score = 50

        score = (
            fg_score * sw.get("fear_greed", 0.40) +
            sonar_score * sw.get("sonar_sentiment", 0.35) +
            social_score * sw.get("social_volume", 0.25)
        )
        return max(0, min(100, score))

    def score_onchain(self, onchain: OnChainEvent) -> float:
        """Score on-chain data. Returns 0-100."""
        if not onchain:
            return 50.0
        sw = self.weights["components"]["on_chain"].get("sub_weights", {})

        # Whale flow (positive = accumulation)
        if onchain.whale_net_flow > 0:
            whale_score = 60 + min(onchain.whale_net_flow * 10, 30)
        else:
            whale_score = 40 + max(onchain.whale_net_flow * 10, -30)

        # Exchange flow (negative = coins leaving exchanges = bullish)
        if onchain.exchange_net_flow_24h_btc < -100:
            exch_score = 75
        elif onchain.exchange_net_flow_24h_btc < 0:
            exch_score = 60
        elif onchain.exchange_net_flow_24h_btc > 100:
            exch_score = 30
        else:
            exch_score = 50

        # Smart money signal (-1 to 1)
        smart_score = 50 + onchain.smart_money_signal * 40

        score = (
            whale_score * sw.get("whale_flow", 0.40) +
            exch_score * sw.get("exchange_flow", 0.30) +
            smart_score * sw.get("smart_money", 0.30)
        )
        return max(0, min(100, score))

    def composite_score(
        self,
        technical_score: float,
        sentiment_score: float,
        onchain_score: float,
        ai_score: float = 50.0,
        altfins_bonus: float = 0.0,
        fib_score_adj: float = 0.0,
        whale_confidence: float = 0.0,
        fg_boost: float = 1.0,
    ) -> tuple[float, dict]:
        """Combine all component scores into final 0-100 composite.

        ``altfins_bonus`` is an additive bonus (0-20) from the altFINS
        enrichment layer (chart patterns + oversold-in-uptrend filter).
        ``fib_score_adj`` is an additive adjustment (-10 to +10) from
        multi-timeframe Fibonacci analysis (golden pocket, confluence).
        ``whale_confidence`` (0-1) — if > 0.7, apply 1.20x multiplier.
        ``fg_boost`` — Fear & Greed multiplier (0.90 to 1.10).
        All applied after the weighted sum, before clamping.

        Returns (composite_score, breakdown_dict).
        """
        cw = self.weights["components"]
        composite = (
            technical_score * cw["technical"]["weight"] +
            sentiment_score * cw["sentiment"]["weight"] +
            onchain_score * cw["on_chain"]["weight"] +
            ai_score * cw["ai_analyst"]["weight"]
        )
        composite += altfins_bonus
        composite += fib_score_adj

        # Whale confidence boost: 1.20x when whale_confidence > 0.7
        whale_boost = 1.0
        if whale_confidence > 0.7:
            whale_boost = 1.20
            composite *= whale_boost

        # Fear & Greed overlay
        composite *= fg_boost

        composite = max(0, min(100, composite))

        breakdown = {
            "technical": round(technical_score, 1),
            "sentiment": round(sentiment_score, 1),
            "on_chain": round(onchain_score, 1),
            "ai_analyst": round(ai_score, 1),
            "whale_boost": whale_boost,
            "fg_boost": round(fg_boost, 2),
            "altfins_bonus": round(altfins_bonus, 1),
            "fib_adj": round(fib_score_adj, 1),
            "composite": round(composite, 1),
        }

        return composite, breakdown

    def score_to_direction(self, score: float) -> Direction:
        if score >= self.weights["thresholds"]["min_score_to_propose"]:
            return Direction.LONG
        elif score <= 100 - self.weights["thresholds"]["min_score_to_propose"]:
            return Direction.SHORT
        return Direction.FLAT
