"""Signal Forge v2 — Dual Account Tracker

Runs the same trading signals on both accounts simultaneously:
  - Paper ($100K): 2% per trade, tests signal quality at scale
  - Live ($300): 15% per trade, tests real-money viability

Same signal → two trades → compare results.
Proves whether the strategy scales up AND works on small capital.
"""

import asyncio
import json
import time
from datetime import datetime
from loguru import logger
import httpx

from config.settings import settings
from agents.risk_agent import RiskAgent
from db.live_repository import LiveRepository
from agents.trending_trader import TrendingTrader


class DualTracker:
    """Executes signals on both paper and live accounts, tracks performance separately."""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.live_repo = LiveRepository()
        self.trader = TrendingTrader()
        self.alpaca_key = settings.alpaca_api_key
        self.alpaca_secret = settings.alpaca_secret_key or settings.alpaca_api_secret
        self.alpaca_base = settings.alpaca_base_url

        # Track trades per account
        self._paper_trades: list[dict] = []
        self._live_trades: list[dict] = []

    async def run_cycle(self):
        """One full scan + trade cycle on both accounts."""
        # Get signals from trending trader
        signals = await self.trader.scan()

        if not signals:
            logger.info("DualTracker: no trending signals this cycle")
            return

        # Get account state
        account = await self._get_account()
        positions = await self._get_positions()
        paper_cash = account.get("cash", 0)
        position_count = len(positions)

        for signal in signals[:3]:  # Max 3 signals per cycle
            symbol = signal.get("alpaca_symbol", "")
            if not symbol:
                continue

            # Skip if already holding
            if any(p.get("symbol", "").replace("USD", "/USD") == symbol or
                   p.get("symbol", "") == symbol.replace("/", "") for p in positions):
                logger.info(f"DualTracker: already holding {symbol}, skip")
                continue

            score = signal.get("score", 0)
            strategy = signal.get("strategy", "unknown")
            price = signal.get("price", 0) or signal.get("suggested_entry", 0)

            if price <= 0:
                # Get live price
                price = await self._get_price(symbol)
                if price <= 0:
                    continue

            stop = signal.get("suggested_stop", price * 0.96)
            tp = signal.get("suggested_tp", price * 1.05)

            logger.info(
                f"DualTracker SIGNAL: {symbol} [{strategy}] score={score} "
                f"entry=${price:,.4f} stop=${stop:,.4f} tp=${tp:,.4f}"
            )

            # ── Paper trade ($100K account) ──
            paper_size = paper_cash * 0.02  # 2% per trade
            paper_qty = paper_size / price if price > 0 else 0

            if paper_qty > 0 and position_count < 10:
                if not self.dry_run:
                    order = await self._place_order(symbol, paper_qty, "buy")
                    if order:
                        self._paper_trades.append({
                            "symbol": symbol, "qty": paper_qty, "entry": price,
                            "size": paper_size, "stop": stop, "tp": tp,
                            "strategy": strategy, "score": score,
                            "account": "paper_100k", "time": datetime.now().isoformat(),
                        })
                        logger.info(f"  PAPER: bought {paper_qty:.6f} {symbol} @ ${price:,.4f} (${paper_size:,.0f})")
                else:
                    self._paper_trades.append({
                        "symbol": symbol, "qty": paper_qty, "entry": price,
                        "size": paper_size, "stop": stop, "tp": tp,
                        "strategy": strategy, "score": score,
                        "account": "paper_100k", "time": datetime.now().isoformat(),
                    })
                    logger.info(f"  PAPER (dry): would buy {paper_qty:.6f} {symbol} @ ${price:,.4f} (${paper_size:,.0f})")

            # ── Live trade ($300 account) ──
            live_size = 300 * RiskAgent.MAX_POSITION_PCT  # Uses RiskAgent sizing
            live_qty = live_size / price if price > 0 else 0

            # Apply conviction bonuses
            if score >= 70:
                live_size *= 1.15  # +15% for high conviction
                live_qty = live_size / price

            if live_qty > 0 and live_size >= 10.00:  # Min order $10
                self.live_repo.open_trade(
                    symbol=symbol, side="buy", entry_price=price,
                    quantity=live_qty, size_usd=live_size,
                    stop_price=stop, tp1_price=tp,
                    signal_score=score, ai_confidence=0,
                    consensus=0,
                )
                self._live_trades.append({
                    "symbol": symbol, "qty": live_qty, "entry": price,
                    "size": live_size, "stop": stop, "tp": tp,
                    "strategy": strategy, "score": score,
                    "account": "live_300", "time": datetime.now().isoformat(),
                })
                logger.info(f"  LIVE {'(dry)' if self.dry_run else ''}: {live_qty:.6f} {symbol} @ ${price:,.4f} (${live_size:,.0f})")

            position_count += 1

    async def run_forever(self, interval_seconds: int = 900):
        """Main loop — scan every 15 minutes."""
        logger.info(f"DualTracker starting ({'DRY RUN' if self.dry_run else 'LIVE'})")
        logger.info(f"Paper: $100K × 2% = $2,000/trade | Live: $300 × 15% = $45/trade")

        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"DualTracker cycle error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _get_account(self) -> dict:
        headers = {"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"{self.alpaca_base}/v2/account", headers=headers)
                if r.status_code == 200:
                    a = r.json()
                    return {"cash": float(a.get("cash", 0)), "portfolio_value": float(a.get("portfolio_value", 0))}
            except Exception:
                pass
        return {"cash": 0, "portfolio_value": 0}

    async def _get_positions(self) -> list:
        headers = {"APCA-API-KEY-ID": self.alpaca_key, "APCA-API-SECRET-KEY": self.alpaca_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"{self.alpaca_base}/v2/positions", headers=headers)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
        return []

    async def _get_price(self, symbol: str) -> float:
        coinbase_sym = symbol.replace("/", "-")
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"https://api.coinbase.com/api/v3/brokerage/market/products/{coinbase_sym}")
                if r.status_code == 200:
                    return float(r.json().get("price", 0))
            except Exception:
                pass
        return 0

    async def _place_order(self, symbol: str, qty: float, side: str) -> dict | None:
        headers = {
            "APCA-API-KEY-ID": self.alpaca_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.post(f"{self.alpaca_base}/v2/orders", headers=headers,
                    json={"symbol": symbol, "qty": str(round(qty, 6)),
                          "side": side, "type": "market", "time_in_force": "gtc"})
                if r.status_code in (200, 201):
                    return r.json()
            except Exception as e:
                logger.error(f"Order failed: {e}")
        return None

    def get_comparison(self) -> dict:
        """Compare performance between paper and live accounts."""
        return {
            "paper_trades": self._paper_trades,
            "live_trades": self._live_trades,
            "paper_count": len(self._paper_trades),
            "live_count": len(self._live_trades),
            "summary": {
                "paper_100k": {
                    "position_size": "2% ($2,000)",
                    "total_trades": len(self._paper_trades),
                    "symbols": list(set(t["symbol"] for t in self._paper_trades)),
                },
                "live_300": {
                    "position_size": "15% ($45)",
                    "total_trades": len(self._live_trades),
                    "symbols": list(set(t["symbol"] for t in self._live_trades)),
                },
            },
        }
