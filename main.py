#!/usr/bin/env python3
"""Signal Forge v2 — Master Orchestrator

Coordinates all agents via event bus. Manages the main scan loop.
Architecture: 3-tier hierarchy (Strategic → Tactical → Execution).
"""

import asyncio
import signal as sig
from datetime import datetime
from loguru import logger
import uvicorn

from config.settings import settings
from agents.event_bus import EventBus
from agents.events import (
    MarketStateEvent, TechnicalEvent, SentimentEvent, OnChainEvent, SignalBundle
)
from agents.market_data_agent import MarketDataAgent
from agents.technical_agent import TechnicalAgent
from agents.sentiment_agent import SentimentAgent
from agents.onchain_agent import OnChainAgent
from agents.ai_analyst_agent import AIAnalystAgent
from agents.risk_agent import RiskAgent
from agents.execution_agent import ExecutionAgent
from agents.monitor_agent import MonitorAgent
from agents.learning_agent import LearningAgent
from agents.regime_engine import RegimeAdaptiveEngine
from agents.whale_trigger import WhaleTrigger
from agents.chart_pattern_agent import ChartPatternAgent
from agents.altfins_enrichment import AltFINSEnrichment
from agents.scoring import SignalScorer
from db.repository import Repository
from dashboard.app import app as dashboard_app


