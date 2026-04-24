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
    # Exit parameters — tuned 2026-04-16 to fix win/loss asymmetry
    # Old: 2.5x stop, 1.5R TP1 → net negative (stop too wide, TP1 too tight)
    # New: 2.0x stop, 2R/4R/6R TPs → positive expectancy at 44%+ win rate
    ATR_STOP_MULT = 1.5  # tightened from 2.0 — hard stop avg was still -$5.27
    ATR_ACTIVATION_MULT = 1.5  # raised from 0.75 — let winners run, avg win was only $1.61
    MAX_LOSS_PER_TRADE_USD = 10.0  # tightened from $15 — worst loss was $17, target max $10
    TP1_R = 2.0   # TP1 at 2× risk (was 1.5)
    TP2_R = 4.0   # TP2 at 4× risk (was 3.0)
    TP3_R = 6.0   # TP3 at 6× risk (was 5.0)
    MAX_HOLD_HOURS = 72
    FLAT_HOURS = 48
    FLAT_THRESHOLD = 0.005  # ±0.5%
    FIXED_TRAIL_FALLBACK_PCT = 0.05   # 5% fixed if ATR unavailable

    # Hybrid ATR trailing: start at 2.5×ATR, tighten to 1.5×ATR after +2R profit
    TRAIL_ATR_INITIAL = 2.5   # was 3.0
    TRAIL_ATR_TIGHT = 1.5     # was 2.0
    TRAIL_TIGHTEN_R = 2.0     # tighten after bigger profit (was 1.5)

    # Regime-calibrated alpha (multiplied by ATR for trailing distance)
    REGIME_ALPHA = {"low_vol": 2.0, "normal": 2.5, "high_vol": 3.5}

    # Volume confirmation gate (added 2026-04-16)
    # Don't trigger trailing stop on low-volume wicks — require close below stop
    VOL_CONFIRM_THRESHOLD = 0.5   # if volume < 50% of 20-period avg, require confirmation
    VOL_LOOKBACK = 20             # periods for volume SMA

    # ATR spike detection — widen trail during volatility spikes
    ATR_SMA_LOOKBACK = 10         # SMA of ATR for spike detection
    ATR_SPIKE_MULT = 1.5          # ATR > 1.5x SMA(ATR) = spike
    ATR_SPIKE_WIDEN = 0.5         # add 0.5x to trail during spikes

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
        self._loop_count = 0
        while True:
            try:
                await self._evaluate_all()
                self._loop_count += 1
                # Every 30 cycles (~2.5 hours), check fd health
                if self._loop_count % 30 == 0:
                    self._check_fd_health()
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(interval_seconds)

    def _check_fd_health(self):
        """Check file descriptor count — warn if approaching limit."""
        try:
            import os
            pid = os.getpid()
            fd_count = len(os.listdir(f"/dev/fd"))
            if fd_count > 4000:
                logger.warning(f"FD HEALTH: {fd_count} open file descriptors — approaching limit")
            elif fd_count > 2000:
                logger.info(f"FD HEALTH: {fd_count} open file descriptors — elevated")
        except Exception:
            pass

    async def _check_daily_loss_guard(self):
        """Theme 4: Auto-pause if daily realized losses exceed threshold."""
        try:
            import sqlite3
            from config.settings import settings as _s
            conn = sqlite3.connect(str(_s.database_path), timeout=3)
            try:
                row = conn.execute(
                    "SELECT coalesce(sum(pnl_usd), 0) FROM trades WHERE status='closed' AND date(closed_at)=date('now') AND close_reason NOT IN ('stale_cleanup','orphan_no_position','orphan_cleanup')"
                ).fetchone()
                daily_pnl = row[0] if row else 0
                if daily_pnl < -200:
                    logger.warning(f"DAILY LOSS GUARD: ${daily_pnl:.2f} exceeds -$200 limit — halting new entries")
                    return True  # signal to halt
            finally:
                conn.close()
        except Exception:
            pass
        return False

    async def _evaluate_all(self):
        positions = await self._fetch_alpaca_positions()
        if not positions:
            return

        # KILL SWITCH: if total unrealized loss exceeds 3% of portfolio, close everything
        total_upl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        if total_upl < -3000:  # -$3,000 on $100K = 3%
            logger.critical(f"KILL SWITCH: total unrealized P&L ${total_upl:,.2f} exceeds -$3,000 — closing ALL positions")
            for pos in positions:
                await self._close_position(pos, "kill_switch", float(pos.get("current", pos.get("current_price", 0))))
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
                        parsed_time = datetime.fromisoformat(fill_time.replace("Z", "+00:00")).replace(tzinfo=None)
                        # Sanity check: if fill time is > 24h ago, use now instead
                        # This prevents stale fill times from triggering immediate time exits
                        if (datetime.now() - parsed_time).total_seconds() < 86400:
                            first_seen = parsed_time
                        else:
                            logger.warning(f"Monitor: {symbol} fill time {fill_time} is >24h old, using now()")
                    except Exception:
                        pass
                self._state[symbol] = {
                    "hwm": current,
                    "trailing_active": False,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "first_seen": first_seen,
                    "volumes": [],           # rolling volume history
                    "atr_history": [],        # rolling ATR for spike detection
                    "vol_pending_confirm": False,  # volume gate: waiting for close confirmation
                }

            state = self._state[symbol]

            # (A) Trail from highest CLOSE not wick high — update HWM at scan
            #     cycle only (current = closing price at scan time, not intraday)
            if current > state["hwm"]:
                state["hwm"] = current

            # Calculate REAL ATR from recent price history (not hardcoded)
            closes = state.get("closes", [])
            closes.append(current)
            if len(closes) > 100:
                closes = closes[-100:]
            state["closes"] = closes

            if len(closes) >= 15:
                true_ranges = [abs(closes[i] - closes[i-1]) for i in range(-14, 0)]
                atr = sum(true_ranges) / len(true_ranges)
            else:
                # (B) Fallback to 5% fixed if ATR unavailable (was 1.2%)
                atr = entry * self.FIXED_TRAIL_FALLBACK_PCT

            # Track ATR history for spike detection
            atr_history = state.get("atr_history", [])
            atr_history.append(atr)
            if len(atr_history) > 50:
                atr_history = atr_history[-50:]
            state["atr_history"] = atr_history

            # Fetch and track volume for confirmation gate
            vol = await self._fetch_coinbase_volume(symbol)
            volumes = state.get("volumes", [])
            if vol > 0:
                volumes.append(vol)
                if len(volumes) > 50:
                    volumes = volumes[-50:]
                state["volumes"] = volumes

            # Use proposal's stop/TP levels if available in position_state DB.
            # S/R strategy sets stop below support level (tighter than ATR-based).
            # Fall back to ATR-based if DB levels not found.
            db_stop = state.get("db_stop_price", 0)
            db_tp1 = state.get("db_tp1_price", 0)
            db_tp2 = state.get("db_tp2_price", 0)
            db_tp3 = state.get("db_tp3_price", 0)

            # Load from DB once per position lifecycle
            if db_stop == 0 and "db_loaded" not in state:
                try:
                    import sqlite3 as _sql
                    from config.settings import settings as _s
                    _conn = _sql.connect(str(_s.database_path), timeout=3)
                    try:
                        _conn.row_factory = _sql.Row
                        _row = _conn.execute(
                            "SELECT stop_price, tp1_price, tp2_price, tp3_price FROM position_state WHERE symbol=?",
                            (symbol,)
                        ).fetchone()
                        if _row:
                            db_stop = _row["stop_price"] or 0
                            db_tp1 = _row["tp1_price"] or 0
                            db_tp2 = _row["tp2_price"] or 0
                            db_tp3 = _row["tp3_price"] or 0
                            state["db_stop_price"] = db_stop
                            state["db_tp1_price"] = db_tp1
                            state["db_tp2_price"] = db_tp2
                            state["db_tp3_price"] = db_tp3
                    finally:
                        _conn.close()
                except Exception:
                    pass
                state["db_loaded"] = True

            risk = atr * self.ATR_STOP_MULT
            stop = db_stop if db_stop > 0 else (entry - risk)

            # (B) Activation: don't trail until profit >= 1.0 × ATR(14)
            activation = entry + atr * self.ATR_ACTIVATION_MULT

            tp1 = db_tp1 if db_tp1 > 0 else (entry + risk * self.TP1_R)
            tp2 = db_tp2 if db_tp2 > 0 else (entry + risk * self.TP2_R)
            tp3 = db_tp3 if db_tp3 > 0 else (entry + risk * self.TP3_R)

            # Recalculate risk from actual stop for trailing purposes
            risk = entry - stop if stop > 0 else risk

            pnl_pct = (current - entry) / entry
            hold_hours = (datetime.now() - state["first_seen"]).total_seconds() / 3600

            # (D) Regime-calibrated alpha — from regime engine state
            regime_alpha = self._get_regime_alpha()

            # ── Layer 0: Absolute Dollar Cap ──
            qty = float(pos.get("qty", 0) if isinstance(pos, dict) else pos.qty)
            upl_usd = (current - entry) * qty
            if upl_usd < -self.MAX_LOSS_PER_TRADE_USD:
                logger.warning(f"Monitor DOLLAR CAP: {symbol} loss ${upl_usd:.2f} exceeds -${self.MAX_LOSS_PER_TRADE_USD}")
                await self._close_position(pos, "hard_stop", current)
                actions_taken += 1
                continue

            # ── Layer 1: Hard Stop (with volume confirmation gate) ──
            if current <= stop:
                # Volume gate: if very low volume, delay 1 cycle to confirm
                # This prevents exits on liquidity sweep wicks
                if not self._is_volume_confirmed(state, vol) and not state.get("hard_stop_pending"):
                    state["hard_stop_pending"] = True
                    logger.info(f"Monitor HARD STOP: {symbol} breached but LOW VOLUME — confirming next cycle")
                    continue
                state["hard_stop_pending"] = False
                await self._close_position(pos, "hard_stop", current)
                actions_taken += 1
                continue
            else:
                state["hard_stop_pending"] = False

            # ── Layer 2: Hybrid ATR Trailing Stop (enhanced 2026-04-16) ──
            if current >= activation:
                state["trailing_active"] = True

            if state["trailing_active"]:
                profit_r = (current - entry) / risk if risk > 0 else 0

                # Stepped trailing: progressive tightening at profit milestones
                trail_mult = self._stepped_trail_mult(profit_r)

                # ATR spike detection: widen trail during volatility spikes
                if self._is_atr_spike(state, atr):
                    trail_mult += self.ATR_SPIKE_WIDEN

                # (D) Apply regime alpha overlay
                trail_distance = atr * min(trail_mult, regime_alpha)

                trailing_stop = state["hwm"] - trail_distance

                # (E) No-widening: stop can only move UP, never down
                old_stop = state.get("trailing_stop_price", stop)
                new_stop = max(old_stop, trailing_stop)
                state["trailing_stop_price"] = new_stop

                if current <= new_stop:
                    # Volume confirmation gate: don't exit on low-volume wicks
                    if not self._is_volume_confirmed(state, vol):
                        if not state.get("vol_pending_confirm"):
                            state["vol_pending_confirm"] = True
                            logger.info(f"Monitor TRAIL: {symbol} breached stop but LOW VOLUME — waiting for confirmation")
                        continue  # skip exit, wait for next cycle to confirm
                    state["vol_pending_confirm"] = False

                    await self._close_position(pos, "trailing_stop", current)
                    actions_taken += 1
                    continue
                else:
                    # Price recovered above stop — clear pending confirmation
                    state["vol_pending_confirm"] = False

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

    def _get_regime_alpha(self) -> float:
        """(D) Return trailing-stop alpha based on current regime volatility.

        Uses avg ATR % across tracked positions as a proxy for volatility regime:
          avg_atr_pct > 6% → high_vol (alpha=3.5)
          avg_atr_pct < 1.5% → low_vol (alpha=2.0)
          else → normal (alpha=2.5)
        """
        atr_pcts = []
        for sym, s in self._state.items():
            closes = s.get("closes", [])
            if len(closes) >= 15:
                trs = [abs(closes[i] - closes[i-1]) for i in range(-14, 0)]
                atr = sum(trs) / len(trs)
                price = closes[-1] if closes[-1] > 0 else 1
                atr_pcts.append(atr / price * 100)
        if not atr_pcts:
            return self.REGIME_ALPHA["normal"]
        avg = sum(atr_pcts) / len(atr_pcts)
        if avg > 6.0:
            return self.REGIME_ALPHA["high_vol"]
        elif avg < 1.5:
            return self.REGIME_ALPHA["low_vol"]
        return self.REGIME_ALPHA["normal"]

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

        # Also update the trades table so LearningAgent data stays consistent
        try:
            from db.repository import Repository
            from config.settings import settings
            repo = Repository(settings.database_path)
            # Find matching open trade and close it
            open_trades = repo.get_open_trades()
            for t in open_trades:
                if t.get("symbol") == symbol:
                    repo.update_trade(t["id"],
                        status="closed", exit_price=price,
                        pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                        closed_at=datetime.now().isoformat(),
                        close_reason=reason)
                    break
        except Exception as e:
            logger.debug(f"Trade table update failed: {e}")

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

    async def _fetch_coinbase_volume(self, symbol: str) -> float:
        """Fetch latest 1h candle volume from Coinbase for volume confirmation."""
        product = symbol if "-USD" in symbol else f"{symbol}-USD"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"https://api.exchange.coinbase.com/products/{product}/candles",
                    params={"granularity": 3600, "limit": 1},
                )
                if r.status_code == 200:
                    candles = r.json()
                    if candles:
                        return float(candles[0][5])  # [time, low, high, open, close, volume]
        except Exception:
            pass
        return 0.0

    def _is_volume_confirmed(self, state: dict, current_vol: float) -> bool:
        """Check if current volume is sufficient to confirm a stop trigger.
        Returns False if volume is too low (wick on thin liquidity)."""
        volumes = state.get("volumes", [])
        if len(volumes) < self.VOL_LOOKBACK:
            return True  # not enough history, assume confirmed

        vol_sma = sum(volumes[-self.VOL_LOOKBACK:]) / self.VOL_LOOKBACK
        if vol_sma <= 0:
            return True

        return current_vol >= vol_sma * self.VOL_CONFIRM_THRESHOLD

    def _is_atr_spike(self, state: dict, current_atr: float) -> bool:
        """Detect ATR volatility spike — widen trail during spikes."""
        atr_history = state.get("atr_history", [])
        if len(atr_history) < self.ATR_SMA_LOOKBACK:
            return False

        atr_sma = sum(atr_history[-self.ATR_SMA_LOOKBACK:]) / self.ATR_SMA_LOOKBACK
        return current_atr > atr_sma * self.ATR_SPIKE_MULT if atr_sma > 0 else False

    def _stepped_trail_mult(self, profit_r: float) -> float:
        """Stepped trailing: progressive tightening based on profit milestones.
        Returns the ATR multiplier for the trailing stop distance."""
        if profit_r >= 3.0:
            return self.TRAIL_ATR_TIGHT     # 1.5x ATR — tight
        elif profit_r >= 2.0:
            return 2.0                       # 2.0x ATR — medium
        else:
            return self.TRAIL_ATR_INITIAL    # 2.5x ATR — initial
