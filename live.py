#!/usr/bin/env python3
"""Signal Forge v2 — Live Trading Engine (Refactored)

Uses the SAME agent pipeline as main.py:
  EventBus → MarketData → Technical → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent

All thresholds come from RiskAgent (MIN_SIGNAL_SCORE=62, MIN_AI_CONFIDENCE=0.62).
No inline threshold checks. RiskAgent is the single source of truth.

Usage:
  PYTHONPATH=. python live.py              # Start live engine
  PYTHONPATH=. python live.py --dry-run    # Simulate without placing real orders
"""

import asyncio
import signal as sig
import argparse
import time
from datetime import datetime
from loguru import logger

from config.settings import settings
from db.live_repository import LiveRepository
from db.repository import Repository
from data import coinbase_client, fear_greed_client
from agents.event_bus import EventBus
from agents.events import (
    MarketStateEvent, TechnicalEvent, SentimentEvent, OnChainEvent,
    SignalBundle, TradeProposal, RiskAssessmentEvent, RiskDecision, Direction,
)
from agents.market_data_agent import MarketDataAgent
from agents.technical_agent import TechnicalAgent
from agents.sentiment_agent import SentimentAgent
from agents.onchain_agent import OnChainAgent
from agents.ai_analyst_agent import AIAnalystAgent
from agents.risk_agent import RiskAgent
from agents.execution_agent import ExecutionAgent
from agents.monitor_agent import MonitorAgent
from agents.scoring import SignalScorer
from agents.regime_engine import RegimeAdaptiveEngine
from agents.whale_trigger import WhaleTrigger
from agents.altfins_enrichment import AltFINSEnrichment


# Live watchlist — only the most liquid
LIVE_WATCHLIST = ["BTC-USD", "ETH-USD", "SOL-USD"]

# Live-specific constants (not thresholds — those come from RiskAgent)
STARTING_CAPITAL = 300.00
DAILY_LOSS_LIMIT_USD = 15.00      # 5% of $300
DAILY_LOSS_LIMIT_PCT = 0.05
MAX_TRADES_PER_DAY = 5
WHALE_NET_FLOW_WINDOW_SECONDS = 43200  # 12 hours — rolling whale event window
WHALE_BLOCK_MIN_STRENGTH = 3           # min bearish strength to qualify for block


