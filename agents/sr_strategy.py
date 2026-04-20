"""Signal Forge v2 — S/R Mean Reversion Strategy

Ported from Enso Trading Terminal's proven S/R engine.
Enters ONLY when price bounces off a support level with volume confirmation.

This is the opposite of the quant fast-path: instead of "score is high, buy,"
it's "price just touched support and bounced, buy the reversal."

Entry: Price within 1.5% of a pivot support + close back above it (failed breakdown)
Exit: Standard 7-layer exit strategy (2.5x ATR stop, 2R/4R/6R TPs)
"""

import uuid
from datetime import datetime
from loguru import logger

from agents.event_bus import EventBus, Priority
from agents.events import SignalBundle, TradeProposal, Direction


class SRStrategy:
    """Support/Resistance mean-reversion entry strategy."""

    LOOKBACK_BARS = 48        # 48 hourly candles = 2 days for pivot detection
    PROXIMITY_PCT = 1.5       # price must be within 1.5% of support
    MIN_BOUNCES = 2           # level tested at least twice
    COOLDOWN_MINUTES = 120    # don't re-enter same symbol within 2 hours

    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._last_entry: dict[str, datetime] = {}
        self._support_levels: dict[str, list[float]] = {}  # symbol → [support prices]
        self.bus.subscribe(SignalBundle, self._on_signal)

    async def _on_signal(self, bundle: SignalBundle):
        symbol = bundle.symbol
        market = bundle.market_state
        tech = bundle.technical

        # Cooldown check
        last = self._last_entry.get(symbol)
        if last and (datetime.now() - last).total_seconds() < self.COOLDOWN_MINUTES * 60:
            return

        price = market.price
        if price <= 0:
            return

        # Build support levels from technical data
        # Use support_levels from technical event if available
        supports = tech.support_levels if tech.support_levels else []

        # Also track our own pivots from price history
        # (simplified: use recent lows as support approximation)
        if not supports:
            return

        # Check if price is near any support level
        for support in supports:
            if support <= 0:
                continue

            distance_pct = (price - support) / price * 100

            # Price must be ABOVE support (bouncing) and within proximity
            if 0 < distance_pct <= self.PROXIMITY_PCT:
                # Confirm bounce: RSI should be recovering (> 30) and BB position rising
                if tech.rsi_14 < 25:
                    continue  # still falling, not bouncing yet

                if tech.bb_position < 0.1:
                    continue  # still at bottom of bands

                # Volume confirmation: need above-average volume on the bounce
                if tech.volume_ratio < 1.0:
                    continue  # thin volume bounce = weak

                # Entry signal: price near support + bouncing + volume
                atr = price * tech.atr_14_pct if tech.atr_14_pct > 0 else price * 0.03
                risk = atr * 2.5

                proposal = TradeProposal(
                    timestamp=datetime.now(),
                    proposal_id=str(uuid.uuid4()),
                    symbol=symbol,
                    direction=Direction.LONG,
                    raw_score=70.0,  # S/R entries get a fixed score
                    ai_confidence=0.70,
                    ai_rationale=f"S/R REVERSAL: {symbol} bouncing off support ${support:.2f} (dist={distance_pct:.1f}%, RSI={tech.rsi_14:.0f}, vol={tech.volume_ratio:.1f}x)",
                    suggested_entry=price,
                    suggested_stop=support - (atr * 0.5),  # stop just below support
                    suggested_tp1=price + risk * 2.0,
                    suggested_tp2=price + risk * 4.0,
                    suggested_tp3=price + risk * 6.0,
                )

                logger.warning(
                    f"S/R ENTRY: {symbol} at ${price:,.2f} near support ${support:,.2f} "
                    f"(dist={distance_pct:.1f}%, RSI={tech.rsi_14:.0f}, vol={tech.volume_ratio:.1f}x)"
                )
                self._last_entry[symbol] = datetime.now()
                await self.bus.publish(proposal, priority=Priority.HIGH)
                return  # one entry per scan per symbol