class SignalForgeOrchestrator:
    """
    Master orchestrator. Initializes all agents, wires event bus,
    and manages the main scan loop.

    Tier 1 (Strategic): This orchestrator
    Tier 2 (Tactical): MarketData, Technical, Sentiment, OnChain, AI Analyst
    Tier 3 (Execution): Risk Agent (+ Execution, Monitor, Learning in Phase 3-4)
    """

    def __init__(self):
        self.bus = EventBus()
        self.repo = Repository(settings.database_path)
        self.scorer = SignalScorer()

        config = settings.model_dump()

        # Tier 2 agents
        self.market_data = MarketDataAgent(self.bus, config)
        self.technical = TechnicalAgent(self.bus)
        self.sentiment = SentimentAgent(self.bus, config)
        self.onchain = OnChainAgent(self.bus, config)
        self.ai_analyst = AIAnalystAgent(self.bus, config, self.scorer)

        # Tier 3 agents
        self.risk = RiskAgent(self.bus, settings.database_path, settings.portfolio_value)
        self.execution = ExecutionAgent(self.bus, config)
        self.monitor = MonitorAgent(self.bus, settings.database_path)
        self.learning = LearningAgent(self.bus, settings.database_path)
        self.whale_trigger = WhaleTrigger(event_bus=self.bus, on_signal=self._on_whale_signal)
        self.chart_patterns = ChartPatternAgent(self.bus)
        self.altfins = AltFINSEnrichment(
            api_key=config.get("altfins_api_key", ""),
            watchlist=config.get("watchlist", []),
        )
        self.regime = RegimeAdaptiveEngine(settings.database_path)

        # Orchestrator state for bundle assembly
        self._market_states: dict = {}
        self._technical_states: dict = {}
        self._latest_sentiment: dict = {}
        self._latest_onchain: dict = {}
        self._last_sentiment_ts: datetime = datetime.now()
        self._last_onchain_ts: datetime = datetime.now()

        # Subscribe orchestrator to assemble SignalBundles
        self.bus.subscribe(MarketStateEvent, self._on_market_state)
        self.bus.subscribe(TechnicalEvent, self._on_technical)
        self.bus.subscribe(SentimentEvent, self._on_sentiment)
        self.bus.subscribe(OnChainEvent, self._on_onchain)

    async def _on_market_state(self, event: MarketStateEvent):
        self._market_states[event.symbol] = event

        # Update regime engine with latest market state
        perf = self.repo.get_performance_stats(7)
        win_rate = perf.get("win_rate", 50) / 100
        self.regime.update(
            fear_greed=event.fear_greed_index,
            market_regime=event.regime,
            avg_atr_pct=event.atr_14 / event.price if event.price > 0 and event.atr_14 > 0 else 0.03,
            recent_win_rate=win_rate,
            open_positions=len(self.repo.get_open_trades()),
        )

        # Log to DB — use RegimeEngine's canonical classification, not the raw
        # MarketDataAgent directional enum. RegimeEngine is the single source
        # of truth for regime state (see CLAUDE.md §2).
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

    async def _on_whale_signal(self, signal: dict):
        """Whale activity detected → trigger immediate market scan."""
        direction = signal.get("direction", "neutral")
        strength = signal.get("strength", 0)
        reason = signal.get("reason", "")

        logger.warning(f"WHALE TRIGGER: {direction.upper()} (strength {strength}/5) — {reason}")
        logger.info("Triggering immediate market scan...")

        # Force an immediate scan cycle
        try:
            await self.market_data._scan_all()
        except Exception as e:
            logger.error(f"Whale-triggered scan failed: {e}")

        # Log the event
        self.repo.log_event("whale_trigger", f"whale_{direction}", None, signal)

    async def _try_assemble_bundle(self, symbol: str):
        from datetime import timedelta
        MAX_SENTIMENT_AGE = timedelta(minutes=30)
        MAX_ONCHAIN_AGE = timedelta(hours=2)

        market = self._market_states.get(symbol)
        technical = self._technical_states.get(symbol)

        if not (market and technical):
            return

        now = datetime.now()

        # Staleness checks
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

        # If both context sources are stale, raise the bar
        if bundle.sentiment_stale and bundle.onchain_stale:
            bundle.max_allowed_confidence = 0.65

        # Log the signal (with altFINS enrichment bonus)
        tech_score = self.scorer.score_technical(technical)
        sent_score = self.scorer.score_sentiment(bundle.sentiment) if bundle.sentiment else 50
        onchain_score = self.scorer.score_onchain(bundle.on_chain) if bundle.on_chain else 50
        altfins_bonus = self.altfins.get_total_bonus(symbol)
        composite, breakdown = self.scorer.composite_score(
            tech_score, sent_score, onchain_score, altfins_bonus=altfins_bonus,
        )

        # Use adaptive threshold instead of fixed
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

        # Pass adaptive parameters to agents
        self.ai_analyst._adaptive_threshold = self.regime.params.score_threshold
        self.risk.MIN_SIGNAL_SCORE = self.regime.params.score_threshold
        self.risk.MIN_AI_CONFIDENCE = self.regime.params.ai_confidence_min
        self.risk.MAX_OPEN_POSITIONS = self.regime.params.max_positions

        await self.bus.publish(bundle)

        # Clear consumed states to avoid re-processing
        self._market_states.pop(symbol, None)
        self._technical_states.pop(symbol, None)

    async def run(self):
        logger.info("Signal Forge v2 starting...")
        logger.info(f"Mode: {settings.mode} | Watchlist: {len(settings.watchlist)} coins")
        logger.info(f"Ollama: {settings.ollama_host} | Models: {settings.deepseek_model}, {settings.fast_model}")
        logger.info(f"Dashboard: http://localhost:{settings.dashboard_port}")

        # Warm up technical indicators with historical data
        logger.info("Warming up technical indicators...")
        await self.technical.warmup(settings.watchlist[:8])  # Top 8 coins to avoid rate limits
        logger.info("Technical warmup complete — indicators ready for immediate signals")

        # Start event bus
        bus_task = asyncio.create_task(self.bus.run())

        # Start altFINS enrichment background polling (patterns 4h, screener 15m)
        await self.altfins.start()

        # Pass enrichment ref to RiskAgent for pre-execution checks
        self.risk.altfins = self.altfins

        # Start agent loops
        agent_tasks = [
            asyncio.create_task(self.market_data.run_forever(
                interval_seconds=settings.scan_interval_seconds
            )),
            asyncio.create_task(self.sentiment.run_forever(interval_seconds=900)),
            asyncio.create_task(self.onchain.run_forever(interval_seconds=3600)),
            asyncio.create_task(self.monitor.run_monitor_loop(
                interval_seconds=settings.monitor_interval_seconds
            )),
            asyncio.create_task(self.whale_trigger.run_forever()),  # 60s whale monitoring
            asyncio.create_task(self.chart_patterns.run_forever(interval_seconds=14400)),  # 4h pattern scan
        ]

        # Dashboard runs separately on port 8888 (dashboard_server.py)
        # Don't start another one here — avoids port conflicts

        logger.info(f"Signal Forge v2 running — {len(agent_tasks)} agent loops + event bus")

        try:
            await asyncio.gather(bus_task, *agent_tasks)
        except asyncio.CancelledError:
            logger.info("Signal Forge v2 shutting down...")
            self.bus.stop()


def main():
    orchestrator = SignalForgeOrchestrator()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(sig.SIGINT, shutdown)
    loop.add_signal_handler(sig.SIGTERM, shutdown)

    try:
        loop.run_until_complete(orchestrator.run())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
