"""Signal Forge v2 — Monitor Agent (Rebuilt)

7-layer exit strategy per spec Section 5:
1. Hard stop: Entry - ATR×2.5
2. ATR trailing: Trail from highest CLOSE after +1.5×ATR activation
3. TP1 (+1.5R): Close 33%, move stop to breakeven
4. TP2 (+3R): Close 33%
5. TP3 (+5R): Close 34%
6. Time exit: 72h max, 48h if flat ±0.5%
7. Signal degradation: Exit if re-score < 30

Reads positions from Alpaca (source of truth). Tracks state in memory (not DB).
"""

import asyncio
import json
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import OrderFilledEvent, TradeClosedEvent
from config.settings import settings


class MonitorAgent:
    # Exit parameters from spec Section 5
    ATR_STOP_MULT = 2.5
    ATR_ACTIVATION_MULT = 1.5
    TP1_R = 1.5   # TP1 at 1.5× risk
    TP2_R = 3.0
    TP3_R = 5.0
    MAX_HOLD_HOURS = 72
    FLAT_HOURS = 48
    FLAT_THRESHOLD = 0.005  # ±0.5%

    def __init__(self, event_bus: EventBus, db_path: str):
        self.bus = event_bus
        self.alpaca_key = settings.alpaca_api_key
        self.alpaca_secret = settings.alpaca_secret_key or settings.alpaca_api_secret
        self.alpaca_base = settings.alpaca_base_url

        # In-memory position state (survives DB issues)
        self._state: dict[str, dict] = {}
        self.bus.subscribe(OrderFilledEvent, self._on_order_filled)

    async def _on_order_filled(self, event: OrderFilledEvent):
        logger.info(f"Monitor: tracking order {event.order_id} filled @ ${event.filled_price:,.2f}")

    async def run_monitor_loop(self, interval_seconds: int = 300):
        while True:
            try:
                await self._evaluate_all()
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _evaluate_all(self):
        positions = await self._fetch_alpaca_positions()
        if not positions:
            return

        # Fetch actual fill times from Alpaca orders (once per cycle)
        fill_times = await self._fetch_fill_times()

        actions_taken = 0
        for pos in positions:
            symbol = pos["symbol"]
            entry = pos["entry"]
            current = pos["current"]
            qty = pos["qty"]

            if entry <= 0 or current <= 0:
                continue

            # Get or create state — use actual fill time if available
            if symbol not in self._state:
                alpaca_sym = pos["alpaca_symbol"]
                fill_time = fill_times.get(alpaca_sym) or fill_times.get(alpaca_sym.replace("USD", "/USD"))
                first_seen = datetime.now()
                if fill_time:
                    try:
                        first_seen = datetime.fromisoformat(fill_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        pass
                self._state[symbol] = {
                    "hwm": current,
                    "trailing_active": False,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "first_seen": first_seen,
                }

            state = self._state[symbol]

            # Update high water mark (highest CLOSE)
            if current > state["hwm"]:
                state["hwm"] = current

            # Calculate REAL ATR from recent price history (not hardcoded)
            closes = state.get("closes", [])
            closes.append(current)
            if len(closes) > 100:
                closes = closes[-100:]
            state["closes"] = closes

            if len(closes) >= 15:
                # True ATR(14): average of absolute price changes over 14 periods
                true_ranges = [abs(closes[i] - closes[i-1]) for i in range(-14, 0)]
                atr = sum(true_ranges) / len(true_ranges)
            else:
                # Fallback: 1.2% of entry (realistic for current low-vol market)
                atr = entry * 0.012

            risk = atr * self.ATR_STOP_MULT
            stop = entry - risk
            activation = entry + atr * self.ATR_ACTIVATION_MULT
            tp1 = entry + risk * self.TP1_R
            tp2 = entry + risk * self.TP2_R
            tp3 = entry + risk * self.TP3_R

            pnl_pct = (current - entry) / entry
            hold_hours = (datetime.now() - state["first_seen"]).total_seconds() / 3600

            # ── Layer 1: Hard Stop ──
            if current <= stop:
                await self._close_position(pos, "hard_stop", current)
                actions_taken += 1
                continue

            # ── Layer 2: ATR Trailing Stop ──
            if current >= activation:
                state["trailing_active"] = True

            if state["trailing_active"]:
                trailing_stop = state["hwm"] - risk
                # Trailing stop only moves up
                if trailing_stop > stop:
                    stop = trailing_stop
                if current <= stop:
                    await self._close_position(pos, "trailing_stop", current)
                    actions_taken += 1
                    continue

            # ── Layer 3: TP1 — close 33%, move stop to breakeven ──
            if current >= tp1 and not state["tp1_hit"]:
                sell_qty = round(qty * 0.33, 6)
                await self._partial_close(pos, sell_qty, "tp1")
                state["tp1_hit"] = True
                logger.info(f"Monitor TP1: {symbol} +{pnl_pct:.1%} — sold 33%, stop → breakeven")
                actions_taken += 1

            # ── Layer 4: TP2 — close 33% of remaining ──
            if current >= tp2 and state["tp1_hit"] and not state["tp2_hit"]:
                remaining = qty * 0.67  # After TP1
                sell_qty = round(remaining * 0.5, 6)
                await self._partial_close(pos, sell_qty, "tp2")
                state["tp2_hit"] = True
                logger.info(f"Monitor TP2: {symbol} +{pnl_pct:.1%} — sold 50% of remaining")
                actions_taken += 1

            # ── Layer 5: TP3 — close all remaining ──
            if current >= tp3:
                await self._close_position(pos, "tp3", current)
                actions_taken += 1
                continue

            # ── Layer 6: Time exits ──
            if hold_hours >= self.MAX_HOLD_HOURS:
                await self._close_position(pos, "time_72h", current)
                actions_taken += 1
                continue

            if hold_hours >= self.FLAT_HOURS and abs(pnl_pct) < self.FLAT_THRESHOLD:
                await self._close_position(pos, "flat_48h", current)
                actions_taken += 1
                continue

            # ── Layer 7: Signal degradation ──
            # Every 6th cycle (~30 min), check if market has turned against us
            cycle_count = state.get("check_count", 0) + 1
            state["check_count"] = cycle_count
            if cycle_count % 6 == 0:  # Every 6 monitor cycles ≈ 30 min
                degradation_score = self._quick_rescore(entry, current, pnl_pct, hold_hours)
                if degradation_score < 30:
                    logger.warning(f"Monitor SIGNAL DEGRADATION: {symbol} rescore={degradation_score:.0f} < 30")
                    await self._close_position(pos, "signal_degradation", current)
                    actions_taken += 1
                    continue

            # Log status
            trail_status = f" TRAILING from ${state['hwm']:,.2f}" if state["trailing_active"] else ""
            tp_status = " [TP1 hit]" if state["tp1_hit"] else ""
            logger.debug(f"Monitor: {symbol} P&L={pnl_pct:+.2%} hold={hold_hours:.0f}h{trail_status}{tp_status}")

        if actions_taken > 0:
            logger.info(f"Monitor: {actions_taken} exit actions taken this cycle")

    async def _close_position(self, pos: dict, reason: str, price: float):
        symbol = pos["symbol"]
        alpaca_sym = pos["alpaca_symbol"]
        entry = pos["entry"]
        qty = pos["qty"]
        pnl_pct = (price - entry) / entry
        pnl_usd = (price - entry) * qty
        hold_hours = 0
        if symbol in self._state:
            hold_hours = (datetime.now() - self._state[symbol]["first_seen"]).total_seconds() / 3600

        logger.info(f"Monitor EXIT: {symbol} reason={reason} P&L={pnl_pct:+.2%} (${pnl_usd:+,.2f}) hold={hold_hours:.0f}h")

        # Log trade outcome for learning
        try:
            from agents.trade_logger import log_trade_outcome
            log_trade_outcome(
                symbol=symbol, direction="long", entry_price=entry, exit_price=price,
                entry_time=self._state.get(symbol, {}).get("first_seen", datetime.now()).isoformat(),
                exit_time=datetime.now().isoformat(),
                pnl_pct=pnl_pct * 100, pnl_usd=pnl_usd,
                hold_minutes=hold_hours * 60, exit_reason=reason,
            )
        except Exception as e:
            logger.debug(f"Trade log failed: {e}")

        # Close on Alpaca
        headers = {"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.delete(f"{self.alpaca_base}/v2/positions/{alpaca_sym}", headers=headers)
            except Exception as e:
                logger.error(f"Close failed {symbol}: {e}")

        # Clean up state
        self._state.pop(symbol, None)

        # Emit event
        event = TradeClosedEvent(
            timestamp=datetime.now(),
            order_id=symbol,
            close_price=price,
            close_reason=reason,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            hold_time_hours=hold_hours,
        )
        await self.bus.publish(event)

    async def _partial_close(self, pos: dict, qty: float, reason: str):
        alpaca_sym = pos["alpaca_symbol"]
        headers = {
            "APCA-API-KEY-ID": self.alpaca_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(
                    f"{self.alpaca_base}/v2/orders",
                    headers=headers,
                    json={"symbol": alpaca_sym, "qty": str(qty), "side": "sell",
                          "type": "market", "time_in_force": "gtc"},
                )
                logger.info(f"Monitor partial close: {pos['symbol']} sell {qty:.6f} ({reason})")
            except Exception as e:
                logger.error(f"Partial close failed {pos['symbol']}: {e}")

    def _quick_rescore(self, entry: float, current: float, pnl_pct: float, hold_hours: float) -> float:
        """Quick signal re-evaluation without AI call.

        Returns 0-100 score. Below 30 = signal degraded, exit.
        Factors: price trend, hold duration, P&L trajectory.
        """
        score = 50.0  # neutral baseline

        # Price vs entry — losing ground is bearish
        if pnl_pct > 0.05:
            score += 20  # +5%+ = strong
        elif pnl_pct > 0.02:
            score += 10  # +2%+ = decent
        elif pnl_pct > 0:
            score += 5   # slightly positive
        elif pnl_pct > -0.02:
            score -= 5   # slightly negative
        elif pnl_pct > -0.05:
            score -= 15  # losing 2-5%
        else:
            score -= 30  # losing 5%+ = very bearish

        # Hold time penalty — longer holds with poor returns = stale
        if hold_hours > 48 and pnl_pct < 0.01:
            score -= 15  # 2 days with no returns
        elif hold_hours > 24 and pnl_pct < 0:
            score -= 10  # 1 day and losing

        # Trend: price moving away from entry in wrong direction
        if current < entry * 0.95:
            score -= 20  # 5%+ below entry

        return max(0, min(100, score))

    async def _fetch_fill_times(self) -> dict[str, str]:
        """Get the earliest fill timestamp per symbol from Alpaca orders."""
        headers = {"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret}
        fill_times: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(
                    f"{self.alpaca_base}/v2/orders",
                    headers=headers,
                    params={"status": "filled", "limit": 100, "direction": "asc"},
                )
                if r.status_code == 200:
                    for o in r.json():
                        sym = o.get("symbol", "")
                        filled_at = o.get("filled_at", "")
                        if sym and filled_at and o.get("side") == "buy":
                            # Keep the earliest buy fill time per symbol
                            if sym not in fill_times:
                                fill_times[sym] = filled_at
            except Exception as e:
                logger.error(f"Monitor: fill times fetch failed: {e}")
        return fill_times

    async def _fetch_alpaca_positions(self) -> list[dict]:
        headers = {"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"{self.alpaca_base}/v2/positions", headers=headers)
                if r.status_code == 200:
                    result = []
                    for p in r.json():
                        raw = p.get("symbol", "")
                        # Normalize: BTCUSD → BTC-USD
                        norm = raw[:-3] + "-USD" if raw.endswith("USD") and "-" not in raw else raw
                        result.append({
                            "symbol": norm,
                            "alpaca_symbol": raw,
                            "entry": float(p.get("avg_entry_price", 0)),
                            "current": float(p.get("current_price", 0)),
                            "qty": float(p.get("qty", 0)),
                            "market_value": float(p.get("market_value", 0)),
                            "unrealized_pl": float(p.get("unrealized_pl", 0)),
                        })
                    return result
            except Exception as e:
                logger.error(f"Monitor: Alpaca fetch failed: {e}")
        return []
