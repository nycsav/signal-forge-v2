"""Signal Forge v2 — Grid Trading Strategy

Manual grid bot implementation for Alpaca paper trading.
No KuCoin needed — executes buy/sell limit orders on Alpaca.

Works best in ranging markets (F&G 30-60, low ATR).
Places buy orders at support zones and sell orders at resistance zones.

Each "grid level" is a price where we buy or sell. When price oscillates
between levels, each round-trip generates profit equal to the grid spacing.
"""

import uuid
from datetime import datetime
from loguru import logger

from agents.event_bus import EventBus, Priority
from agents.events import TradeProposal, Direction


class GridStrategy:
    """Grid trading for ranging markets — buy low, sell high, repeat."""

    GRID_LEVELS = 3           # reduced from 5 — fewer levels, faster cycling
    GRID_SPACING_PCT = 0.75   # tighter from 1.0 — more frequent fills in ranging market
    SIZE_PER_LEVEL = 500      # $500 per grid level
    COOLDOWN_MINUTES = 30     # reduced from 60 — grids should cycle fast

    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._active_grids: dict[str, dict] = {}  # symbol → grid config
        self._last_entry: dict[str, datetime] = {}
        self._filled_levels: dict[str, set] = {}  # symbol → set of filled price levels

    def setup_grid(self, symbol: str, center_price: float, atr_pct: float):
        """
        Calculate grid levels around the current price.

        Buy levels below current price, sell targets above.
        Grid spacing adapts to ATR — wider in high vol, tighter in low vol.
        """
        spacing = center_price * max(self.GRID_SPACING_PCT / 100, atr_pct * 0.5)

        buy_levels = []
        for i in range(1, self.GRID_LEVELS + 1):
            buy_price = center_price - (spacing * i)
            sell_target = buy_price + spacing  # sell one level up
            buy_levels.append({
                "buy_price": round(buy_price, 6),
                "sell_target": round(sell_target, 6),
                "level": i,
                "filled": False,
            })

        self._active_grids[symbol] = {
            "center": center_price,
            "spacing": spacing,
            "levels": buy_levels,
            "created_at": datetime.now(),
        }
        self._filled_levels.setdefault(symbol, set())

        logger.info(
            f"GRID SETUP: {symbol} center=${center_price:,.2f} spacing=${spacing:,.4f} "
            f"levels={self.GRID_LEVELS} (${self.SIZE_PER_LEVEL}/level)"
        )
        return buy_levels

    async def check_grid(self, symbol: str, current_price: float, atr_pct: float):
        """
        Check if price has reached any grid buy level.
        Called every scan cycle for symbols with active grids.
        """
        if symbol not in self._active_grids:
            # Auto-setup grid for ranging tokens
            if atr_pct < 0.04:  # only grid in low-vol environment
                self.setup_grid(symbol, current_price, atr_pct)
            return

        grid = self._active_grids[symbol]

        # Cooldown check
        last = self._last_entry.get(symbol)
        if last and (datetime.now() - last).total_seconds() < self.COOLDOWN_MINUTES * 60:
            return

        # Check each unfilled level
        for level in grid["levels"]:
            if level["filled"]:
                continue

            buy_price = level["buy_price"]

            # Price reached buy level (within 0.3% tolerance)
            if current_price <= buy_price * 1.003:
                risk = grid["spacing"]

                proposal = TradeProposal(
                    timestamp=datetime.now(),
                    proposal_id=str(uuid.uuid4()),
                    symbol=symbol,
                    direction=Direction.LONG,
                    raw_score=65.0,
                    ai_confidence=0.65,
                    ai_rationale=(
                        f"GRID BUY: level {level['level']} at ${buy_price:,.4f} "
                        f"(target ${level['sell_target']:,.4f}, spacing=${risk:,.4f})"
                    ),
                    suggested_entry=current_price,
                    suggested_stop=buy_price - risk * 2,  # stop 2 grid levels below
                    suggested_tp1=level["sell_target"],
                    suggested_tp2=level["sell_target"] + risk,
                    suggested_tp3=level["sell_target"] + risk * 2,
                )

                logger.warning(
                    f"GRID ENTRY: {symbol} level {level['level']} "
                    f"buy=${current_price:,.4f} target=${level['sell_target']:,.4f}"
                )
                level["filled"] = True
                self._filled_levels[symbol].add(level["level"])
                self._last_entry[symbol] = datetime.now()
                await self.bus.publish(proposal, priority=Priority.NORMAL)
                return  # one grid fill per cycle

    def reset_grid(self, symbol: str):
        """Reset grid after all levels filled or conditions change."""
        self._active_grids.pop(symbol, None)
        self._filled_levels.pop(symbol, None)
        logger.info(f"GRID RESET: {symbol}")

    @property
    def active_symbols(self) -> list[str]:
        return list(self._active_grids.keys())
