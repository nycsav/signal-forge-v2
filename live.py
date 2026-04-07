#!/usr/bin/env python3
"""Signal Forge v2 — Live Trading Engine

Runs alongside paper (main.py). Shares the same AI brain, data sources, and
learning framework. Uses its own database (live_trades.db) and strict rules.

Usage:
  PYTHONPATH=. python live.py              # Start live engine
  PYTHONPATH=. python live.py --dry-run    # Simulate without placing real orders

The paper engine (main.py) keeps running via launchd daemon.
Both feed into the Learning Agent.
"""

import asyncio
import signal as sig
import argparse
from datetime import datetime
from loguru import logger

from config.settings import settings
from config import live_rules as rules
from db.live_repository import LiveRepository
from data import coinbase_client, fear_greed_client
from data.alpaca_client import AlpacaClient
from agents.event_bus import EventBus
from agents.events import MarketStateEvent, TechnicalEvent, SignalBundle, Direction
from agents.technical_agent import TechnicalAgent
from agents.ai_analyst_agent import AIAnalystAgent
from agents.scoring import SignalScorer
from agents.fibonacci import multi_timeframe_fib
import httpx


class LiveEngine:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.repo = LiveRepository()
        self.bus = EventBus()
        self.scorer = SignalScorer()
        self.technical = TechnicalAgent(self.bus)
        self.alpaca = AlpacaClient(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_secret_key or settings.alpaca_api_secret,
            base_url=settings.alpaca_base_url,
        )
        self.balance = rules.STARTING_CAPITAL
        self.halted = False
        self.trades_today = 0

        logger.info(f"Live Engine initialized — {'DRY RUN' if dry_run else 'REAL MONEY'}")
        logger.info(f"Capital: ${rules.STARTING_CAPITAL} | Coins: {rules.WATCHLIST}")
        logger.info(f"Max position: {rules.MAX_POSITION_PCT*100}% | Max positions: {rules.MAX_OPEN_POSITIONS}")
        logger.info(f"Stop: {rules.STOP_LOSS_PCT*100}% | TP1: +{rules.TP1_PCT*100}% | TP2: +{rules.TP2_PCT*100}%")

    async def run(self):
        logger.info("Live Engine starting scan loop (15 min interval)...")
        self.repo.log("engine", "Live engine started" + (" (DRY RUN)" if self.dry_run else ""))

        # Warm up technical indicators
        await self.technical.warmup(rules.WATCHLIST)

        while True:
            try:
                await self._scan_cycle()
            except Exception as e:
                logger.error(f"Live scan error: {e}")
                self.repo.log("error", str(e))
            await asyncio.sleep(900)  # 15 min

    async def _scan_cycle(self):
        # Check halt
        halted, reason = self.repo.check_daily_halt(rules.DAILY_LOSS_LIMIT_USD)
        if halted:
            logger.warning(f"LIVE HALTED: {reason}")
            self.repo.log("halt", reason)
            self.halted = True
            return

        # Check trade count limit
        if self.trades_today >= rules.MAX_TRADES_PER_DAY:
            logger.info(f"Daily trade limit reached ({self.trades_today}/{rules.MAX_TRADES_PER_DAY})")
            return

        # Get account balance
        account = await self.alpaca.get_account()
        if account:
            self.balance = account.get("portfolio_value", self.balance)

        # Get positions
        positions = await self.alpaca.get_positions()
        open_count = len(positions)

        # Monitor existing positions for exits
        for pos in positions:
            await self._check_exits(pos)

        # Don't open new if at max
        if open_count >= rules.MAX_OPEN_POSITIONS:
            logger.info(f"At max positions ({open_count}/{rules.MAX_OPEN_POSITIONS})")
            return

        # Fetch F&G
        fg = await fear_greed_client.get_fear_greed()
        fg_val = fg.get("value", 50)

        # Scan watchlist
        prices = await coinbase_client.get_all_prices(rules.WATCHLIST)

        for symbol in rules.WATCHLIST:
            price = prices.get(symbol, 0)
            if price <= 0:
                continue

            # Skip if we already hold this coin
            if any(p["symbol"].replace("USD", "-USD") == symbol or p["symbol"] == symbol.replace("-", "") for p in positions):
                continue

            # Feed to technical agent
            self.technical._init_symbol(symbol)
            ind = self.technical._indicators.get(symbol, {})
            if ind.get("count", 0) < 30:
                continue

            # Score — in extreme fear, lower the bar further
            tech_score = self._quick_tech_score(ind, price)
            effective_threshold = rules.MIN_SIGNAL_SCORE
            if fg_val < rules.FEAR_AGGRESSIVE_THRESHOLD:
                effective_threshold = 50  # More aggressive in extreme fear
            logger.info(f"Live SCAN {symbol}: tech_score={tech_score:.0f} threshold={effective_threshold}")
            if tech_score < effective_threshold:
                continue

            # AI analysis (both models)
            ai_result = await self._get_ai_consensus(symbol, price, tech_score, fg_val)
            if not ai_result:
                continue

            direction = ai_result.get("direction", "flat")
            confidence = ai_result.get("confidence", 0)
            consensus = ai_result.get("consensus", False)
            rationale = ai_result.get("rationale", "")

            # Apply live filters
            if direction == "flat":
                continue
            if confidence < rules.MIN_AI_CONFIDENCE:
                logger.info(f"Live SKIP {symbol}: confidence {confidence:.0%} < {rules.MIN_AI_CONFIDENCE:.0%}")
                continue
            if rules.REQUIRE_CONSENSUS and not consensus:
                logger.info(f"Live SKIP {symbol}: no model consensus")
                continue

            # Fibonacci check
            if rules.REQUIRE_FIB_CONFLUENCE:
                closes = ind.get("closes", [])
                fib = multi_timeframe_fib(symbol, {"1h": closes}, price)
                if fib.confluence_count < 1 and fib.fib_score_adj < 3:
                    logger.info(f"Live SKIP {symbol}: no Fib confluence (score_adj={fib.fib_score_adj})")
                    continue

            # Calculate position
            size_usd = self.balance * rules.MAX_POSITION_PCT
            if size_usd < rules.MIN_ORDER_USD:
                logger.info(f"Live SKIP: position size ${size_usd:.2f} below minimum ${rules.MIN_ORDER_USD}")
                continue

            qty = size_usd / price
            stop = price * (1 - rules.STOP_LOSS_PCT)
            tp1 = price * (1 + rules.TP1_PCT)
            tp2 = price * (1 + rules.TP2_PCT)

            logger.info(
                f"LIVE {'(DRY RUN) ' if self.dry_run else ''}SIGNAL: {symbol} {direction} "
                f"score={tech_score:.0f} conf={confidence:.0%} consensus={consensus} "
                f"size=${size_usd:.2f} stop=${stop:.2f} tp1=${tp1:.2f}"
            )

            # Execute
            if not self.dry_run:
                order = await self._place_order(symbol, qty, "buy" if direction == "long" else "sell")
                if order:
                    trade_id = self.repo.open_trade(
                        symbol=symbol, side="buy" if direction == "long" else "sell",
                        entry_price=price, quantity=qty, size_usd=size_usd,
                        stop_price=stop, tp1_price=tp1, tp2_price=tp2,
                        signal_score=tech_score, ai_confidence=confidence,
                        consensus=1 if consensus else 0,
                    )
                    self.trades_today += 1
                    logger.info(f"LIVE ORDER FILLED: {symbol} qty={qty:.6f} @ ${price:,.2f} (trade_id={trade_id})")
            else:
                self.repo.open_trade(
                    trade_id=f"dry_{datetime.now().strftime('%H%M%S')}_{symbol}",
                    symbol=symbol, side="buy" if direction == "long" else "sell",
                    entry_price=price, quantity=qty, size_usd=size_usd,
                    stop_price=stop, tp1_price=tp1, tp2_price=tp2,
                    signal_score=tech_score, ai_confidence=confidence,
                    consensus=1 if consensus else 0,
                )
                self.trades_today += 1
                logger.info(f"DRY RUN: would buy {symbol} qty={qty:.6f} @ ${price:,.2f}")

    async def _check_exits(self, pos: dict):
        """Check exit conditions for an open position."""
        symbol = pos["symbol"]
        current = pos.get("current_price", 0)
        entry = pos.get("avg_entry", 0) or pos.get("avg_entry_price", 0)
        if not current or not entry:
            return

        pnl_pct = (current - entry) / entry

        # Find matching live trade
        open_trades = self.repo.get_open_trades()
        trade = None
        for t in open_trades:
            if t["symbol"] == symbol or t["symbol"] == symbol.replace("USD", "-USD"):
                trade = t
                break

        if not trade:
            return

        # Stop loss
        stop = trade.get("stop_price", entry * (1 - rules.STOP_LOSS_PCT))
        if current <= stop:
            logger.warning(f"LIVE EXIT {symbol}: STOP HIT at ${current:.4f} (stop=${stop:.4f})")
            if not self.dry_run:
                await self._close_position(symbol)
            self.repo.close_trade(trade["trade_id"], current, "stop_loss")
            return

        # TP1: close 50%
        tp1 = trade.get("tp1_price", entry * (1 + rules.TP1_PCT))
        if current >= tp1 and "tp1" not in (trade.get("exit_reason") or ""):
            logger.info(f"LIVE TP1 {symbol}: +{pnl_pct:.1%} — closing 50%")
            if not self.dry_run:
                sell_qty = float(pos.get("qty", 0)) * rules.TP1_SCALE
                await self._place_order(symbol, sell_qty, "sell")
            self.repo.log("tp1_hit", f"{symbol} +{pnl_pct:.1%}", trade["trade_id"])

        # TP2: close remaining
        tp2 = trade.get("tp2_price", entry * (1 + rules.TP2_PCT))
        if current >= tp2:
            logger.info(f"LIVE TP2 {symbol}: +{pnl_pct:.1%} — closing all")
            if not self.dry_run:
                await self._close_position(symbol)
            self.repo.close_trade(trade["trade_id"], current, "tp2")

    def _quick_tech_score(self, ind: dict, price: float) -> float:
        """Quick technical score from cached indicators."""
        rsi = None
        try:
            vals = ind.get("rsi_14", {})
            if hasattr(vals, 'output_values') and vals.output_values:
                rsi = float(vals.output_values[-1]) if vals.output_values[-1] is not None else None
        except Exception:
            pass

        score = 50
        if rsi:
            if rsi < 30: score += 15
            elif rsi < 40: score += 8
            elif rsi > 70: score -= 15
            elif rsi > 60: score -= 5

        # EMA trend
        try:
            ema9 = float(ind["ema_9"].output_values[-1]) if ind.get("ema_9") and ind["ema_9"].output_values[-1] else None
            ema21 = float(ind["ema_21"].output_values[-1]) if ind.get("ema_21") and ind["ema_21"].output_values[-1] else None
            if ema9 and ema21:
                if ema9 > ema21: score += 8
                else: score -= 5
        except Exception:
            pass

        return score

    async def _get_ai_consensus(self, symbol: str, price: float, score: float, fear_greed: int) -> dict | None:
        """Get dual-model AI consensus."""
        prompt = f"{symbol} ${price:,.0f} RSI=? F&G={fear_greed} Score={score:.0f}/100\n\nJSON only: {{\"direction\":\"long/short/flat\",\"score\":0-100,\"ai_confidence\":0.0-1.0,\"rationale\":\"one sentence\"}}"

        # Use Llama 3.2 3B for speed (3s, reliable JSON), then Qwen3 as second opinion
        models = ["llama3.2:3b", "qwen3:14b"]
        results = []
        for model in models:
            timeout_s = 15 if "llama" in model else 60
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    r = await client.post(f"{settings.ollama_host}/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False,
                              "options": {"temperature": 0.1, "num_predict": 2000 if "qwen" in model else 300}})
                    if r.status_code == 200:
                        resp = r.json().get("response", "")
                        if not resp or len(resp.strip()) < 5:
                            logger.debug(f"Live AI: {model} returned empty")
                            continue
                        import re, json as _json
                        matches = re.findall(r'\{[^{}]*\}', resp)
                        for m in matches:
                            try:
                                parsed = _json.loads(m)
                                if "direction" in parsed:
                                    results.append(parsed)
                                    logger.info(f"Live AI [{model}]: {parsed.get('direction')} conf={parsed.get('ai_confidence')} — {parsed.get('rationale','')[:60]}")
                                    break
                            except Exception:
                                pass
            except Exception as e:
                logger.debug(f"Live AI {model} failed: {e}")

        if not results:
            return None

        primary = results[0]
        consensus = False
        if len(results) >= 2:
            consensus = results[0].get("direction") == results[1].get("direction") and results[0].get("direction") != "flat"

        return {
            "direction": primary.get("direction", "flat"),
            "confidence": primary.get("ai_confidence", 0.5),
            "consensus": consensus,
            "rationale": primary.get("rationale", ""),
        }

    async def _place_order(self, symbol: str, qty: float, side: str) -> dict | None:
        alpaca_sym = symbol.replace("-", "/")
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_secret_key or settings.alpaca_api_secret,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.post(f"{settings.alpaca_base_url}/v2/orders", headers=headers,
                    json={"symbol": alpaca_sym, "qty": str(round(qty, 6)), "side": side,
                          "type": "market", "time_in_force": "gtc"})
                if r.status_code in (200, 201):
                    return r.json()
                logger.error(f"Live order failed: {r.status_code} {r.text[:100]}")
            except Exception as e:
                logger.error(f"Live order error: {e}")
        return None

    async def _close_position(self, symbol: str):
        alpaca_sym = symbol.replace("-USD", "").replace("-", "") + "USD"
        headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_secret_key or settings.alpaca_api_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.delete(f"{settings.alpaca_base_url}/v2/positions/{alpaca_sym}", headers=headers)
            except Exception as e:
                logger.error(f"Close position failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Signal Forge v2 — Live Trading")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without placing real orders")
    args = parser.parse_args()

    engine = LiveEngine(dry_run=args.dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("Live engine shutting down")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(sig.SIGINT, shutdown)
    loop.add_signal_handler(sig.SIGTERM, shutdown)

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        logger.info("Live engine stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
