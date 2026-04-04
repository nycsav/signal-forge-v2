#!/usr/bin/env python3
"""Signal Forge v2 — Master Orchestrator

Coordinates all agents via event bus. Manages the main scan loop.
Architecture: 3-tier hierarchy (Strategic → Tactical → Execution).
"""

import asyncio
import signal as sig
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

        # Orchestrator state for bundle assembly
        self._market_states: dict = {}
        self._technical_states: dict = {}
        self._latest_sentiment: dict = {}
        self._latest_onchain: dict = {}

        # Subscribe orchestrator to assemble SignalBundles
        self.bus.subscribe(MarketStateEvent, self._on_market_state)
        self.bus.subscribe(TechnicalEvent, self._on_technical)
        self.bus.subscribe(SentimentEvent, self._on_sentiment)
        self.bus.subscribe(OnChainEvent, self._on_onchain)

    async def _on_market_state(self, event: MarketStateEvent):
        self._market_states[event.symbol] = event
        # Log to DB
        self.repo.save_snapshot(
            symbol=event.symbol,
            price=event.price,
            fear_greed=event.fear_greed_index,
            market_regime=event.regime.value,
        )

    async def _on_technical(self, event: TechnicalEvent):
        self._technical_states[event.symbol] = event
        await self._try_assemble_bundle(event.symbol)

    async def _on_sentiment(self, event: SentimentEvent):
        self._latest_sentiment[event.symbol] = event

    async def _on_onchain(self, event: OnChainEvent):
        self._latest_onchain[event.symbol] = event

    async def _try_assemble_bundle(self, symbol: str):
        market = self._market_states.get(symbol)
        technical = self._technical_states.get(symbol)

        if not (market and technical):
            return

        bundle = SignalBundle(
            timestamp=market.timestamp,
            symbol=symbol,
            market_state=market,
            sentiment=self._latest_sentiment.get(symbol),
            on_chain=self._latest_onchain.get(symbol),
            technical=technical,
        )

        # Log the signal
        tech_score = self.scorer.score_technical(technical)
        sent_score = self.scorer.score_sentiment(bundle.sentiment) if bundle.sentiment else 50
        onchain_score = self.scorer.score_onchain(bundle.on_chain) if bundle.on_chain else 50
        composite, breakdown = self.scorer.composite_score(tech_score, sent_score, onchain_score)

        self.repo.log_signal(
            timestamp=market.timestamp.isoformat(),
            symbol=symbol,
            raw_score=composite,
            direction=self.scorer.score_to_direction(composite).value,
            score_breakdown=breakdown,
            fear_greed=market.fear_greed_index,
            market_regime=market.regime.value,
            decision="proposed" if composite >= settings.min_signal_score else "skipped",
        )

        await self.bus.publish(bundle)

        # Clear consumed states to avoid re-processing
        self._market_states.pop(symbol, None)
        self._technical_states.pop(symbol, None)

    async def run(self):
        logger.info("Signal Forge v2 starting...")
        logger.info(f"Mode: {settings.mode} | Watchlist: {len(settings.watchlist)} coins")
        logger.info(f"Ollama: {settings.ollama_host} | Models: {settings.deepseek_model}, {settings.fast_model}")
        logger.info(f"Dashboard: http://localhost:{settings.dashboard_port}")

        # Start event bus
        bus_task = asyncio.create_task(self.bus.run())

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
        ]

        # Start dashboard
        dashboard_task = asyncio.create_task(
            uvicorn.Server(
                uvicorn.Config(
                    dashboard_app,
                    host="0.0.0.0",
                    port=settings.dashboard_port,
                    log_level="warning",
                )
            ).serve()
        )

        logger.info(f"Signal Forge v2 running — {len(agent_tasks)} agent loops + dashboard + event bus")

        try:
            await asyncio.gather(bus_task, dashboard_task, *agent_tasks)
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
