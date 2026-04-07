"""Signal Forge — Multi-Agent Analyst System

7 specialist analyst agents, each testing different probability angles.
They vote independently, then a Risk Judge makes the final call.

Agents:
  1. TrendAnalyst      — EMA crossovers, ADX, trend strength
  2. MomentumAnalyst   — RSI, multi-period returns, volume momentum
  3. ReversionAnalyst  — Bollinger Bands, Z-score, mean reversion setups
  4. VolumeAnalyst     — Volume spikes, accumulation/distribution, OBV
  5. SentimentAnalyst  — Fear & Greed, market regime, macro context
  6. PatternAnalyst    — Support/resistance, breakout detection, price patterns
  7. RiskAnalyst       — Volatility regime, correlation, drawdown risk

Pipeline: All 7 analysts vote → weighted combination → Risk Judge override → final signal
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AnalystVote:
    """A single analyst's verdict."""
    agent_name: str
    signal: float       # -1.0 (strong sell) to +1.0 (strong buy)
    confidence: float   # 0.0 to 1.0
    reasoning: str
    data: dict = field(default_factory=dict)  # Raw indicator values


@dataclass
class ConsensusResult:
    """Combined result from all analysts."""
    score: int              # 0-100 (Signal Forge compatible)
    action: str             # BUY, SELL, HOLD, SKIP
    consensus_signal: float # -1 to +1 weighted average
    total_confidence: float
    votes: list             # List of AnalystVote
    risk_override: bool     # True if Risk Judge blocked the trade
    risk_reason: str
    bull_count: int
    bear_count: int
    neutral_count: int


class TrendAnalyst:
    """Tests probability: Is the trend our friend?"""
    NAME = "Trend"
    WEIGHT = 0.20

    def analyze(self, closes: list, highs: list = None, lows: list = None) -> AnalystVote:
        if len(closes) < 55:
            return AnalystVote(self.NAME, 0, 0.1, "Insufficient data", {})

        # EMA calculations
        ema8 = self._ema(closes, 8)
        ema21 = self._ema(closes, 21)
        ema55 = self._ema(closes, 55)

        # Trend alignment
        short_trend = 1 if ema8 > ema21 else -1
        medium_trend = 1 if ema21 > ema55 else -1
        long_aligned = ema8 > ema21 > ema55

        # ADX approximation (trend strength from directional movement)
        adx = self._approx_adx(closes, highs or closes, lows or closes, 14)

        signal = short_trend * 0.5 + medium_trend * 0.5
        if long_aligned:
            signal = 0.8 if signal > 0 else signal
        elif ema8 < ema21 < ema55:
            signal = -0.8

        confidence = min(adx / 50.0, 1.0) if adx else 0.3

        reasons = []
        if long_aligned:
            reasons.append("All EMAs aligned bullish (8>21>55)")
        elif ema8 < ema21 < ema55:
            reasons.append("All EMAs aligned bearish (8<21<55)")
        if adx and adx > 25:
            reasons.append(f"Strong trend (ADX={adx:.0f})")
        elif adx:
            reasons.append(f"Weak trend (ADX={adx:.0f})")

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons) or "Neutral trend",
            {"ema8": ema8, "ema21": ema21, "ema55": ema55, "adx": adx},
        )

    def _ema(self, data, period):
        if len(data) < period:
            return data[-1] if data else 0
        mult = 2.0 / (period + 1)
        ema = sum(data[:period]) / period
        for val in data[period:]:
            ema = (val - ema) * mult + ema
        return ema

    def _approx_adx(self, closes, highs, lows, period):
        if len(closes) < period + 1:
            return None
        # Simplified: use average absolute change as trend strength proxy
        changes = [abs(closes[i] - closes[i-1]) / closes[i-1] for i in range(-period, 0)]
        return sum(changes) / len(changes) * 1000  # Scale to ~0-50 range


