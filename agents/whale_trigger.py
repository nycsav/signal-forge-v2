"""Signal Forge v2 — Whale Activity Trigger

Monitors Arkham whale transfers every 60 seconds.
When significant smart money movement detected → triggers immediate market scan.
This is the LEADING indicator that catches moves BEFORE they happen.

Signals:
- Stablecoins deposited to exchange = about to buy crypto (BULLISH)
- Crypto deposited to exchange = about to sell (BEARISH)
- Large stablecoin mints (Tether/Circle) = new money entering market (BULLISH)
- Smart money (Wintermute/Jump/Galaxy) moving = follow the institutions
"""

import asyncio
import time
from datetime import datetime
from loguru import logger

from data.arkham_client import ArkhamClient
from config.settings import settings


class WhaleTrigger:
    """Polls Arkham every 60s. Fires callback when whale activity detected."""

    # Thresholds
    POLL_INTERVAL = 60          # Check every 60 seconds
    MIN_TRANSFER_USD = 500_000  # $500K minimum to care
    STABLECOIN_TOKENS = {"usdt", "usdc", "dai", "busd", "tusd", "usd coin", "tether", "paypal usd"}
    EXCHANGE_ENTITIES = {"binance", "coinbase", "kraken", "bybit", "okx", "gate", "kucoin", "bitfinex", "gemini"}
    SMART_MONEY = {"wintermute", "jump-trading", "jump-crypto", "galaxy-digital", "paradigm", "a16z", "three-arrows", "alameda"}

    def __init__(self, on_signal=None):
        self.ark = ArkhamClient()
        self.on_signal = on_signal  # Callback: async def on_signal(signal: dict)
        self._seen_txs: set[str] = set()
        self._last_signal_time: float = 0
        self._cooldown = 120  # Don't fire more than once per 2 min

    async def run_forever(self):
        """Main polling loop — checks Arkham every 60 seconds."""
        if not self.ark.enabled:
            logger.warning("WhaleTrigger: Arkham not configured, running in passive mode")
            while True:
                await asyncio.sleep(300)
            return

        logger.info("WhaleTrigger: monitoring whale activity every 60s")

        while True:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"WhaleTrigger error: {e}")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _check(self):
        """Check for significant whale activity."""
        transfers = await self.ark.get_whale_transfers(
            min_usd=self.MIN_TRANSFER_USD,
            time_last="30m",  # Only last 30 minutes
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

        # Cap seen set
        if len(self._seen_txs) > 1000:
            self._seen_txs = set(list(self._seen_txs)[-500:])

    def _classify_transfer(self, tx: dict) -> dict | None:
        """Classify a whale transfer into a trading signal."""
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

        signal = {
            "type": None,
            "direction": "neutral",
            "strength": 0,  # 0-5
            "reason": "",
            "token": token,
            "from": from_label or from_id[:10],
            "to": to_label or to_id[:10],
            "usd_value": usd,
            "chain": chain,
            "timestamp": datetime.now().isoformat(),
        }

        # Stablecoins TO exchange = preparing to buy crypto → BULLISH
        if is_stablecoin and to_exchange:
            signal["type"] = "stables_to_exchange"
            signal["direction"] = "bullish"
            signal["strength"] = 4 if is_smart_money else 3
            signal["reason"] = f"{'Smart money' if is_smart_money else 'Whale'} depositing {token} to {to_label or to_id[:10]} — likely preparing to buy crypto"

        # Crypto TO exchange = preparing to sell → BEARISH
        elif not is_stablecoin and to_exchange:
            signal["type"] = "crypto_to_exchange"
            signal["direction"] = "bearish"
            signal["strength"] = 3 if is_smart_money else 2
            signal["reason"] = f"{'Smart money' if is_smart_money else 'Whale'} depositing {token} to exchange — potential sell incoming"

        # Crypto FROM exchange = accumulation → BULLISH
        elif not is_stablecoin and from_exchange:
            signal["type"] = "crypto_from_exchange"
            signal["direction"] = "bullish"
            signal["strength"] = 3
            signal["reason"] = f"Crypto withdrawn from {from_label or from_id[:10]} — accumulation/cold storage"

        # Smart money moving anything significant
        elif is_smart_money:
            signal["type"] = "smart_money_move"
            signal["direction"] = "bullish" if is_stablecoin else "neutral"
            signal["strength"] = 2
            signal["reason"] = f"Smart money ({from_label or to_label}) moving {token}"

        # Large stablecoin mint (Tether/Circle)
        elif is_stablecoin and ("tether" in from_label or "circle" in from_label) and usd > 10_000_000:
            signal["type"] = "stablecoin_mint"
            signal["direction"] = "bullish"
            signal["strength"] = 4
            signal["reason"] = f"${usd/1e6:.0f}M {token} minted — new capital entering crypto"

        if signal["type"]:
            return signal
        return None

    async def _fire_signal(self, signal: dict):
        """Fire a whale signal — triggers immediate market scan."""
        now = time.time()
        if now - self._last_signal_time < self._cooldown:
            logger.debug(f"WhaleTrigger: cooldown active, skipping {signal['type']}")
            return

        self._last_signal_time = now

        logger.warning(
            f"WHALE SIGNAL [{signal['direction'].upper()}] strength={signal['strength']}/5: "
            f"{signal['reason']} ({signal['token']} ${signal['usd_value']:,.0f} on {signal['chain']})"
        )

        if self.on_signal:
            try:
                await self.on_signal(signal)
            except Exception as e:
                logger.error(f"WhaleTrigger callback error: {e}")

    def get_status(self) -> dict:
        return {
            "enabled": self.ark.enabled,
            "poll_interval": self.POLL_INTERVAL,
            "seen_transactions": len(self._seen_txs),
            "last_signal": self._last_signal_time,
        }
