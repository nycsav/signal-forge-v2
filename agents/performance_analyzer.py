"""Signal Forge v2 — Performance Analyzer (Self-Improving Agent)

Runs after every trade close. Analyzes patterns across outcomes and
broadcasts PerformanceFeedbackEvent so all agents can self-adjust.

This is the "brain" that makes the system learn in real-time:
- Tracks win rates by symbol, exit reason, regime, consensus
- Detects performance drift (rolling window degradation)
- Recommends parameter adjustments via StrategyAdaptationEvent
- Manages re-entry cooldowns after profitable trailing stop exits

Does NOT modify risk floors or circuit breakers (inviolable per CLAUDE.md §2.1).
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from loguru import logger

from agents.event_bus import EventBus, Priority
from agents.events import (
    TradeClosedEvent, PerformanceFeedbackEvent, StrategyAdaptationEvent,
    ReEntrySignalEvent, WeightUpdateEvent,
)
from agents.trade_logger import get_recent_outcomes, get_win_rate_by_signal


# Thresholds for self-improvement triggers
MIN_TRADES_FOR_FEEDBACK = 10
DRIFT_WINDOW = 20          # rolling window for drift detection
DRIFT_THRESHOLD = -0.15    # if avg P&L drops below this, trigger adaptation
REENTRY_COOLDOWN_BARS = 4  # minimum bars before re-entry after trail exit
REENTRY_MIN_PNL_PCT = 0.5  # only re-enter after exits that were profitable


class PerformanceAnalyzer:
    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._trade_buffer: list[dict] = []
        self._reentry_cooldowns: dict[str, datetime] = {}  # symbol → cooldown expiry

        # Subscribe to trade outcomes
        self.bus.subscribe(TradeClosedEvent, self._on_trade_closed, priority=Priority.LOW)
        # Subscribe to weight updates to log adaptation
        self.bus.subscribe(WeightUpdateEvent, self._on_weight_update, priority=Priority.LOW)

    async def _on_trade_closed(self, event: TradeClosedEvent):
        """Process every closed trade for real-time learning."""
        trade = {
            "symbol": event.order_id,  # order_id carries symbol in current impl
            "pnl_pct": event.pnl_pct,
            "pnl_usd": event.pnl_usd,
            "close_reason": event.close_reason,
            "hold_hours": event.hold_time_hours,
            "timestamp": event.timestamp,
        }
        self._trade_buffer.append(trade)

        # Check for re-entry opportunity (trailing stop exit in profit)
        if event.close_reason in ("trailing_stop", "tp1") and event.pnl_pct > REENTRY_MIN_PNL_PCT:
            await self._emit_reentry_signal(event)

        # Emit feedback after every batch
        if len(self._trade_buffer) >= MIN_TRADES_FOR_FEEDBACK:
            await self._analyze_and_broadcast()

    async def _emit_reentry_signal(self, event: TradeClosedEvent):
        """After a profitable trailing stop exit, signal re-entry evaluation."""
        symbol = event.order_id
        now = datetime.now()

        # Check cooldown
        if symbol in self._reentry_cooldowns and now < self._reentry_cooldowns[symbol]:
            return

        # Set cooldown
        self._reentry_cooldowns[symbol] = now + timedelta(hours=1)

        reentry = ReEntrySignalEvent(
            timestamp=now,
            symbol=symbol,
            exit_price=event.close_price,
            exit_reason=event.close_reason,
            pnl_pct=event.pnl_pct,
            original_direction="long",
            atr_at_exit=0,  # will be computed by AI analyst
            cooldown_bars=REENTRY_COOLDOWN_BARS,
        )

        logger.info(
            f"REENTRY SIGNAL: {symbol} exited via {event.close_reason} "
            f"at +{event.pnl_pct:.1%} — queuing for re-evaluation"
        )
        await self.bus.publish(reentry, priority=Priority.HIGH)

    async def _analyze_and_broadcast(self):
        """Analyze recent trades and broadcast insights to all agents."""
        outcomes = get_recent_outcomes(50)
        if len(outcomes) < MIN_TRADES_FOR_FEEDBACK:
            return

        # Win rate
        wins = sum(1 for t in outcomes if (t.get("pnl_pct") or 0) > 0)
        win_rate = wins / len(outcomes)

        # Avg P&L
        pnls = [t.get("pnl_pct") or 0 for t in outcomes]
        avg_pnl = sum(pnls) / len(pnls)

        # Sharpe (simplified)
        import math
        if len(pnls) >= 2:
            mean = sum(pnls) / len(pnls)
            std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
            sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0
        else:
            sharpe = 0

        # Best/worst exit reasons
        by_reason = defaultdict(list)
        for t in outcomes:
            r = t.get("exit_reason", "unknown")
            by_reason[r].append(t.get("pnl_pct") or 0)

        reason_avgs = {r: sum(ps) / len(ps) for r, ps in by_reason.items() if ps}
        best_exit = max(reason_avgs, key=reason_avgs.get) if reason_avgs else "unknown"
        worst_exit = min(reason_avgs, key=reason_avgs.get) if reason_avgs else "unknown"

        # Best/worst symbols
        by_symbol = defaultdict(list)
        for t in outcomes:
            by_symbol[t.get("symbol", "?")].append(t.get("pnl_pct") or 0)

        symbol_avgs = {s: sum(ps) / len(ps) for s, ps in by_symbol.items() if len(ps) >= 2}
        sorted_symbols = sorted(symbol_avgs.items(), key=lambda x: x[1], reverse=True)
        best_symbols = [s for s, _ in sorted_symbols[:3]]
        worst_symbols = [s for s, _ in sorted_symbols[-3:]]

        # Recommendations
        recommendations = []
        if win_rate < 0.40:
            recommendations.append("Win rate below 40% — tighten entry criteria")
        if avg_pnl < -0.5:
            recommendations.append("Avg P&L negative — review stop distances")
        if reason_avgs.get("hard_stop", 0) < -2.0:
            recommendations.append("Hard stops losing >2% avg — consider wider stops or better entries")
        if reason_avgs.get("time_72h", 0) < 0:
            recommendations.append("Time exits losing money — reduce max hold time")

        # Consensus analysis
        signal_stats = get_win_rate_by_signal()
        consensus_data = signal_stats.get("consensus", {})
        no_consensus_data = signal_stats.get("no_consensus", {})
        if consensus_data.get("win_rate", 0) > no_consensus_data.get("win_rate", 0) + 10:
            recommendations.append(f"Consensus trades win {consensus_data.get('win_rate',0):.0f}% vs "
                                   f"non-consensus {no_consensus_data.get('win_rate',0):.0f}% — consensus gate validated")

        feedback = PerformanceFeedbackEvent(
            timestamp=datetime.now(),
            window_trades=len(outcomes),
            win_rate=win_rate,
            avg_pnl_pct=avg_pnl,
            sharpe=sharpe,
            best_exit_reason=best_exit,
            worst_exit_reason=worst_exit,
            best_symbols=best_symbols,
            worst_symbols=worst_symbols,
            recommended_actions=recommendations,
        )

        logger.info(
            f"PERFORMANCE FEEDBACK: {len(outcomes)} trades | "
            f"WR={win_rate:.0%} | Avg={avg_pnl:+.2f}% | Sharpe={sharpe:.2f} | "
            f"Best exit: {best_exit} | Worst: {worst_exit}"
        )
        for rec in recommendations:
            logger.info(f"  → {rec}")

        await self.bus.publish(feedback, priority=Priority.LOW)

        # Check for drift
        if len(pnls) >= DRIFT_WINDOW:
            recent = pnls[:DRIFT_WINDOW]
            recent_avg = sum(recent) / len(recent)
            if recent_avg < DRIFT_THRESHOLD:
                adaptation = StrategyAdaptationEvent(
                    timestamp=datetime.now(),
                    trigger="performance_drift",
                    old_params={"avg_pnl": avg_pnl, "window": len(outcomes)},
                    new_params={"avg_pnl_recent": recent_avg, "window": DRIFT_WINDOW},
                    reason=f"Rolling {DRIFT_WINDOW}-trade avg P&L is {recent_avg:+.2f}%, "
                           f"below drift threshold of {DRIFT_THRESHOLD:+.1%}"
                )
                logger.warning(f"DRIFT DETECTED: {adaptation.reason}")
                await self.bus.publish(adaptation, priority=Priority.HIGH)

        self._trade_buffer.clear()

    async def _on_weight_update(self, event: WeightUpdateEvent):
        """Log when LearningAgent updates weights."""
        logger.info(
            f"WEIGHTS ADAPTED: Sharpe improvement {event.sharpe_improvement:+.3f} | "
            f"Window: {event.training_window_trades} trades | "
            f"New: {event.new_weights}"
        )
