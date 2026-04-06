"""Signal Forge v2 — Regime Adaptive Engine

Dynamically adjusts trading parameters based on market conditions.
Runs every scan cycle, reads market state, and modifies:
  - Entry score threshold (normally 55)
  - Position size multiplier
  - Stop loss distance
  - Take profit targets
  - Strategy bias (accumulate vs momentum vs defensive)

The system trades differently in fear vs greed, trending vs ranging, high vol vs low vol.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from loguru import logger

from agents.events import MarketRegime
from db.repository import Repository


@dataclass
class AdaptiveParameters:
    """Current trading parameters — changes every cycle based on regime."""
    # Identification
    regime: str
    regime_label: str
    timestamp: str

    # Entry
    score_threshold: float       # Min score to propose trade (default 55)
    ai_confidence_min: float     # Min AI confidence (default 0.45)

    # Sizing
    position_size_mult: float    # Multiplier on Half-Kelly size (0.25x to 1.5x)
    max_positions: int           # Dynamic max positions (2-6)

    # Stops
    stop_atr_mult: float         # ATR multiplier for stops (1.5 to 3.5)
    trailing_activation_mult: float  # When trailing starts (1.0 to 2.0)

    # Take profit
    tp1_r_mult: float            # TP1 as R-multiple (1.0 to 2.0)
    tp2_r_mult: float
    tp3_r_mult: float

    # Strategy bias
    strategy: str                # "accumulate", "momentum", "mean_reversion", "defensive", "hold"
    bias: str                    # "long_only", "long_bias", "neutral", "short_bias"

    # Reasoning
    reasoning: list[str]


class RegimeAdaptiveEngine:
    """Reads market conditions and outputs adaptive trading parameters."""

    def __init__(self, db_path: str):
        self.repo = Repository(db_path)
        self._current: AdaptiveParameters | None = None
        self._history: list[dict] = []

    @property
    def params(self) -> AdaptiveParameters:
        if self._current is None:
            return self._default_params()
        return self._current

    def update(
        self,
        fear_greed: int,
        market_regime: MarketRegime,
        avg_atr_pct: float = 0.03,
        recent_win_rate: float = 0.5,
        open_positions: int = 0,
        recent_stop_rate: float = 0.0,
        recent_tp_rate: float = 0.0,
    ) -> AdaptiveParameters:
        """Recalculate adaptive parameters from current market state."""
        reasoning = []

        # ── Step 1: Determine primary regime ──
        if fear_greed < 15:
            regime = "capitulation"
            regime_label = "CAPITULATION"
        elif fear_greed < 25:
            regime = "extreme_fear"
            regime_label = "EXTREME FEAR"
        elif fear_greed < 40:
            regime = "fear"
            regime_label = "FEAR"
        elif fear_greed < 60:
            regime = "neutral"
            regime_label = "NEUTRAL"
        elif fear_greed < 75:
            regime = "greed"
            regime_label = "GREED"
        elif fear_greed < 90:
            regime = "extreme_greed"
            regime_label = "EXTREME GREED"
        else:
            regime = "euphoria"
            regime_label = "EUPHORIA"

        # ── Step 2: Volatility overlay ──
        vol_regime = "normal"
        if avg_atr_pct > 0.06:
            vol_regime = "high_vol"
        elif avg_atr_pct < 0.015:
            vol_regime = "low_vol"

        # ── Step 3: Adapt entry thresholds ──
        if regime == "capitulation":
            # Maximum fear = maximum opportunity. Lower threshold, accumulate.
            score_threshold = 40
            ai_confidence_min = 0.35
            strategy = "accumulate"
            bias = "long_only"
            reasoning.append(f"F&G={fear_greed}: Capitulation — lowering threshold to 40, accumulation mode")

        elif regime == "extreme_fear":
            score_threshold = 45
            ai_confidence_min = 0.40
            strategy = "accumulate"
            bias = "long_bias"
            reasoning.append(f"F&G={fear_greed}: Extreme fear — contrarian buying, threshold 45")

        elif regime == "fear":
            score_threshold = 50
            ai_confidence_min = 0.42
            strategy = "mean_reversion"
            bias = "long_bias"
            reasoning.append(f"F&G={fear_greed}: Fear — mean reversion, threshold 50")

        elif regime == "neutral":
            score_threshold = 55
            ai_confidence_min = 0.45
            strategy = "momentum"
            bias = "neutral"
            reasoning.append(f"F&G={fear_greed}: Neutral — standard parameters")

        elif regime == "greed":
            score_threshold = 60
            ai_confidence_min = 0.50
            strategy = "momentum"
            bias = "neutral"
            reasoning.append(f"F&G={fear_greed}: Greed — higher bar, threshold 60")

        elif regime == "extreme_greed":
            score_threshold = 70
            ai_confidence_min = 0.55
            strategy = "defensive"
            bias = "short_bias"
            reasoning.append(f"F&G={fear_greed}: Extreme greed — defensive, threshold 70, tighten stops")

        else:  # euphoria
            score_threshold = 80
            ai_confidence_min = 0.65
            strategy = "defensive"
            bias = "short_bias"
            reasoning.append(f"F&G={fear_greed}: Euphoria — near top, minimal new longs, threshold 80")

        # ── Step 4: Adapt position sizing ──
        if regime in ("capitulation", "extreme_fear"):
            position_size_mult = 0.5  # Small positions in fear (scale in gradually)
            max_positions = 15        # Allow many positions for gradual accumulation in fear
        elif regime == "fear":
            position_size_mult = 0.75
            max_positions = 5
        elif regime == "neutral":
            position_size_mult = 1.0
            max_positions = 5
        elif regime == "greed":
            position_size_mult = 0.75
            max_positions = 4
        elif regime in ("extreme_greed", "euphoria"):
            position_size_mult = 0.5
            max_positions = 3
        else:
            position_size_mult = 1.0
            max_positions = 5

        # Volatility adjustment
        if vol_regime == "high_vol":
            position_size_mult *= 0.6
            reasoning.append(f"High volatility (ATR {avg_atr_pct:.1%}) — sizing down 40%")
        elif vol_regime == "low_vol":
            position_size_mult *= 1.3
            reasoning.append(f"Low volatility (ATR {avg_atr_pct:.1%}) — sizing up 30%")

        # ── Step 5: Adapt stops ──
        if vol_regime == "high_vol":
            stop_atr_mult = 3.5      # Wider stops in high vol
            trailing_activation = 2.0
        elif vol_regime == "low_vol":
            stop_atr_mult = 1.5      # Tighter stops in low vol
            trailing_activation = 1.0
        else:
            stop_atr_mult = 2.5      # Default
            trailing_activation = 1.5

        # If recent stop rate is high, widen stops
        if recent_stop_rate > 0.7 and recent_stop_rate > 0:
            stop_atr_mult += 0.5
            reasoning.append(f"Stop rate {recent_stop_rate:.0%} — widening stops by 0.5x ATR")

        # ── Step 6: Adapt take profits ──
        if regime in ("extreme_fear", "capitulation"):
            # In fear, take profits earlier (market may not run far)
            tp1_r = 1.0
            tp2_r = 2.0
            tp3_r = 3.5
            reasoning.append("Fear regime — tighter TP targets (1R/2R/3.5R)")
        elif regime in ("extreme_greed", "euphoria"):
            # In greed, let winners run more (momentum strong)
            tp1_r = 2.0
            tp2_r = 4.0
            tp3_r = 7.0
            reasoning.append("Greed regime — wider TP targets (2R/4R/7R)")
        else:
            tp1_r = 1.5
            tp2_r = 3.0
            tp3_r = 5.0

        # If TP1 is never hit, widen it
        if recent_tp_rate < 0.2 and recent_tp_rate > 0:
            tp1_r *= 0.8
            reasoning.append(f"Low TP rate {recent_tp_rate:.0%} — tightening TP1")

        # ── Step 7: Learning adjustments ──
        if recent_win_rate < 0.35 and recent_win_rate > 0:
            score_threshold += 5
            position_size_mult *= 0.7
            reasoning.append(f"Win rate {recent_win_rate:.0%} — raising threshold +5, sizing down 30%")
        elif recent_win_rate > 0.65:
            position_size_mult *= 1.2
            reasoning.append(f"Win rate {recent_win_rate:.0%} — sizing up 20%")

        # ── Build result ──
        params = AdaptiveParameters(
            regime=regime,
            regime_label=regime_label,
            timestamp=datetime.now().isoformat(),
            score_threshold=score_threshold,
            ai_confidence_min=ai_confidence_min,
            position_size_mult=round(min(1.5, max(0.25, position_size_mult)), 2),
            max_positions=max_positions,
            stop_atr_mult=round(stop_atr_mult, 1),
            trailing_activation_mult=round(trailing_activation, 1),
            tp1_r_mult=round(tp1_r, 1),
            tp2_r_mult=round(tp2_r, 1),
            tp3_r_mult=round(tp3_r, 1),
            strategy=strategy,
            bias=bias,
            reasoning=reasoning,
        )

        self._current = params

        # Log
        self._history.append({
            "timestamp": params.timestamp,
            "regime": regime,
            "fear_greed": fear_greed,
            "score_threshold": score_threshold,
            "position_size_mult": params.position_size_mult,
            "strategy": strategy,
        })
        if len(self._history) > 500:
            self._history = self._history[-500:]

        self.repo.log_event("regime_engine", "params_updated", None, {
            "regime": regime, "fear_greed": fear_greed,
            "score_threshold": score_threshold,
            "position_size_mult": params.position_size_mult,
            "strategy": strategy, "bias": bias,
            "stop_atr_mult": params.stop_atr_mult,
        })

        logger.info(
            f"Regime: {regime_label} | Strategy: {strategy} | "
            f"Threshold: {score_threshold} | Size: {params.position_size_mult}x | "
            f"Stops: {params.stop_atr_mult}x ATR | Bias: {bias}"
        )

        return params

    def _default_params(self) -> AdaptiveParameters:
        return AdaptiveParameters(
            regime="neutral", regime_label="NEUTRAL",
            timestamp=datetime.now().isoformat(),
            score_threshold=55, ai_confidence_min=0.45,
            position_size_mult=1.0, max_positions=5,
            stop_atr_mult=2.5, trailing_activation_mult=1.5,
            tp1_r_mult=1.5, tp2_r_mult=3.0, tp3_r_mult=5.0,
            strategy="momentum", bias="neutral", reasoning=["Default parameters"],
        )

    def get_dashboard_data(self) -> dict:
        p = self.params
        return {
            "regime": p.regime,
            "regime_label": p.regime_label,
            "strategy": p.strategy,
            "bias": p.bias,
            "score_threshold": p.score_threshold,
            "ai_confidence_min": p.ai_confidence_min,
            "position_size_mult": p.position_size_mult,
            "max_positions": p.max_positions,
            "stop_atr_mult": p.stop_atr_mult,
            "trailing_activation_mult": p.trailing_activation_mult,
            "tp_targets": [p.tp1_r_mult, p.tp2_r_mult, p.tp3_r_mult],
            "reasoning": p.reasoning,
            "history_length": len(self._history),
        }
