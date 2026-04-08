"""Signal Forge v2 — Whale Activity Trigger

Two scan modes:
  1. Global scan (every 60s): large transfers across all chains
  2. Per-asset scan (every 15 min): top 10 watchlist assets via Arkham transfers endpoint

When entity-labeled wallet moves >$1M into/out of an asset:
  → Publishes WhaleEvent at HIGH priority to event bus

Signals:
- Stablecoins deposited to exchange = about to buy crypto (BULLISH)
- Crypto deposited to exchange = about to sell (BEARISH)
- Large stablecoin mints = new money entering (BULLISH)
- Smart money moving = follow the institutions
"""

import asyncio
import time
from datetime import datetime
from loguru import logger

from data.arkham_client import ArkhamClient
from agents.event_bus import EventBus, Priority
from config.settings import settings


# Top 10 assets to scan per-asset
TOP_WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "DOT", "DOGE", "UNI"]

# Arkham entity names mapped to token symbols for per-asset lookup
ASSET_TO_ARKHAM = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp",
    "ADA": "cardano", "AVAX": "avalanche", "LINK": "chainlink", "DOT": "polkadot",
    "DOGE": "dogecoin", "UNI": "uniswap",
}


class WhaleTrigger:
    """Polls Arkham for whale activity. Fires HIGH priority events on significant moves."""

    GLOBAL_POLL_INTERVAL = 60       # Global scan every 60s
    ASSET_POLL_INTERVAL = 900       # Per-asset scan every 15 min
    MIN_GLOBAL_USD = 2_000_000      # $2M for global scan
    MIN_ASSET_USD = 500_000         # $500K for per-asset scan
    MIN_EVENT_USD = 1_000_000       # $1M to publish a WhaleEvent

    BRIDGE_TAGS = {"bridge", "wrapped", "relay", "cross-chain", "wormhole", "layerzero"}
    STABLECOIN_TOKENS = {"usdt", "usdc", "dai", "busd", "tusd", "usd coin", "tether", "paypal usd"}
    EXCHANGE_ENTITIES = {"binance", "coinbase", "kraken", "bybit", "okx", "gate", "kucoin", "bitfinex", "gemini"}
    SMART_MONEY = {"wintermute", "jump-trading", "jump-crypto", "galaxy-digital", "paradigm", "a16z", "three-arrows", "alameda"}

    def __init__(self, event_bus: EventBus = None, on_signal=None):
        self.ark = ArkhamClient()
        self.bus = event_bus
        self.on_signal = on_signal
        self._seen_txs: set[str] = set()
        self._last_signal_time: float = 0
        self._cooldown = 120

    async def run_forever(self):
        """Main loop: global scan every 60s, per-asset scan every 15 min."""
        if not self.ark.enabled:
            logger.warning("WhaleTrigger: Arkham not configured, running in passive mode")
            while True:
                await asyncio.sleep(300)
            return

        logger.info("WhaleTrigger: global scan every 60s + per-asset scan every 15min")

        cycle = 0
        while True:
            try:
                # Global scan every cycle
                await self._global_check()

                # Per-asset scan every 15th cycle (15 × 60s = 15 min)
                cycle += 1
                if cycle % 15 == 1:
                    await self._per_asset_check()

            except Exception as e:
                logger.error(f"WhaleTrigger error: {e}")
            await asyncio.sleep(self.GLOBAL_POLL_INTERVAL)

    async def _global_check(self):
        """Check for large transfers across all chains."""
        transfers = await self.ark.get_whale_transfers(
            min_usd=self.MIN_GLOBAL_USD,
            time_last="30m",
            limit=20,
        )
        if not transfers:
            return

        for tx in transfers:
            tx_id = tx.get("tx_hash", "")
            if tx_id in self._seen_txs:
                continue
            self._seen_txs.add(tx_id)

            signal = self._classify_transfer(tx)
            if signal and signal["strength"] >= 2:
                await self._fire_signal(signal)

        if len(self._seen_txs) > 1000:
            self._seen_txs = set(list(self._seen_txs)[-500:])

    async def _per_asset_check(self):
        """Check each top 10 asset for entity-labeled wallet movements >$500K."""
        logger.info(f"WhaleTrigger: per-asset scan for {len(TOP_WATCHLIST)} assets...")
        found = 0

        for asset in TOP_WATCHLIST:
            try:
                transfers = await self.ark.get_whale_transfers(
                    min_usd=self.MIN_ASSET_USD,
                    time_last="30m",
                    limit=10,
                )
                # Filter for this specific asset
                asset_transfers = [
                    t for t in transfers
                    if asset.lower() in (t.get("token") or "").lower()
                    or asset.lower() in (t.get("from") or "").lower()
                    or asset.lower() in (t.get("to") or "").lower()
                ]

                for tx in asset_transfers:
                    tx_id = tx.get("tx_hash", "")
                    if tx_id in self._seen_txs:
                        continue
                    self._seen_txs.add(tx_id)

                    usd = tx.get("usd_value", 0)
                    from_label = tx.get("from_label", "Unknown")
                    to_label = tx.get("to_label", "Unknown")

                    # Only publish WhaleEvent for >$1M entity-labeled moves
                    if usd >= self.MIN_EVENT_USD and (from_label != "Unknown" or to_label != "Unknown"):
                        direction = self._infer_direction(tx)
                        signal = {
                            "type": "per_asset_whale",
                            "asset": asset,
                            "direction": direction,
                            "strength": 4 if usd >= 5_000_000 else 3,
                            "usd_value": usd,
                            "from_entity": from_label,
                            "to_entity": to_label,
                            "token": tx.get("token", asset),
                            "chain": tx.get("chain", ""),
                            "reason": f"{from_label} → {to_label}: ${usd:,.0f} {asset} ({direction})",
                            "timestamp": datetime.now().isoformat(),
                        }
                        await self._fire_signal(signal)
                        found += 1

                await asyncio.sleep(1)  # Arkham rate limit between assets

            except Exception as e:
                logger.debug(f"WhaleTrigger per-asset {asset} error: {e}")

        if found > 0:
            logger.info(f"WhaleTrigger: per-asset scan found {found} significant movements")

    def _infer_direction(self, tx: dict) -> str:
        """Infer buy/sell direction from transfer context."""
        token = (tx.get("token") or "").lower()
        from_label = (tx.get("from_label") or "").lower()
        to_label = (tx.get("to_label") or "").lower()
        is_stablecoin = any(s in token for s in self.STABLECOIN_TOKENS)
        to_exchange = any(e in to_label for e in self.EXCHANGE_ENTITIES)
        from_exchange = any(e in from_label for e in self.EXCHANGE_ENTITIES)

        if is_stablecoin and to_exchange:
            return "bullish"  # Stables to exchange = preparing to buy
        elif not is_stablecoin and to_exchange:
            return "bearish"  # Crypto to exchange = preparing to sell
        elif not is_stablecoin and from_exchange:
            return "bullish"  # Crypto leaving exchange = accumulation
        return "neutral"

    def _classify_transfer(self, tx: dict) -> dict | None:
        """Classify a global whale transfer into a trading signal."""
        token = (tx.get("token") or "").lower()
        from_label = (tx.get("from_label") or "").lower()
        to_label = (tx.get("to_label") or "").lower()
        from_id = (tx.get("from") or "").lower()
        to_id = (tx.get("to") or "").lower()
        usd = tx.get("usd_value", 0)
        chain = tx.get("chain", "")

        is_stablecoin = any(s in token for s in self.STABLECOIN_TOKENS)
        to_exchange = any(e in to_label or e in to_id for e in self.EXCHANGE_ENTITIES)
        from_exchange = any(e in from_label or e in from_id for e in self.EXCHANGE_ENTITIES)
        is_smart_money = any(s in from_label or s in from_id or s in to_label or s in to_id for s in self.SMART_MONEY)

        # Filter: ignore bridge/relay noise
        all_tags = f"{from_label} {to_label} {from_id} {to_id}"
        if any(b in all_tags for b in self.BRIDGE_TAGS):
            return None

        signal = {
            "type": None,
            "direction": "neutral",
            "strength": 0,
            "reason": "",
            "token": token,
            "from_entity": from_label or from_id[:10],
            "to_entity": to_label or to_id[:10],
            "usd_value": usd,
            "chain": chain,
            "timestamp": datetime.now().isoformat(),
        }

        if is_stablecoin and to_exchange:
            signal["type"] = "stables_to_exchange"
            signal["direction"] = "bullish"
            signal["strength"] = 4 if is_smart_money else 3
            signal["reason"] = f"{'Smart money' if is_smart_money else 'Whale'} depositing {token} to {to_label or to_id[:10]} — likely preparing to buy crypto"

        elif not is_stablecoin and to_exchange:
            signal["type"] = "crypto_to_exchange"
            signal["direction"] = "bearish"
            signal["strength"] = 3 if is_smart_money else 2
            signal["reason"] = f"{'Smart money' if is_smart_money else 'Whale'} depositing {token} to exchange — potential sell"

        elif not is_stablecoin and from_exchange:
            signal["type"] = "crypto_from_exchange"
            signal["direction"] = "bullish"
            signal["strength"] = 3
            signal["reason"] = f"Crypto withdrawn from {from_label or from_id[:10]} — accumulation"

        elif is_smart_money:
            signal["type"] = "smart_money_move"
            signal["direction"] = "bullish" if is_stablecoin else "neutral"
            signal["strength"] = 2
            signal["reason"] = f"Smart money ({from_label or to_label}) moving {token}"

        elif is_stablecoin and ("tether" in from_label or "circle" in from_label) and usd > 10_000_000:
            signal["type"] = "stablecoin_mint"
            signal["direction"] = "bullish"
            signal["strength"] = 4
            signal["reason"] = f"${usd/1e6:.0f}M {token} minted — new capital entering"

        if signal["type"]:
            return signal
        return None

    async def _fire_signal(self, signal: dict):
        """Fire a whale signal at HIGH priority."""
        now = time.time()
        if now - self._last_signal_time < self._cooldown:
            return

        self._last_signal_time = now

        logger.warning(
            f"WHALE [{signal['direction'].upper()}] str={signal.get('strength',0)}/5: "
            f"{signal['reason']}"
        )

        # Publish to event bus at HIGH priority
        if self.bus:
            await self.bus.publish(signal, priority=Priority.HIGH)

        # Also fire callback for orchestrator
        if self.on_signal:
            try:
                await self.on_signal(signal)
            except Exception as e:
                logger.error(f"WhaleTrigger callback error: {e}")

    def get_status(self) -> dict:
        return {
            "enabled": self.ark.enabled,
            "global_interval": self.GLOBAL_POLL_INTERVAL,
            "asset_interval": self.ASSET_POLL_INTERVAL,
            "assets_tracked": TOP_WATCHLIST,
            "min_global_usd": self.MIN_GLOBAL_USD,
            "min_asset_usd": self.MIN_ASSET_USD,
            "min_event_usd": self.MIN_EVENT_USD,
            "seen_transactions": len(self._seen_txs),
        }