class MomentumAnalyst:
    """Tests probability: Is momentum accelerating or fading?"""
    NAME = "Momentum"
    WEIGHT = 0.20

    def analyze(self, closes: list, volumes: list = None) -> AnalystVote:
        if len(closes) < 21:
            return AnalystVote(self.NAME, 0, 0.1, "Insufficient data", {})

        price = closes[-1]

        # RSI
        rsi = self._rsi(closes, 14)

        # Multi-period returns
        ret_5d = (price - closes[-5]) / closes[-5] if len(closes) >= 5 else 0
        ret_21d = (price - closes[-21]) / closes[-21] if len(closes) >= 21 else 0

        # Volume momentum
        vol_ratio = 1.0
        if volumes and len(volumes) >= 21:
            avg_vol = sum(volumes[-21:]) / 21
            vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        # Signal
        momentum_raw = ret_5d * 0.6 + ret_21d * 0.4
        signal = max(-1, min(1, momentum_raw * 10))

        # RSI adjustment
        if rsi < 30:
            signal = max(signal, 0.3)  # Oversold bounce potential
        elif rsi > 70:
            signal = min(signal, -0.3)  # Overbought reversal risk

        # Volume confirmation
        confidence = 0.4
        if vol_ratio > 2.0 and signal > 0:
            confidence = 0.8  # Volume confirms bullish momentum
        elif vol_ratio > 2.0 and signal < 0:
            confidence = 0.8  # Volume confirms selling
        elif vol_ratio < 0.5:
            confidence = 0.2  # Low volume = low conviction

        reasons = []
        reasons.append(f"RSI={rsi:.0f}")
        if ret_5d > 0.03:
            reasons.append(f"5d up {ret_5d*100:.1f}%")
        elif ret_5d < -0.03:
            reasons.append(f"5d down {ret_5d*100:.1f}%")
        if vol_ratio > 2:
            reasons.append(f"Volume spike {vol_ratio:.1f}x")

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons),
            {"rsi": rsi, "ret_5d": ret_5d, "ret_21d": ret_21d, "vol_ratio": vol_ratio},
        )

    def _rsi(self, closes, period):
        if len(closes) < period + 1:
            return 50
        changes = [closes[i] - closes[i-1] for i in range(-period, 0)]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))


class ReversionAnalyst:
    """Tests probability: Is price at an extreme that will snap back?"""
    NAME = "Reversion"
    WEIGHT = 0.15

    def analyze(self, closes: list) -> AnalystVote:
        if len(closes) < 20:
            return AnalystVote(self.NAME, 0, 0.1, "Insufficient data", {})

        price = closes[-1]
        window = closes[-20:]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window))

        # Bollinger Bands
        upper = mean + 2 * std
        lower = mean - 2 * std

        # Z-score
        z_score = (price - mean) / std if std > 0 else 0

        signal = 0.0
        confidence = 0.3
        reasons = []

        if price < lower:
            signal = 0.6  # Below lower BB = oversold
            confidence = 0.7
            reasons.append(f"Below lower BB (z={z_score:.1f})")
        elif price > upper:
            signal = -0.6  # Above upper BB = overbought
            confidence = 0.7
            reasons.append(f"Above upper BB (z={z_score:.1f})")
        elif abs(z_score) < 0.5:
            reasons.append("Near mean (neutral)")

        # Squeeze detection (low volatility = big move coming)
        bb_width = (upper - lower) / mean if mean > 0 else 0
        if bb_width < 0.03:
            confidence = min(confidence + 0.2, 1.0)
            reasons.append(f"BB squeeze (width={bb_width:.3f})")

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons) or "Within normal range",
            {"z_score": z_score, "bb_upper": upper, "bb_lower": lower, "bb_width": bb_width},
        )


class VolumeAnalyst:
    """Tests probability: Is smart money accumulating or distributing?"""
    NAME = "Volume"
    WEIGHT = 0.15

    def analyze(self, closes: list, volumes: list = None) -> AnalystVote:
        if not volumes or len(volumes) < 10 or len(closes) < 10:
            return AnalystVote(self.NAME, 0, 0.1, "No volume data", {})

        # On-Balance Volume trend
        obv_trend = self._obv_trend(closes[-20:], volumes[-20:])

        # Volume-price divergence
        price_up = closes[-1] > closes[-5] if len(closes) >= 5 else False
        vol_up = sum(volumes[-5:]) > sum(volumes[-10:-5]) if len(volumes) >= 10 else False

        # Accumulation/distribution signal
        signal = 0.0
        reasons = []

        if price_up and vol_up:
            signal = 0.5
            reasons.append("Price up + volume up (healthy)")
        elif price_up and not vol_up:
            signal = -0.2
            reasons.append("Price up on declining volume (weak)")
        elif not price_up and vol_up:
            signal = -0.4
            reasons.append("Price down on rising volume (distribution)")
        elif not price_up and not vol_up:
            signal = 0.1
            reasons.append("Price down on low volume (capitulation fading)")

        if obv_trend > 0:
            signal += 0.2
            reasons.append("OBV trending up")
        elif obv_trend < 0:
            signal -= 0.2
            reasons.append("OBV trending down")

        signal = max(-1, min(1, signal))

        return AnalystVote(
            self.NAME, signal, 0.5,
            "; ".join(reasons),
            {"obv_trend": obv_trend, "price_up": price_up, "vol_up": vol_up},
        )

    def _obv_trend(self, closes, volumes):
        if len(closes) < 2:
            return 0
        obv = 0
        obvs = []
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
            obvs.append(obv)
        if len(obvs) < 5:
            return 0
        recent = sum(obvs[-5:]) / 5
        older = sum(obvs[:5]) / 5
        return 1 if recent > older else -1 if recent < older else 0


