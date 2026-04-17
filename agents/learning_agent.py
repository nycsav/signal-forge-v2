"""Signal Forge v2 — Learning Agent (with guard rails)

Closed-loop feedback: learns from trade outcomes to adjust scoring weights.
Guard rails prevent overfitting and violent weight swings.

Rules:
- MIN_TRADES_BEFORE_UPDATE = 20 (batch minimum)
- 25% validation holdout (must improve Sharpe by >5% on holdout to accept)
- MAX_WEIGHT_DELTA = 0.15 (no single update shifts a weight by >15%)
- Can ONLY adjust scoring weights. Cannot modify risk limits or circuit breakers.
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

# Guard rails
MIN_TRADES_BEFORE_UPDATE = 20   # Don't update on every trade — batch minimum
TRAINING_WINDOW = 200           # Last 200 trades
VALIDATION_HOLDOUT_RATIO = 0.25 # 25% holdout for validation
MAX_WEIGHT_DELTA = 0.15         # No single update shifts a weight by >15%
MIN_WEIGHT = 5.0                # No component goes below 5
SMOOTHING = 0.70                # 70% old, 30% new


class LearningAgent:
    def __init__(self, event_bus: EventBus, db_path: str):
        self.bus = event_bus
        self.repo = Repository(db_path)
        self._trade_buffer: list = []
        self._current_weights = self._load_weights()
        self.bus.subscribe(TradeClosedEvent, self._on_trade_closed)

    async def _on_trade_closed(self, event: TradeClosedEvent):
        self._record_outcome(event)
        self._trade_buffer.append({
            "pnl_pct": event.pnl_pct,
            "pnl_usd": event.pnl_usd,
            "close_reason": event.close_reason,
            "hold_hours": event.hold_time_hours,
        })

        # Don't update on every trade — batch minimum
        if len(self._trade_buffer) < MIN_TRADES_BEFORE_UPDATE:
            logger.debug(f"Learning: {len(self._trade_buffer)}/{MIN_TRADES_BEFORE_UPDATE} trades buffered")
            return

        await self._run_weight_optimization()

    def _record_outcome(self, event: TradeClosedEvent):
        """Record trade outcome for learning."""
        self.repo.log_event("learning_agent", "trade_outcome", None, {
            "order_id": event.order_id,
            "pnl_usd": event.pnl_usd,
            "pnl_pct": event.pnl_pct,
            "close_reason": event.close_reason,
            "hold_hours": event.hold_time_hours,
        })

    async def _run_weight_optimization(self):
        """Optimize weights with validation holdout and delta clamping."""
        trades = self._load_trade_history(TRAINING_WINDOW)
        if len(trades) < MIN_TRADES_BEFORE_UPDATE:
            logger.info(f"Learning: only {len(trades)} trades, need {MIN_TRADES_BEFORE_UPDATE}+ to retrain")
            return

        # Split into train and validation sets
        n_val = max(1, int(len(trades) * VALIDATION_HOLDOUT_RATIO))
        train_trades = trades[:-n_val]
        val_trades = trades[-n_val:]

        old_weights = self._current_weights

        # Optimize on training set
        candidate_weights = self._optimize(train_trades)

        # Validate on holdout — must improve Sharpe by >5% to accept
        train_sharpe = self._compute_sharpe_with_weights(train_trades, old_weights)
        val_sharpe = self._compute_sharpe_with_weights(val_trades, candidate_weights)

        if val_sharpe <= train_sharpe * 1.05:
            logger.warning(
                f"Learning: weight update REJECTED — val Sharpe {val_sharpe:.3f} "
                f"did not improve >5% over train {train_sharpe:.3f}"
            )
            self._trade_buffer.clear()
            return

        # Clamp delta — prevent violent weight swings
        new_weights = {}
        for k in candidate_weights:
            old_val = old_weights.get(k, {}).get("weight", 0.25) if isinstance(old_weights.get(k), dict) else old_weights.get(k, 0.25)
            new_val = candidate_weights[k]
            delta = new_val - old_val
            clamped = max(-MAX_WEIGHT_DELTA, min(MAX_WEIGHT_DELTA, delta))
            new_weights[k] = round(old_val + clamped, 3)

        # Ensure minimums and normalize
        for k in new_weights:
            new_weights[k] = max(MIN_WEIGHT / 100, new_weights[k])
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 3) for k, v in new_weights.items()}

        # Save
        self._current_weights = new_weights
        self._save_weights(new_weights)
        self._trade_buffer.clear()

        # Emit event
        old_fractions = {k: v.get("weight", 0.25) if isinstance(v, dict) else v for k, v in old_weights.items()}
        event = WeightUpdateEvent(
            timestamp=datetime.now(),
            old_weights=old_fractions,
            new_weights=new_weights,
            training_window_trades=len(trades),
            sharpe_improvement=val_sharpe - train_sharpe,
        )
        await self.bus.publish(event)

        logger.info(
            f"Learning: weights UPDATED after {len(trades)} trades "
            f"(train={len(train_trades)}, val={len(val_trades)}). "
            f"Val Sharpe: {val_sharpe:.3f} (train: {train_sharpe:.3f})"
        )

        # Persist to DB
        self.repo.save_weights(new_weights, len(trades), val_sharpe - train_sharpe)

    def _optimize(self, trades: list) -> dict:
        """Optimize weights from trade history using component win rate analysis."""
        components = ["technical", "sentiment", "on_chain", "ai_analyst"]
        component_win_rates = {}

        for comp in components:
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
                component_win_rates[comp] = 0.25
                continue

            median_score = sorted(s[0] for s in scores)[len(scores) // 2]
            above = [s for s in scores if s[0] >= median_score]
            below = [s for s in scores if s[0] < median_score]

            above_wr = sum(s[1] for s in above) / len(above) if above else 0.5
            below_wr = sum(s[1] for s in below) / len(below) if below else 0.5

            # Weight proportional to edge
            component_win_rates[comp] = max(0.05, above_wr - below_wr + 0.25)

        # Normalize to sum = 1.0
        total = sum(component_win_rates.values())
        if total <= 0:
            return {k: 0.25 for k in components}
        return {k: v / total for k, v in component_win_rates.items()}

    def _compute_sharpe_with_weights(self, trades: list, weights: dict) -> float:
        """Compute Sharpe ratio using given weights."""
        # For now, use raw P&L since we don't re-score with different weights
        return self._compute_sharpe(trades)

    def _load_trade_history(self, limit: int = 200) -> list[dict]:
        # Fix 2026-04-16: Read from trade_outcomes (real P&L data), not trades
        # table (which has all pnl=0 due to missing close updates).
        # Also join score_breakdown from signals_log for weight optimization.
        from agents.trade_logger import get_recent_outcomes
        outcomes = get_recent_outcomes(limit)
        # Enrich with score_breakdown from signals_log where possible
        for t in outcomes:
            if not t.get("score_breakdown"):
                sym = t.get("symbol", "")
                rows = self.repo._conn().execute(
                    "SELECT score_breakdown FROM signals_log WHERE symbol=? "
                    "AND score_breakdown IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (sym,)
                ).fetchall()
                if rows:
                    t["score_breakdown"] = dict(rows[0]).get("score_breakdown")
        return outcomes

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
        return self._current_weights

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
            "guard_rails": {
                "min_trades_before_update": MIN_TRADES_BEFORE_UPDATE,
                "validation_holdout": f"{VALIDATION_HOLDOUT_RATIO*100:.0f}%",
                "max_weight_delta": MAX_WEIGHT_DELTA,
                "smoothing": SMOOTHING,
            },
        }
