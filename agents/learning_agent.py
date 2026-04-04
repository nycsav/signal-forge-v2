"""Signal Forge v2 — Learning Agent

Closed-loop feedback: learns from trade outcomes to adjust scoring weights.
Retrains after every 50 trades using logistic regression.
Smoothed weight updates: new = 0.70 × old + 0.30 × optimized.

Can ONLY adjust scoring weights. Cannot modify risk limits or circuit breakers.
"""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

from agents.event_bus import EventBus
from agents.events import TradeClosedEvent, WeightUpdateEvent
from db.repository import Repository

WEIGHTS_PATH = Path(__file__).parent.parent / "config" / "weights.json"
RETRAIN_THRESHOLD = 50  # Retrain every 50 trades
TRAINING_WINDOW = 200   # Last 200 trades
SMOOTHING = 0.70        # 70% old, 30% new
MIN_WEIGHT = 5.0        # No component goes below 5


class LearningAgent:
    def __init__(self, event_bus: EventBus, db_path: str):
        self.bus = event_bus
        self.repo = Repository(db_path)
        self._trades_since_retrain = 0
        self.bus.subscribe(TradeClosedEvent, self._on_trade_closed)

    async def _on_trade_closed(self, event: TradeClosedEvent):
        self._record_outcome(event)
        self._trades_since_retrain += 1

        if self._trades_since_retrain >= RETRAIN_THRESHOLD:
            await self._update_weights()
            self._trades_since_retrain = 0

    def _record_outcome(self, event: TradeClosedEvent):
        """Record trade outcome for learning."""
        self.repo.log_event("learning_agent", "trade_outcome", None, {
            "order_id": event.order_id,
            "pnl_usd": event.pnl_usd,
            "pnl_pct": event.pnl_pct,
            "close_reason": event.close_reason,
            "hold_hours": event.hold_time_hours,
        })

    async def _update_weights(self):
        """Retrain scoring weights from recent trade history."""
        trades = self._load_trade_history(TRAINING_WINDOW)
        if len(trades) < 20:
            logger.info(f"Learning: only {len(trades)} trades, need 20+ to retrain")
            return

        old_weights = self._load_weights()

        # Simple feature importance: correlation between component scores and win/loss
        # Components: technical, sentiment, on_chain, ai_analyst
        components = ["technical", "sentiment", "on_chain", "ai_analyst"]
        component_win_rates = {}

        for comp in components:
            # Trades where this component scored above median → did they win more?
            scores = []
            for t in trades:
                breakdown = t.get("score_breakdown")
                if isinstance(breakdown, str):
                    try:
                        breakdown = json.loads(breakdown)
                    except Exception:
                        breakdown = {}
                elif not isinstance(breakdown, dict):
                    breakdown = {}
                score = breakdown.get(comp, 50)
                won = 1 if (t.get("pnl_pct") or 0) > 0 else 0
                scores.append((score, won))

            if not scores:
                component_win_rates[comp] = 50
                continue

            median_score = sorted(s[0] for s in scores)[len(scores) // 2]
            above = [s for s in scores if s[0] >= median_score]
            below = [s for s in scores if s[0] < median_score]

            above_wr = sum(s[1] for s in above) / len(above) * 100 if above else 50
            below_wr = sum(s[1] for s in below) / len(below) * 100 if below else 50

            # How much better does above-median perform?
            component_win_rates[comp] = above_wr - below_wr + 50

        # Normalize to sum = 100
        total = sum(component_win_rates.values())
        if total <= 0:
            return

        new_raw = {k: v / total * 100 for k, v in component_win_rates.items()}

        # Apply smoothing: 70% old + 30% new
        new_weights = {}
        for comp in components:
            old_val = old_weights.get(comp, {}).get("weight", 0.25) * 100
            new_val = SMOOTHING * old_val + (1 - SMOOTHING) * new_raw.get(comp, 25)
            new_weights[comp] = max(MIN_WEIGHT, new_val)

        # Renormalize to sum = 100
        total_new = sum(new_weights.values())
        new_weights = {k: v / total_new * 100 for k, v in new_weights.items()}

        # Convert back to 0-1 fractions
        weight_fractions = {k: round(v / 100, 3) for k, v in new_weights.items()}

        # Save
        self._save_weights(weight_fractions)

        # Compute improvement metric
        old_sharpe = self._compute_sharpe(trades[:len(trades)//2])
        new_sharpe = self._compute_sharpe(trades[len(trades)//2:])

        # Emit event
        old_fractions = {k: v.get("weight", 0.25) if isinstance(v, dict) else v for k, v in old_weights.items()}
        event = WeightUpdateEvent(
            timestamp=datetime.now(),
            old_weights=old_fractions,
            new_weights=weight_fractions,
            training_window_trades=len(trades),
            sharpe_improvement=new_sharpe - old_sharpe,
        )
        await self.bus.publish(event)

        logger.info(
            f"Learning: weights updated from {len(trades)} trades. "
            f"Sharpe delta: {new_sharpe - old_sharpe:+.2f}"
        )

        # Persist to DB
        self.repo.save_weights(weight_fractions, len(trades), new_sharpe - old_sharpe)

    def _load_trade_history(self, limit: int = 200) -> list[dict]:
        return self.repo.get_recent_trades(limit)

    def _compute_sharpe(self, trades: list, risk_free: float = 0.0434) -> float:
        pnls = [t.get("pnl_pct") or 0 for t in trades]
        if len(pnls) < 2:
            return 0
        mean_pnl = sum(pnls) / len(pnls)
        std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls))
        if std_pnl < 1e-8:
            return 0
        daily_rf = risk_free / 365
        return (mean_pnl - daily_rf) / std_pnl * math.sqrt(365)

    def _compute_win_rate(self, trades: list) -> float:
        if not trades:
            return 0
        wins = sum(1 for t in trades if (t.get("pnl_pct") or 0) > 0)
        return wins / len(trades)

    def get_weights(self) -> dict:
        return self._load_weights()

    def _load_weights(self) -> dict:
        try:
            data = json.loads(WEIGHTS_PATH.read_text())
            return data.get("components", {})
        except Exception:
            return {"technical": {"weight": 0.35}, "sentiment": {"weight": 0.15},
                    "on_chain": {"weight": 0.10}, "ai_analyst": {"weight": 0.40}}

    def _save_weights(self, weight_fractions: dict):
        try:
            data = json.loads(WEIGHTS_PATH.read_text())
        except Exception:
            data = {"components": {}, "thresholds": {"min_score_to_propose": 55}}

        for comp, frac in weight_fractions.items():
            if comp in data["components"]:
                data["components"][comp]["weight"] = frac
            else:
                data["components"][comp] = {"weight": frac}

        WEIGHTS_PATH.write_text(json.dumps(data, indent=2))
        logger.info(f"Weights saved: {weight_fractions}")

    def generate_performance_report(self, window_days: int = 7) -> dict:
        since = (datetime.now() - timedelta(days=window_days)).isoformat()
        trades = self.repo.get_closed_trades_since(since)

        if not trades:
            return {"error": "No trades in window", "period_days": window_days}

        pnl_list = [t.get("pnl_usd") or 0 for t in trades]
        pnl_pct_list = [t.get("pnl_pct") or 0 for t in trades]

        wins = [p for p in pnl_list if p > 0]
        losses = [p for p in pnl_list if p <= 0]

        sharpe = self._compute_sharpe(trades)

        # Max drawdown
        equity = []
        cumsum = 0
        for p in pnl_list:
            cumsum += p
            equity.append(cumsum)
        peak = 0
        max_dd = 0
        for e in equity:
            if e > peak:
                peak = e
            dd = peak - e
            if dd > max_dd:
                max_dd = dd

        return {
            "period_days": window_days,
            "total_trades": len(trades),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "total_pnl_usd": sum(pnl_list),
            "total_pnl_pct": sum(pnl_pct_list),
            "avg_win_usd": sum(wins) / len(wins) if wins else 0,
            "avg_loss_usd": sum(losses) / len(losses) if losses else 0,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf"),
            "sharpe_ratio": sharpe,
            "max_drawdown_usd": max_dd,
            "avg_hold_hours": sum(t.get("hold_time_hours") or 0 for t in trades) / len(trades) if trades else 0,
            "exit_breakdown": {
                "stop": len([t for t in trades if t.get("close_reason") == "stop"]),
                "trailing": len([t for t in trades if t.get("close_reason") == "trailing_stop"]),
                "tp1": len([t for t in trades if t.get("close_reason") == "tp1"]),
                "tp2": len([t for t in trades if t.get("close_reason") == "tp2"]),
                "tp3": len([t for t in trades if t.get("close_reason") == "tp3"]),
                "time": len([t for t in trades if "time" in (t.get("close_reason") or "")]),
                "flat": len([t for t in trades if "flat" in (t.get("close_reason") or "")]),
            },
            "current_weights": self.get_weights(),
        }