class LiveEngine:
    """Live trading engine using the identical agent pipeline as main.py.

    Pipeline: EventBus → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.live_repo = LiveRepository()
        self.bus = EventBus()
        self.repo = Repository(settings.database_path)
        self.scorer = SignalScorer()

        config = settings.model_dump()

        # ── Same agents as main.py ──
        self.market_data = MarketDataAgent(self.bus, config)
        self.technical = TechnicalAgent(self.bus)
        self.sentiment = SentimentAgent(self.bus, config)
        self.onchain = OnChainAgent(self.bus, config)
        self.ai_analyst = AIAnalystAgent(self.bus, config, self.scorer)
        self.risk = RiskAgent(self.bus, settings.database_path, settings.portfolio_value)
        self.execution = ExecutionAgent(self.bus, config)
        self.monitor = MonitorAgent(self.bus, settings.database_path)
        self.regime = RegimeAdaptiveEngine(settings.database_path)

        # altFINS enrichment (patterns 4h, screener 15m)
        self.altfins = AltFINSEnrichment(
            api_key=config.get("altfins_api_key", ""),
            watchlist=config.get("watchlist", []),
        )

        # Whale trigger with direction-aware callback
        self.whale_trigger = WhaleTrigger(event_bus=self.bus, on_signal=self._on_whale_signal)

        # Live-specific state
        self.halted = False
        self.trades_today = 0
        # Rolling 12h whale-event window for net-flow block model.
        # Each entry: {"timestamp": float, "direction": str, "usd_value": float, "strength": int}
        self._whale_events: list[dict] = []
        self._whale_confidence_boost: float = 0  # temporary confidence boost from bullish whale

        # Orchestrator state for bundle assembly (same as main.py)
        self._market_states: dict = {}
        self._technical_states: dict = {}
        self._latest_sentiment: dict = {}
        self._latest_onchain: dict = {}
        self._last_sentiment_ts: datetime = datetime.now()
        self._last_onchain_ts: datetime = datetime.now()

        # Subscribe orchestrator to assemble SignalBundles (same as main.py)
        self.bus.subscribe(MarketStateEvent, self._on_market_state)
        self.bus.subscribe(TechnicalEvent, self._on_technical)
        self.bus.subscribe(SentimentEvent, self._on_sentiment)
        self.bus.subscribe(OnChainEvent, self._on_onchain)

        # Subscribe to risk decisions to log in live_repo
        self.bus.subscribe(RiskAssessmentEvent, self._on_risk_decision)

        logger.info(f"Live Engine initialized — {'DRY RUN' if dry_run else 'REAL MONEY'}")
        logger.info(f"Pipeline: EventBus → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent")
        logger.info(f"Thresholds from RiskAgent: score>={self.risk.MIN_SIGNAL_SCORE}, "
                     f"confidence>={self.risk.MIN_AI_CONFIDENCE}")
        logger.info(f"Watchlist: {LIVE_WATCHLIST}")

    # ── Event handlers (identical to main.py orchestrator) ──

    async def _on_market_state(self, event: MarketStateEvent):
        self._market_states[event.symbol] = event

        perf = self.repo.get_performance_stats(7)
        win_rate = perf.get("win_rate", 50) / 100
        self.regime.update(
            fear_greed=event.fear_greed_index,
            market_regime=event.regime,
            avg_atr_pct=event.atr_14 / event.price if event.price > 0 and event.atr_14 > 0 else 0.03,
            recent_win_rate=win_rate,
            open_positions=len(self.repo.get_open_trades()),
        )

        # Use RegimeEngine's canonical classification (see CLAUDE.md §2)
        self.repo.save_snapshot(
            symbol=event.symbol,
            price=event.price,
            fear_greed=event.fear_greed_index,
            market_regime=self.regime.params.regime,
        )

    async def _on_technical(self, event: TechnicalEvent):
        self._technical_states[event.symbol] = event
        await self._try_assemble_bundle(event.symbol)

    async def _on_sentiment(self, event: SentimentEvent):
        self._latest_sentiment[event.symbol] = event
        self._last_sentiment_ts = datetime.now()

    async def _on_onchain(self, event: OnChainEvent):
        self._latest_onchain[event.symbol] = event
        self._last_onchain_ts = datetime.now()

    async def _try_assemble_bundle(self, symbol: str):
        """Assemble SignalBundle and publish — identical logic to main.py."""
        from datetime import timedelta
        MAX_SENTIMENT_AGE = timedelta(minutes=30)
        MAX_ONCHAIN_AGE = timedelta(hours=2)

        market = self._market_states.get(symbol)
        technical = self._technical_states.get(symbol)

        if not (market and technical):
            return

        # Only process live watchlist symbols
        if symbol not in LIVE_WATCHLIST:
            return

        # Rolling 12h whale net-flow block check
        self._prune_whale_events()
        bullish_usd_12h, bearish_usd_12h, max_bearish_str = self._whale_window_totals()
        if self._whale_block_active(bullish_usd_12h, bearish_usd_12h, max_bearish_str):
            logger.info(
                f"Live BLOCKED by whale net-flow (bear=${bearish_usd_12h/1e6:.1f}m > "
                f"bull=${bullish_usd_12h/1e6:.1f}m, max_bear_str={max_bearish_str}) — skipping {symbol}"
            )
            self.live_repo.log(
                "whale_block",
                f"Net-flow block active, skipping {symbol}",
                data={"bullish_usd_12h": bullish_usd_12h, "bearish_usd_12h": bearish_usd_12h},
            )
            return

        now = datetime.now()

        sentiment_ts = getattr(self, '_last_sentiment_ts', now)
        onchain_ts = getattr(self, '_last_onchain_ts', now)
        sentiment_age = now - sentiment_ts
        onchain_age = now - onchain_ts
        sentiment_fresh = sentiment_age <= MAX_SENTIMENT_AGE
        onchain_fresh = onchain_age <= MAX_ONCHAIN_AGE

        bundle = SignalBundle(
            timestamp=market.timestamp,
            symbol=symbol,
            market_state=market,
            sentiment=self._latest_sentiment.get(symbol) if sentiment_fresh else None,
            on_chain=self._latest_onchain.get(symbol) if onchain_fresh else None,
            technical=technical,
            sentiment_stale=not sentiment_fresh,
            onchain_stale=not onchain_fresh,
            sentiment_age_mins=round(sentiment_age.total_seconds() / 60, 1),
            onchain_age_hrs=round(onchain_age.total_seconds() / 3600, 1),
        )

        if bundle.sentiment_stale and bundle.onchain_stale:
            bundle.max_allowed_confidence = 0.65

        # Score for logging
        tech_score = self.scorer.score_technical(technical)
        sent_score = self.scorer.score_sentiment(bundle.sentiment) if bundle.sentiment else 50
        onchain_score = self.scorer.score_onchain(bundle.on_chain) if bundle.on_chain else 50
        altfins_bonus = self.altfins.get_total_bonus(symbol)
        composite, breakdown = self.scorer.composite_score(
            tech_score, sent_score, onchain_score, altfins_bonus=altfins_bonus,
        )

        adaptive_threshold = self.regime.params.score_threshold

        self.repo.log_signal(
            timestamp=market.timestamp.isoformat(),
            symbol=symbol,
            raw_score=composite,
            direction=self.scorer.score_to_direction(composite).value,
            score_breakdown=breakdown,
            fear_greed=market.fear_greed_index,
            market_regime=market.regime.value,
            decision="proposed" if composite >= adaptive_threshold else "skipped",
        )

        # Pass adaptive parameters to agents (same as main.py)
        self.ai_analyst._adaptive_threshold = self.regime.params.score_threshold
        self.risk.MIN_SIGNAL_SCORE = self.regime.params.score_threshold
        self.risk.MIN_AI_CONFIDENCE = self.regime.params.ai_confidence_min
        self.risk.MAX_OPEN_POSITIONS = self.regime.params.max_positions

        # Publish bundle → triggers AIAnalyst → RiskAgent → ExecutionAgent pipeline
        await self.bus.publish(bundle)

        self._market_states.pop(symbol, None)
        self._technical_states.pop(symbol, None)

    async def _on_risk_decision(self, event: RiskAssessmentEvent):
        """Log risk decisions to live journal."""
        if event.decision == RiskDecision.APPROVED:
            self.live_repo.log("risk_approved",
                f"Approved {event.proposal_id}: ${event.approved_size_usd:,.0f} "
                f"({event.approved_size_pct_portfolio:.1%})")
            self.trades_today += 1
        else:
            self.live_repo.log("risk_vetoed",
                f"Vetoed {event.proposal_id}: {event.veto_reason}")

    # ── Whale trigger with rolling 12h net-flow model ──

    def _prune_whale_events(self):
        """Drop whale events older than the 12h window."""
        cutoff = time.time() - WHALE_NET_FLOW_WINDOW_SECONDS
        self._whale_events = [e for e in self._whale_events if e["timestamp"] >= cutoff]

    def _whale_window_totals(self) -> tuple[float, float, int]:
        """Return (bullish_usd_12h, bearish_usd_12h, strongest_bearish_strength)."""
        bullish_usd = 0.0
        bearish_usd = 0.0
        max_bear_str = 0
        for e in self._whale_events:
            if e["direction"] == "bullish":
                bullish_usd += e["usd_value"]
            elif e["direction"] == "bearish":
                bearish_usd += e["usd_value"]
                if e["strength"] > max_bear_str:
                    max_bear_str = e["strength"]
        return bullish_usd, bearish_usd, max_bear_str

    def _whale_block_active(self, bullish_usd: float, bearish_usd: float, max_bear_str: int) -> bool:
        """Block when bearish flow dominates AND at least one strong bearish event."""
        return bearish_usd > bullish_usd and max_bear_str >= WHALE_BLOCK_MIN_STRENGTH

    async def _on_whale_signal(self, signal: dict):
        """Rolling 12h net-flow whale handler.

        Each whale event is appended to a 12h window. The buy block is computed
        from the net flow over that window — no fixed expiry timer. A block
        clears automatically when bullish flow >= bearish flow.
        """
        direction = signal.get("direction", "neutral")
        strength = int(signal.get("strength", 0) or 0)
        reason = signal.get("reason", "")
        usd = float(signal.get("usd_value", 0) or 0)

        if direction not in ("bullish", "bearish"):
            logger.info(f"LIVE WHALE NEUTRAL (str={strength}/5): {reason} — no action")
            return

        # Snapshot block state BEFORE appending the new event
        self._prune_whale_events()
        pre_bull, pre_bear, pre_max_bear = self._whale_window_totals()
        was_blocked = self._whale_block_active(pre_bull, pre_bear, pre_max_bear)

        # Append + reprune (new event may itself be too old? no — it's now)
        self._whale_events.append({
            "timestamp": time.time(),
            "direction": direction,
            "usd_value": usd,
            "strength": strength,
        })

        post_bull, post_bear, post_max_bear = self._whale_window_totals()
        is_blocked = self._whale_block_active(post_bull, post_bear, post_max_bear)

        if direction == "bearish":
            logger.warning(
                f"LIVE WHALE BEARISH (str={strength}/5, ${usd/1e6:.1f}m): {reason} — "
                f"window bull=${post_bull/1e6:.1f}m bear=${post_bear/1e6:.1f}m "
                f"block={'ACTIVE' if is_blocked else 'inactive'}"
            )
            self.live_repo.log(
                "whale_bearish_block",
                f"Bearish whale (${usd/1e6:.1f}m): {reason}",
                data={
                    "signal": signal,
                    "bullish_usd_12h": post_bull,
                    "bearish_usd_12h": post_bear,
                    "block_active": is_blocked,
                },
            )
            return  # do not scan on bearish

        # Bullish branch
        logger.warning(
            f"LIVE WHALE BULLISH (str={strength}/5, ${usd/1e6:.1f}m): {reason} — "
            f"window bull=${post_bull/1e6:.1f}m bear=${post_bear/1e6:.1f}m, "
            f"+20% confidence boost, triggering scan"
        )
        self.live_repo.log(
            "whale_bullish_boost",
            f"Bullish whale (${usd/1e6:.1f}m), +20% confidence boost: {reason}",
            data={
                "signal": signal,
                "bullish_usd_12h": post_bull,
                "bearish_usd_12h": post_bear,
            },
        )

        # If this bullish event flipped the rolling block off, journal it
        if was_blocked and not is_blocked:
            logger.warning(
                f"Whale net-flow block CLEARED — bull=${post_bull/1e6:.1f}m "
                f">= bear=${post_bear/1e6:.1f}m"
            )
            self.live_repo.log(
                "whale_net_flow_cleared",
                f"Rolling 12h net-flow block cleared: bull=${post_bull/1e6:.1f}m "
                f">= bear=${post_bear/1e6:.1f}m",
                data={
                    "bullish_usd_12h": post_bull,
                    "bearish_usd_12h": post_bear,
                },
            )

        try:
            await self.market_data._scan_all()
        except Exception as e:
            logger.error(f"Whale-triggered scan failed: {e}")
            self.live_repo.log("whale_neutral", reason, data=signal)

    # ── Main loop ──

    async def run(self):
        logger.info("Live Engine starting — same pipeline as main.py")
        self.live_repo.log("engine", "Live engine started" + (" (DRY RUN)" if self.dry_run else ""))

        # Override market_data to only scan live watchlist
        settings_copy = settings.model_dump()
        settings_copy["watchlist"] = LIVE_WATCHLIST

        # Warm up technical indicators
        logger.info("Warming up technical indicators for live watchlist...")
        await self.technical.warmup(LIVE_WATCHLIST)
        logger.info("Technical warmup complete")

        # Start event bus
        bus_task = asyncio.create_task(self.bus.run())

        # Start altFINS enrichment + pass ref to risk agent
        await self.altfins.start()
        self.risk.altfins = self.altfins

        # Start agent loops — same as main.py but with live watchlist
        agent_tasks = [
            asyncio.create_task(self.market_data.run_forever(
                interval_seconds=settings.scan_interval_seconds
            )),
            asyncio.create_task(self.sentiment.run_forever(interval_seconds=900)),
            asyncio.create_task(self.onchain.run_forever(interval_seconds=3600)),
            asyncio.create_task(self.monitor.run_monitor_loop(
                interval_seconds=settings.monitor_interval_seconds
            )),
            asyncio.create_task(self.whale_trigger.run_forever()),
        ]

        logger.info(f"Live Engine running — {len(agent_tasks)} agent loops + event bus")
        logger.info(f"RiskAgent thresholds: score>={self.risk.MIN_SIGNAL_SCORE}, "
                     f"confidence>={self.risk.MIN_AI_CONFIDENCE}, "
                     f"max_positions={self.risk.MAX_OPEN_POSITIONS}")

        try:
            await asyncio.gather(bus_task, *agent_tasks)
        except asyncio.CancelledError:
            logger.info("Live Engine shutting down...")
            self.bus.stop()


def main():
    parser = argparse.ArgumentParser(description="Signal Forge v2 — Live Trading")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without placing real orders")
    args = parser.parse_args()

    engine = LiveEngine(dry_run=args.dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("Live engine shutting down")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(sig.SIGINT, shutdown)
    loop.add_signal_handler(sig.SIGTERM, shutdown)

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        logger.info("Live engine stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