class SentimentAnalyst:
    """Tests probability: What's the market mood telling us?"""
    NAME = "Sentiment"
    WEIGHT = 0.10

    def analyze(self, fear_greed: int = 50, market_trend: str = "neutral") -> AnalystVote:
        signal = 0.0
        confidence = 0.4
        reasons = []

        # Contrarian F&G interpretation
        if fear_greed < 20:
            signal = 0.5  # Extreme fear = contrarian buy
            confidence = 0.6
            reasons.append(f"Extreme fear ({fear_greed}) — contrarian buy")
        elif fear_greed < 35:
            signal = 0.3
            reasons.append(f"Fear ({fear_greed}) — accumulation zone")
        elif fear_greed > 80:
            signal = -0.5  # Extreme greed = sell signal
            confidence = 0.6
            reasons.append(f"Extreme greed ({fear_greed}) — distribution risk")
        elif fear_greed > 65:
            signal = -0.2
            reasons.append(f"Greed ({fear_greed}) — caution")
        else:
            reasons.append(f"Neutral sentiment ({fear_greed})")

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons),
            {"fear_greed": fear_greed},
        )


class PatternAnalyst:
    """Tests probability: Are there actionable price patterns?"""
    NAME = "Pattern"
    WEIGHT = 0.10

    def analyze(self, closes: list, highs: list = None, lows: list = None) -> AnalystVote:
        if len(closes) < 30:
            return AnalystVote(self.NAME, 0, 0.1, "Insufficient data", {})

        highs = highs or closes
        lows = lows or closes
        price = closes[-1]

        signal = 0.0
        confidence = 0.3
        reasons = []

        # Support/Resistance from recent highs/lows
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        price_range = recent_high - recent_low if recent_high > recent_low else 1

        # Near support = bullish, near resistance = bearish
        position_in_range = (price - recent_low) / price_range

        if position_in_range < 0.2:
            signal = 0.4
            confidence = 0.5
            reasons.append(f"Near support (${recent_low:.2f})")
        elif position_in_range > 0.8:
            signal = -0.3
            confidence = 0.5
            reasons.append(f"Near resistance (${recent_high:.2f})")

        # Breakout detection: price making new highs with conviction
        if price >= recent_high * 0.99:
            prev_high = max(highs[-40:-20]) if len(highs) >= 40 else recent_high
            if recent_high > prev_high:
                signal = 0.6
                confidence = 0.6
                reasons.append("Breakout: new high above prior range")

        # Higher lows pattern (uptrend structure)
        if len(lows) >= 20:
            low1 = min(lows[-20:-10])
            low2 = min(lows[-10:])
            if low2 > low1:
                signal += 0.2
                reasons.append("Higher lows forming")
            elif low2 < low1:
                signal -= 0.2
                reasons.append("Lower lows forming")

        signal = max(-1, min(1, signal))

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons) or "No clear pattern",
            {"support": recent_low, "resistance": recent_high, "range_position": position_in_range},
        )


class RiskAnalyst:
    """Tests probability: What could go wrong? Acts as Risk Judge."""
    NAME = "Risk"
    WEIGHT = 0.10

    def analyze(self, closes: list, portfolio_value: float = 100000,
                open_positions: int = 0, max_positions: int = 5) -> AnalystVote:
        if len(closes) < 20:
            return AnalystVote(self.NAME, 0, 0.5, "Insufficient data for risk assessment", {})

        # Volatility (annualized from recent data)
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-min(20, len(closes)-1), 0)]
        vol = math.sqrt(sum(r**2 for r in returns) / len(returns)) * math.sqrt(365) if returns else 0

        # Max drawdown in window
        peak = closes[0]
        max_dd = 0
        for p in closes:
            if p > peak:
                peak = p
            dd = (peak - p) / peak
            if dd > max_dd:
                max_dd = dd

        # Position capacity
        at_capacity = open_positions >= max_positions

        signal = 0.0
        confidence = 0.5
        reasons = []
        risk_flags = []

        # High volatility = risk
        if vol > 1.0:
            signal -= 0.3
            risk_flags.append(f"High volatility ({vol:.0%} annualized)")
        elif vol > 0.5:
            risk_flags.append(f"Moderate volatility ({vol:.0%})")

        # Drawdown risk
        if max_dd > 0.15:
            signal -= 0.3
            risk_flags.append(f"Recent drawdown {max_dd:.1%}")

        # Position capacity
        if at_capacity:
            signal = -1.0
            confidence = 1.0
            risk_flags.append(f"At max positions ({open_positions}/{max_positions})")

        if risk_flags:
            reasons = risk_flags
        else:
            reasons = ["Risk levels acceptable"]
            signal = 0.1  # Slight bullish bias when risk is low

        return AnalystVote(
            self.NAME, signal, confidence,
            "; ".join(reasons),
            {"volatility": vol, "max_drawdown": max_dd, "at_capacity": at_capacity},
        )


