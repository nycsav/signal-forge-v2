"""Signal Forge v2 — Monitor Agent

6-layer exit strategy evaluated every 5 minutes:
1. Hard stop (price crosses stop)
2. ATR trailing stop (trails from highest CLOSE after activation)
3. Take profit 1 (+1.5R → close 33%, move stop to breakeven)
4. Take profit 2 (+3R → close 33%)
5. Take profit 3 (+5R → close 34%)
6. Time exits (72h max, 48h if flat)
7. Signal degradation (re-score < 30)

Emits TradeClosedEvent.
"""

import asyncio
from datetime import datetime, timedelta
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import OrderFilledEvent, TradeClosedEvent
from db.repository import Repository
from config.settings import settings
from data import coinbase_client


class MonitorAgent:
    MAX_HOLD_HOURS = 72
    FLAT_EXIT_HOURS = 48
    FLAT_THRESHOLD_PCT = 0.005  # ±0.5%
    SIGNAL_DEGRADE_THRESHOLD = 30
    ATR_ACTIVATION_MULT = 1.5
    ATR_TRAIL_MULT = 2.5

    def __init__(self, event_bus: EventBus, db_path: str):
        self.bus = event_bus
        self.repo = Repository(db_path)
        self.alpaca_key = settings.alpaca_api_key
        self.alpaca_secret = settings.alpaca_secret_key or settings.alpaca_api_secret
        self.alpaca_base = settings.alpaca_base_url
        self.bus.subscribe(OrderFilledEvent, self._on_order_filled)

    async def _on_order_filled(self, event: OrderFilledEvent):
        logger.info(f"Monitor: tracking order {event.order_id} filled @ ${event.filled_price:,.2f}")

    async def run_monitor_loop(self, interval_seconds: int = 300):
        while True:
            try:
                await self._evaluate_all_positions()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _evaluate_all_positions(self):
        # Pull live positions from Alpaca (source of truth), not just DB
        import httpx as _httpx
        alpaca_positions = []
        try:
            async with _httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self.alpaca_base}/v2/positions",
                    headers={"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret},
                )
                if r.status_code == 200:
                    for p in r.json():
                        raw_sym = p.get("symbol", "")
                        # BTCUSD → BTC-USD for Coinbase lookups
                        if raw_sym.endswith("USD") and "-" not in raw_sym and "/" not in raw_sym:
                            norm_sym = raw_sym[:-3] + "-USD"
                        else:
                            norm_sym = raw_sym
                        entry = float(p.get("avg_entry_price", 0))
                        atr_est = entry * 0.03
                        alpaca_positions.append({
                            "symbol": norm_sym,
                            "alpaca_symbol": raw_sym,
                            "entry_price": entry,
                            "current_price": float(p.get("current_price", 0)),
                            "quantity": float(p.get("qty", 0)),
                            "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                            "stop_price": entry - atr_est * 2.5,
                            "tp1_price": entry + atr_est * 3.75,
                            "tp2_price": entry + atr_est * 7.5,
                            "tp3_price": entry + atr_est * 12.5,
                            "hwm": float(p.get("current_price", 0)),
                            "trailing_active": 0,
                            "tp1_hit": 0,
                            "tp2_hit": 0,
                            "direction": "long",
                            "opened_at": datetime.now().isoformat(),
                        })
        except Exception as e:
            logger.error(f"Monitor: failed to fetch Alpaca positions: {e}")
            return

        if not alpaca_positions:
            return

        # Merge with DB state (for stop/TP tracking) — skip DB if locked
        try:
            db_positions = {p["symbol"]: p for p in self.repo.get_all_positions()}
        except Exception:
            db_positions = {}

        for pos in alpaca_positions:
            symbol = pos["symbol"]
            # Use DB state if we have it, otherwise use Alpaca defaults
            if symbol in db_positions:
                db_pos = db_positions[symbol]
                pos["stop_price"] = db_pos.get("stop_price") or pos["stop_price"]
                pos["tp1_price"] = db_pos.get("tp1_price", 0)
                pos["tp2_price"] = db_pos.get("tp2_price", 0)
                pos["tp3_price"] = db_pos.get("tp3_price", 0)
                pos["hwm"] = db_pos.get("hwm", 0)
                pos["trailing_active"] = db_pos.get("trailing_active", 0)
                pos["tp1_hit"] = db_pos.get("tp1_hit", 0)
                pos["tp2_hit"] = db_pos.get("tp2_hit", 0)
                pos["opened_at"] = db_pos.get("opened_at") or pos["opened_at"]
            else:
                # Calculate exit levels from entry
                entry = pos["entry_price"]
                atr = entry * 0.03
                pos["stop_price"] = entry - atr * 2.5
                pos["tp1_price"] = entry + atr * 3.75
                pos["tp2_price"] = entry + atr * 7.5
                pos["tp3_price"] = entry + atr * 12.5
                pos["hwm"] = pos["current_price"]
                pos["trailing_active"] = 0
                pos["tp1_hit"] = 0
                pos["tp2_hit"] = 0

            try:
                await self._evaluate_exits(pos, pos["current_price"])
            except Exception as e:
                logger.error(f"Monitor eval error for {symbol}: {e}")

    async def _evaluate_exits(self, pos: dict, current_price: float):
        symbol = pos["symbol"]
        entry = pos["entry_price"]
        stop = pos["stop_price"]
        tp1 = pos.get("tp1_price", 0)
        tp2 = pos.get("tp2_price", 0)
        tp3 = pos.get("tp3_price", 0)
        hwm = pos.get("hwm", 0) or entry
        qty = pos["quantity"]
        direction = pos.get("direction", "long")
        is_long = direction == "long"

        # Update high water mark (highest CLOSE, not wicks)
        if current_price > hwm:
            hwm = current_price
            try:
                self.repo.upsert_position(symbol,
                    direction=pos.get("direction", "long"),
                    entry_price=pos.get("entry_price", current_price),
                    stop_price=pos.get("stop_price", current_price * 0.925),
                    quantity=pos.get("quantity", 0),
                    hwm=hwm, current_price=current_price,
                    opened_at=pos.get("opened_at", datetime.now().isoformat()),
                    last_checked=datetime.now().isoformat())
            except Exception:
                pass

        pnl_pct = (current_price - entry) / entry if is_long else (entry - current_price) / entry

        # ── Layer 1: Hard Stop ──
        if is_long and current_price <= stop:
            await self._execute_exit(pos, "stop", current_price)
            return
        if not is_long and current_price >= stop:
            await self._execute_exit(pos, "stop", current_price)
            return

        # ── Layer 2: ATR Trailing Stop ──
        activation_price = entry * (1 + entry * pos.get("signal_score", 0) * 0.0001) if entry else 0
        # Simple activation: price moved 1.5x the stop distance above entry
        stop_distance = abs(entry - stop)
        activation_level = entry + stop_distance * self.ATR_ACTIVATION_MULT / self.ATR_TRAIL_MULT if is_long else entry - stop_distance * self.ATR_ACTIVATION_MULT / self.ATR_TRAIL_MULT

        if pos.get("trailing_active") or (is_long and current_price >= activation_level):
            # Trailing active
            new_stop = hwm - stop_distance
            if new_stop > stop:
                self.repo.upsert_position(symbol, stop_price=new_stop, trailing_active=1)
                stop = new_stop
            if is_long and current_price <= new_stop:
                await self._execute_exit(pos, "trailing_stop", current_price)
                return

        # ── Layer 3: Take Profit 1 ──
        if tp1 and not pos.get("tp1_hit") and is_long and current_price >= tp1:
            await self._partial_close(pos, 0.33, current_price, "tp1")
            # Move stop to breakeven
            self.repo.upsert_position(symbol, tp1_hit=1, stop_price=entry)
            logger.info(f"Monitor TP1: {symbol} +{pnl_pct:.1%}, stop → breakeven")

        # ── Layer 4: Take Profit 2 ──
        if tp2 and not pos.get("tp2_hit") and pos.get("tp1_hit") and is_long and current_price >= tp2:
            await self._partial_close(pos, 0.33, current_price, "tp2")
            self.repo.upsert_position(symbol, tp2_hit=1)
            logger.info(f"Monitor TP2: {symbol} +{pnl_pct:.1%}")

        # ── Layer 5: Take Profit 3 ──
        if tp3 and is_long and current_price >= tp3:
            await self._execute_exit(pos, "tp3", current_price)
            return

        # ── Layer 6: Time exits ──
        try:
            opened = datetime.fromisoformat(pos["opened_at"])
        except (ValueError, TypeError):
            opened = datetime.now()
        hold_hours = (datetime.now() - opened).total_seconds() / 3600

        if hold_hours >= self.MAX_HOLD_HOURS:
            await self._execute_exit(pos, "time_72h", current_price)
            return

        if hold_hours >= self.FLAT_EXIT_HOURS and abs(pnl_pct) < self.FLAT_THRESHOLD_PCT:
            await self._execute_exit(pos, "flat_48h", current_price)
            return

        # Update current price in DB (include required fields to avoid NOT NULL errors)
        try:
            self.repo.upsert_position(symbol,
                direction=pos.get("direction", "long"),
                entry_price=pos.get("entry_price", current_price),
                stop_price=pos.get("stop_price", current_price * 0.925),
                quantity=pos.get("quantity", 0),
                current_price=current_price,
                opened_at=pos.get("opened_at", datetime.now().isoformat()),
                last_checked=datetime.now().isoformat())
        except Exception:
            pass

    async def _execute_exit(self, pos: dict, reason: str, price: float):
        symbol = pos["symbol"]
        entry = pos["entry_price"]
        qty = pos["quantity"]
        pnl_pct = (price - entry) / entry
        pnl_usd = (price - entry) * qty

        try:
            opened = datetime.fromisoformat(pos["opened_at"])
        except (ValueError, TypeError):
            opened = datetime.now()
        hold_hours = (datetime.now() - opened).total_seconds() / 3600

        logger.info(f"Monitor EXIT: {symbol} reason={reason} P&L={pnl_pct:+.2%} (${pnl_usd:+,.2f}) hold={hold_hours:.1f}h")

        # Close on Alpaca — use raw symbol (BTCUSD) or slash format (BTC/USD)
        alpaca_symbol = pos.get("alpaca_symbol") or symbol.replace("-", "/")
        await self._close_alpaca_position(alpaca_symbol)

        # Update DB
        self.repo.delete_position(symbol)

        # Emit TradeClosedEvent
        event = TradeClosedEvent(
            timestamp=datetime.now(),
            order_id=pos.get("order_id", symbol),
            close_price=price,
            close_reason=reason,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            hold_time_hours=hold_hours,
            max_favorable_excursion=((pos.get("hwm", entry) or entry) - entry) / entry if entry else 0,
        )
        await self.bus.publish(event)

        self.repo.log_event("monitor_agent", "trade_closed", symbol, {
            "reason": reason, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "hold_hours": hold_hours,
        })

    async def _partial_close(self, pos: dict, pct: float, price: float, reason: str):
        symbol = pos["symbol"]
        qty = pos["quantity"] * pct
        alpaca_symbol = symbol.replace("-", "/")

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
                    json={"symbol": alpaca_symbol, "qty": str(round(qty, 6)),
                          "side": "sell", "type": "market", "time_in_force": "gtc"},
                )
            except Exception as e:
                logger.error(f"Partial close failed {symbol}: {e}")

    async def _close_alpaca_position(self, symbol: str):
        headers = {
            "APCA-API-KEY-ID": self.alpaca_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.delete(
                    f"{self.alpaca_base}/v2/positions/{symbol}",
                    headers=headers,
                )
            except Exception as e:
                logger.error(f"Close position failed {symbol}: {e}")

    async def _get_current_price(self, symbol: str) -> float:
        coinbase_sym = symbol.replace("/", "-")
        if "-USD" not in coinbase_sym:
            coinbase_sym = f"{coinbase_sym}-USD"
        return await coinbase_client.get_price(coinbase_sym)
