"""Signal Forge v2 — Whale-Triggered Entry Strategy

Uses Arkham whale flow data as the PRIMARY entry signal.
When smart money accumulates (strength 4+, >$5M), enter long on that token.

Coinbase Research confirmed: "Long-term holder supply rising + exchange
balances falling = accumulation. Strategy's purchases matter more at
the margin when available supply is already becoming less accessible."

This strategy doesn't predict direction — it follows institutional flow.
"""

import uuid
from datetime import datetime, timedelta
from loguru import logger

from agents.event_bus import EventBus, Priority
from agents.events import TradeProposal, Direction


class WhaleEntryStrategy:
    """Enter trades triggered by whale accumulation events."""

    MIN_STRENGTH = 3           # whale event strength (1-5 scale)
    MIN_USD_VALUE = 1_000_000  # minimum whale transaction size
    COOLDOWN_MINUTES = 180     # 3 hours between entries per symbol
    TRADE_SIZE_USD = 1000      # $1K per whale-triggered trade

    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._last_entry: dict[str, datetime] = {}
        self._pending_symbols: dict[str, dict] = {}  # symbol → whale event data

    def on_whale_signal(self, signal: dict):
        """Called by orchestrator when whale trigger fires.

        signal: {
            "direction": "bullish"|"bearish",
            "strength": 1-5,
            "reason": str,
            "token": str,
            "usd_value": float,
            ...
        }
        """
        direction = signal.get("direction", "neutral")
        strength = signal.get("strength", 0)
        usd_value = signal.get("usd_value", 0)
        token = signal.get("token", "").upper()

        # Only act on bullish whale events with sufficient strength
        if direction != "bullish":
            return
        if strength < self.MIN_STRENGTH:
            return
        if usd_value < self.MIN_USD_VALUE:
            return

        # Map token to tradeable symbol
        symbol = self._map_to_symbol(token, signal)
        if not symbol:
            return

        # Cooldown check
        last = self._last_entry.get(symbol)
        if last and (datetime.now() - last).total_seconds() < self.COOLDOWN_MINUTES * 60:
            return

        # Queue for entry on next price update
        self._pending_symbols[symbol] = {
            "strength": strength,
            "usd_value": usd_value,
            "reason": signal.get("reason", ""),
            "queued_at": datetime.now(),
        }

        logger.warning(
            f"WHALE QUEUE: {symbol} str={strength}/5 ${usd_value:,.0f} — {signal.get('reason', '')}"
        )

    async def check_and_enter(self, symbol: str, price: float, atr_pct: float):
        """Called during scan loop to execute queued whale entries.

        Args:
            symbol: token symbol (e.g. "BTC-USD")
            price: current price
            atr_pct: ATR as percentage of price
        """
        if symbol not in self._pending_symbols:
            return

        pending = self._pending_symbols.pop(symbol)

        # Expire stale signals (>30 min old)
        if (datetime.now() - pending["queued_at"]).total_seconds() > 1800:
            logger.info(f"WHALE EXPIRED: {symbol} signal too old (>30 min)")
            return

        atr = price * atr_pct if atr_pct > 0 else price * 0.03
        risk = atr * 2.5
        confidence = min(0.90, 0.60 + pending["strength"] * 0.06)

        proposal = TradeProposal(
            timestamp=datetime.now(),
            proposal_id=str(uuid.uuid4()),
            symbol=symbol,
            direction=Direction.LONG,
            raw_score=75.0,  # whale entries get a fixed high score
            ai_confidence=confidence,
            ai_rationale=(
                f"WHALE ENTRY: str={pending['strength']}/5 "
                f"${pending['usd_value']:,.0f} — {pending['reason']}"
            ),
            suggested_entry=price,
            suggested_stop=price - risk,
            suggested_tp1=price + risk * 2.0,
            suggested_tp2=price + risk * 4.0,
            suggested_tp3=price + risk * 6.0,
        )

        logger.warning(
            f"WHALE ENTRY: {symbol} at ${price:,.2f} "
            f"str={pending['strength']}/5 conf={confidence:.0%}"
        )
        self._last_entry[symbol] = datetime.now()
        await self.bus.publish(proposal, priority=Priority.HIGH)

    def _map_to_symbol(self, token: str, signal: dict) -> str:
        """Map whale event token name to Alpaca-tradeable symbol."""
        # Common mappings
        token_map = {
            "BTC": "BTC-USD", "BITCOIN": "BTC-USD",
            "ETH": "ETH-USD", "ETHEREUM": "ETH-USD", "ETHER": "ETH-USD",
            "SOL": "SOL-USD", "SOLANA": "SOL-USD",
            "DOGE": "DOGE-USD", "DOGECOIN": "DOGE-USD",
            "LINK": "LINK-USD", "CHAINLINK": "LINK-USD",
            "ADA": "ADA-USD", "CARDANO": "ADA-USD",
            "AVAX": "AVAX-USD", "AVALANCHE": "AVAX-USD",
            "DOT": "DOT-USD", "POLKADOT": "DOT-USD",
            "XRP": "XRP-USD", "RIPPLE": "XRP-USD",
            "USDC": None, "USDT": None, "USD COIN": None,  # stablecoins — don't trade
        }

        # Try direct token name
        if token in token_map:
            return token_map[token]

        # Try from reason text
        reason = signal.get("reason", "").upper()
        for name, sym in token_map.items():
            if name in reason and sym:
                return sym

        # Try from_entity / to_entity
        from_entity = signal.get("from_entity", "").upper()
        to_entity = signal.get("to_entity", "").upper()

        # If withdrawn from exchange → accumulation signal for that token
        if "COINBASE" in from_entity or "BINANCE" in from_entity or "BYBIT" in from_entity:
            # This is a withdrawal — the token field might help
            if token and token + "-USD" in token_map.values():
                return token + "-USD"

        return None

    @property
    def pending_count(self) -> int:
        return len(self._pending_symbols)
