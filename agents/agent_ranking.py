"""Signal Forge v2 — Agent Performance Ranking (from ContestTrade)

Tracks each model's prediction accuracy and weights consensus votes
proportionally to their Sharpe ratio. Agents with Sharpe <= 0 get
zero vote weight — Darwinian pressure on signal quality.

Adapted from: github.com/FinStep-AI/ContestTrade
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from loguru import logger


class AgentRanking:
    """Track model accuracy, rank by Sharpe, weight votes."""

    WINDOW_DAYS = 7        # rolling evaluation window
    MIN_TRADES = 5         # minimum trades before ranking activates

    def __init__(self):
        # {model_name: [{direction, score, symbol, pnl_pct, timestamp}, ...]}
        self._predictions: dict[str, list[dict]] = defaultdict(list)
        self._weights: dict[str, float] = {}
        self._last_rerank: datetime = datetime.min

    def record_prediction(self, model: str, symbol: str, direction: str, score: float):
        """Record a model's prediction for later evaluation."""
        self._predictions[model].append({
            "symbol": symbol,
            "direction": direction,
            "score": score,
            "timestamp": datetime.now(),
            "pnl_pct": None,  # filled when trade closes
        })
        # Keep only recent predictions
        cutoff = datetime.now() - timedelta(days=self.WINDOW_DAYS)
        self._predictions[model] = [
            p for p in self._predictions[model] if p["timestamp"] > cutoff
        ]

    def record_outcome(self, symbol: str, pnl_pct: float):
        """Match a trade outcome to predictions and update accuracy."""
        for model, preds in self._predictions.items():
            for pred in reversed(preds):
                if pred["symbol"] == symbol and pred["pnl_pct"] is None:
                    # Was the direction correct?
                    if pred["direction"] == "long":
                        pred["pnl_pct"] = pnl_pct
                    elif pred["direction"] == "short":
                        pred["pnl_pct"] = -pnl_pct
                    else:
                        pred["pnl_pct"] = 0
                    break

    def get_model_sharpe(self, model: str) -> float:
        """Calculate rolling Sharpe ratio for a model."""
        preds = [p for p in self._predictions.get(model, []) if p["pnl_pct"] is not None]
        if len(preds) < self.MIN_TRADES:
            return 0.5  # neutral weight until enough data

        pnls = [p["pnl_pct"] for p in preds]
        mean_pnl = sum(pnls) / len(pnls)
        std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls))

        if std_pnl < 1e-8:
            return 0.5

        return mean_pnl / std_pnl

    def rerank(self) -> dict[str, float]:
        """Recalculate vote weights based on Sharpe ratios.
        Only models with Sharpe > 0 get non-zero weight.
        Returns {model_name: weight} where weights sum to 1.0."""
        sharpes = {}
        for model in self._predictions:
            sharpes[model] = self.get_model_sharpe(model)

        # Filter positive Sharpe only (ContestTrade pattern)
        positive = {m: s for m, s in sharpes.items() if s > 0}

        if not positive:
            # All models neutral — equal weight
            n = max(len(sharpes), 1)
            self._weights = {m: 1.0 / n for m in sharpes}
        else:
            total = sum(positive.values())
            self._weights = {}
            for m in sharpes:
                if m in positive:
                    self._weights[m] = positive[m] / total
                else:
                    self._weights[m] = 0.0

        self._last_rerank = datetime.now()

        logger.info(f"AGENT RANKING: {', '.join(f'{m}={w:.0%}' for m, w in self._weights.items())}")
        return self._weights

    def get_weight(self, model: str) -> float:
        """Get current vote weight for a model."""
        # Rerank every hour
        if (datetime.now() - self._last_rerank).total_seconds() > 3600:
            self.rerank()
        return self._weights.get(model, 0.5)

    def weighted_consensus(self, votes: dict[str, dict]) -> dict:
        """Compute weighted consensus from multiple model votes.

        votes: {model_name: {"direction": str, "score": float, "confidence": float}}
        Returns: {"direction": str, "score": float, "confidence": float, "consensus": bool}
        """
        if not votes:
            return {"direction": "flat", "score": 0, "confidence": 0, "consensus": False}

        # Rerank if stale
        if (datetime.now() - self._last_rerank).total_seconds() > 3600:
            self.rerank()

        weighted_score = 0
        weighted_conf = 0
        direction_votes = defaultdict(float)
        total_weight = 0

        for model, vote in votes.items():
            w = self.get_weight(model)
            weighted_score += vote.get("score", 0) * w
            weighted_conf += vote.get("confidence", 0) * w
            direction_votes[vote.get("direction", "flat")] += w
            total_weight += w

        if total_weight <= 0:
            return {"direction": "flat", "score": 0, "confidence": 0, "consensus": False}

        # Winner takes all on direction
        best_dir = max(direction_votes, key=direction_votes.get)
        best_dir_weight = direction_votes[best_dir] / total_weight

        # Consensus requires >60% weighted agreement
        consensus = best_dir_weight > 0.6 and best_dir != "flat"

        return {
            "direction": best_dir,
            "score": weighted_score / total_weight,
            "confidence": weighted_conf / total_weight,
            "consensus": consensus,
            "agreement_pct": best_dir_weight,
            "weights_used": dict(self._weights),
        }
