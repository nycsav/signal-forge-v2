"""Signal Forge v2 — Market Data Agent (Full Stack)

Pulls from ALL data sources and synthesizes into MarketStateEvent:
- Coinbase: live prices
- CoinMarketCap: global metrics, volume spikes, market momentum
- altFINS: technical signals (bullish/bearish)
- Fear & Greed: sentiment
- Arkham: whale activity, exchange flows
- CoinGecko: trending coins

The AI Analyst sees ALL of this in its prompt.
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
        self._cmc_global: dict = {}
        self._cmc_volume_spikes: list = []
        self._arkham_whales: list = []
        self._market_momentum: float = 0  # -1 to +1

    async def run_forever(self, interval_seconds: int = 900):
        while True:
            try:
                await self._scan_all()
            except Exception as e:
                logger.error(f"MarketDataAgent error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _scan_all(self):
        logger.info(f"MarketDataAgent scanning {len(self.watchlist)} assets (full stack)...")

        # Parallel fetch from ALL sources
        tasks = [
            fear_greed_client.get_fear_greed(),
            self._fetch_cmc_data(),
            self._fetch_arkham_data(),
        ]
        if self.altfins_key:
            tasks.append(altfins_client.get_all_signals(self.altfins_key))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        if isinstance(results[0], dict):
            self._fear_greed = results[0].get("value", 50)

        # CMC data (from _fetch_cmc_data)
        # Arkham data (from _fetch_arkham_data)

        if len(results) > 3 and isinstance(results[3], list):
            self._altfins_signals = results[3]

        # Calculate overall market momentum from CMC
        self._market_momentum = self._calc_momentum()

        # Fetch prices
        prices = await coinbase_client.get_all_prices(self.watchlist)

        # Emit events with enriched data
        for symbol, price in prices.items():
            if price <= 0:
                continue
            await self._emit_market_state(symbol, price)

        momentum_label = "BULLISH" if self._market_momentum > 0.3 else "BEARISH" if self._market_momentum < -0.3 else "NEUTRAL"
        logger.info(
            f"MarketDataAgent: {sum(1 for p in prices.values() if p > 0)} events | "
            f"F&G={self._fear_greed} | Momentum={momentum_label} ({self._market_momentum:+.2f}) | "
            f"Vol spikes={len(self._cmc_volume_spikes)} | Whales={len(self._arkham_whales)}"
        )

    async def _fetch_cmc_data(self):
        """Fetch CoinMarketCap global metrics and volume spikes."""
        try:
            from data.coinmarketcap_client import CoinMarketCapClient
            cmc = CoinMarketCapClient()
            if not cmc.enabled:
                return

            self._cmc_global = await cmc.get_global_metrics()
            self._cmc_volume_spikes = await cmc.get_volume_spikes(50)

            change = self._cmc_global.get("total_market_cap_yesterday_pct_change", 0)
            if abs(change) > 2:
                logger.info(f"CMC: Market cap {change:+.1f}% today | {len(self._cmc_volume_spikes)} volume spikes")
        except Exception as e:
            logger.debug(f"CMC fetch error: {e}")

    async def _fetch_arkham_data(self):
        """Fetch Arkham whale transfers."""
        try:
            from data.arkham_client import ArkhamClient
            ark = ArkhamClient()
            if not ark.enabled:
                return

            self._arkham_whales = await ark.get_whale_transfers(min_usd=1000000, time_last='4h', limit=10)
            if self._arkham_whales:
                logger.info(f"Arkham: {len(self._arkham_whales)} whale transfers (4h, >$1M)")
        except Exception as e:
            logger.debug(f"Arkham fetch error: {e}")

    def _calc_momentum(self) -> float:
        """Calculate overall market momentum from all sources. Returns -1 to +1."""
        score = 0

        # CMC market cap change
        mcap_change = self._cmc_global.get("total_market_cap_yesterday_pct_change", 0)
        if mcap_change > 3: score += 0.4
        elif mcap_change > 1: score += 0.2
        elif mcap_change < -3: score -= 0.4
        elif mcap_change < -1: score -= 0.2

        # Volume spikes (more spikes = money flowing in)
        if len(self._cmc_volume_spikes) >= 5: score += 0.2
        elif len(self._cmc_volume_spikes) >= 3: score += 0.1

        # Fear & Greed (contrarian: extreme fear + market rising = strong buy)
        if self._fear_greed < 20 and mcap_change > 0:
            score += 0.3  # Fear + green = accumulation phase
        elif self._fear_greed > 80:
            score -= 0.2  # Greed = caution

        # altFINS signal balance
        bullish = sum(1 for s in self._altfins_signals if s.get("direction") == "BULLISH")
        bearish = sum(1 for s in self._altfins_signals if s.get("direction") == "BEARISH")
        if bullish + bearish > 0:
            signal_ratio = (bullish - bearish) / (bullish + bearish)
            score += signal_ratio * 0.2

        return max(-1, min(1, score))

    async def _emit_market_state(self, symbol: str, price: float):
        base = symbol.replace("-USD", "").upper()
        pair_signals = [s for s in self._altfins_signals if s.get("symbol", "").upper() == base]
        bullish = sum(1 for s in pair_signals if s.get("direction") == "BULLISH")
        bearish = sum(1 for s in pair_signals if s.get("direction") == "BEARISH")
        total = bullish + bearish
        signal_score = (bullish - bearish) / total if total > 0 else 0

        # Check if this coin has a CMC volume spike
        is_spiking = any(s["symbol"] == base for s in self._cmc_volume_spikes)
        if is_spiking:
            signal_score = min(1.0, signal_score + 0.3)  # Boost signal for volume spike coins

        # Determine regime using ALL data
        regime = MarketRegime.RANGING
        mcap_change = self._cmc_global.get("total_market_cap_yesterday_pct_change", 0)

        if mcap_change > 3 and self._market_momentum > 0.3:
            regime = MarketRegime.BULL_TREND  # Market-wide rally
        elif mcap_change < -3 and self._market_momentum < -0.3:
            regime = MarketRegime.BEAR_TREND  # Market-wide selloff
        elif self._fear_greed < 20 and mcap_change > 0:
            regime = MarketRegime.BULL_TREND  # Fear + green candles = reversal
        elif self._fear_greed < 25:
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
            volume_24h=0,  # Could be enriched from CMC
            price_change_24h_pct=mcap_change,  # Use market-wide as proxy
        )

        await self.bus.publish(event)
