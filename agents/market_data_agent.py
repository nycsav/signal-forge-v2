"""Signal Forge v2 — Market Data Agent

Fetches prices from Coinbase, signals from altFINS, Fear & Greed index.
Emits MarketStateEvent for each tracked asset.
"""

import asyncio
from datetime import datetime
from loguru import logger

from agents.event_bus import EventBus
from agents.events import MarketStateEvent, MarketRegime
from data import coinbase_client, altfins_client, fear_greed_client


class MarketDataAgent:
    def __init__(self, event_bus: EventBus, config: dict):
        self.bus = event_bus
        self.altfins_key = config.get("altfins_api_key", "")
        self.watchlist = config.get("watchlist", [])
        self._fear_greed: int = 50
        self._altfins_signals: list = []

    async def run_forever(self, interval_seconds: int = 900):
        while True:
            try:
                await self._scan_all()
            except Exception as e:
                logger.error(f"MarketDataAgent error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _scan_all(self):
        logger.info(f"MarketDataAgent scanning {len(self.watchlist)} assets...")

        # Fetch F&G and altFINS in parallel
        fg_task = fear_greed_client.get_fear_greed()
        signals_task = altfins_client.get_all_signals(self.altfins_key) if self.altfins_key else asyncio.coroutine(lambda: [])()

        fg, altfins_sigs = await asyncio.gather(fg_task, signals_task, return_exceptions=True)

        if isinstance(fg, dict):
            self._fear_greed = fg.get("value", 50)
        if isinstance(altfins_sigs, list):
            self._altfins_signals = altfins_sigs

        # Fetch prices
        prices = await coinbase_client.get_all_prices(self.watchlist)

        # Emit events
        for symbol, price in prices.items():
            if price <= 0:
                continue
            await self._emit_market_state(symbol, price)

        logger.info(f"MarketDataAgent emitted {sum(1 for p in prices.values() if p > 0)} MarketStateEvents")

    async def _emit_market_state(self, symbol: str, price: float):
        # Find altFINS signals for this symbol
        base = symbol.replace("-USD", "").upper()
        pair_signals = [s for s in self._altfins_signals if s.get("symbol", "").upper() == base]
        bullish = sum(1 for s in pair_signals if s.get("direction") == "BULLISH")
        bearish = sum(1 for s in pair_signals if s.get("direction") == "BEARISH")

        # Simple signal score: -1 (all bearish) to +1 (all bullish)
        total = bullish + bearish
        signal_score = (bullish - bearish) / total if total > 0 else 0

        # Determine regime from F&G and signals
        regime = MarketRegime.RANGING
        if self._fear_greed < 25:
            regime = MarketRegime.BEAR_TREND
        elif self._fear_greed > 75:
            regime = MarketRegime.BULL_TREND
        elif signal_score > 0.3:
            regime = MarketRegime.BULL_TREND
        elif signal_score < -0.3:
            regime = MarketRegime.BEAR_TREND

        event = MarketStateEvent(
            timestamp=datetime.now(),
            symbol=symbol,
            price=price,
            fear_greed_index=self._fear_greed,
            regime=regime,
            altfins_signal_score=signal_score,
        )

        await self.bus.publish(event)