# ── Consensus Engine ──────────────────────────────────────────────────────────

class AnalystConsensus:
    """Runs all 7 analysts and combines their votes into a consensus."""

    def __init__(self):
        self.trend = TrendAnalyst()
        self.momentum = MomentumAnalyst()
        self.reversion = ReversionAnalyst()
        self.volume = VolumeAnalyst()
        self.sentiment = SentimentAnalyst()
        self.pattern = PatternAnalyst()
        self.risk = RiskAnalyst()

    def analyze(
        self,
        symbol: str,
        closes: list,
        highs: list = None,
        lows: list = None,
        volumes: list = None,
        fear_greed: int = 50,
        portfolio_value: float = 100000,
        open_positions: int = 0,
        max_positions: int = 5,
    ) -> ConsensusResult:
        """Run all analysts and return consensus."""
        highs = highs or closes
        lows = lows or closes

        # Collect votes from all 7 analysts
        votes = [
            self.trend.analyze(closes, highs, lows),
            self.momentum.analyze(closes, volumes),
            self.reversion.analyze(closes),
            self.volume.analyze(closes, volumes),
            self.sentiment.analyze(fear_greed),
            self.pattern.analyze(closes, highs, lows),
            self.risk.analyze(closes, portfolio_value, open_positions, max_positions),
        ]

        weights = [
            TrendAnalyst.WEIGHT,
            MomentumAnalyst.WEIGHT,
            ReversionAnalyst.WEIGHT,
            VolumeAnalyst.WEIGHT,
            SentimentAnalyst.WEIGHT,
            PatternAnalyst.WEIGHT,
            RiskAnalyst.WEIGHT,
        ]

        # Weighted signal combination
        weighted_sum = sum(v.signal * w * v.confidence for v, w in zip(votes, weights))
        total_weight = sum(w * v.confidence for v, w in zip(votes, weights))
        consensus_signal = weighted_sum / total_weight if total_weight > 0 else 0

        # Count bull/bear/neutral
        bull_count = sum(1 for v in votes if v.signal > 0.2)
        bear_count = sum(1 for v in votes if v.signal < -0.2)
        neutral_count = len(votes) - bull_count - bear_count

        # Map to 0-100 score
        score = int(50 + consensus_signal * 50)
        score = max(0, min(100, score))

        # Determine action
        if score >= 70:
            action = "BUY"
        elif score <= 30:
            action = "SELL"
        elif score >= 55:
            action = "HOLD"
        else:
            action = "SKIP"

        # Risk Judge override
        risk_vote = votes[-1]  # Last vote is always RiskAnalyst
        risk_override = False
        risk_reason = ""

        if risk_vote.data.get("at_capacity"):
            risk_override = True
            risk_reason = "Max positions reached"
            action = "SKIP"
        elif risk_vote.signal < -0.5 and action == "BUY":
            risk_override = True
            risk_reason = risk_vote.reasoning
            action = "HOLD"
            score = min(score, 55)

        total_confidence = sum(v.confidence for v in votes) / len(votes)

        logger.info(
            f"Consensus {symbol}: score={score} action={action} "
            f"bulls={bull_count} bears={bear_count} neutral={neutral_count} "
            f"{'[RISK OVERRIDE]' if risk_override else ''}"
        )

        return ConsensusResult(
            score=score,
            action=action,
            consensus_signal=consensus_signal,
            total_confidence=total_confidence,
            votes=votes,
            risk_override=risk_override,
            risk_reason=risk_reason,
            bull_count=bull_count,
            bear_count=bear_count,
            neutral_count=neutral_count,
        )
