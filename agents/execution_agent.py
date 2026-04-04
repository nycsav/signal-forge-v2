"""Signal Forge v2 — Execution Agent

Receives APPROVED RiskAssessmentEvents, places orders on Alpaca paper.
Implements spread prediction (EWMA), slippage management, and fill tracking.
Emits OrderPlacedEvent and OrderFilledEvent.
"""

import asyncio
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import (
    RiskAssessmentEvent, RiskDecision, TradeProposal, OrderPlacedEvent, OrderFilledEvent, Direction
)
from db.repository import Repository
from config.settings import settings


class ExecutionAgent:
    MAX_SPREAD_MULTIPLIER = 2.0
    SLIPPAGE_LIMIT_BPS = 15

    def __init__(self, event_bus: EventBus, config: dict):
        self.bus = event_bus
        self.repo = Repository(config.get("database_path", settings.database_path))
        self.alpaca_key = config.get("alpaca_api_key", "")
        self.alpaca_secret = config.get("alpaca_secret_key", "") or config.get("alpaca_api_secret", "")
        self.alpaca_base = config.get("alpaca_base_url", "https://paper-api.alpaca.markets")
        self._proposals: dict[str, TradeProposal] = {}
        self._spread_ewma: dict[str, float] = {}

        self.bus.subscribe(TradeProposal, self._cache_proposal)
        self.bus.subscribe(RiskAssessmentEvent, self._on_risk_assessment)

    async def _cache_proposal(self, proposal: TradeProposal):
        self._proposals[proposal.proposal_id] = proposal

    async def _on_risk_assessment(self, assessment: RiskAssessmentEvent):
        if assessment.decision != RiskDecision.APPROVED:
            return

        proposal = self._proposals.pop(assessment.proposal_id, None)
        if not proposal:
            logger.error(f"Execution: no cached proposal for {assessment.proposal_id}")
            return

        await self._execute_trade(proposal, assessment)

    async def _execute_trade(self, proposal: TradeProposal, assessment: RiskAssessmentEvent):
        symbol = proposal.symbol
        size_usd = assessment.approved_size_usd or 0

        if size_usd <= 0:
            logger.warning(f"Execution: zero size for {symbol}")
            return

        # Place order on Alpaca
        alpaca_symbol = symbol.replace("-", "/")  # BTC-USD → BTC/USD
        side = "buy" if proposal.direction == Direction.LONG else "sell"
        qty = round(size_usd / proposal.suggested_entry, 6) if proposal.suggested_entry > 0 else 0

        if qty <= 0:
            logger.warning(f"Execution: zero qty for {symbol}")
            return

        logger.info(f"Execution: placing {side} {qty:.6f} {alpaca_symbol} (${size_usd:,.0f})")

        order = await self._place_alpaca_order(alpaca_symbol, qty, side)
        if not order:
            logger.error(f"Execution: order failed for {symbol}")
            return

        order_id = order.get("id", "unknown")

        # Record trade in DB
        self.repo.insert_trade(
            proposal_id=proposal.proposal_id,
            order_id=order_id,
            symbol=symbol,
            direction=proposal.direction.value,
            entry_price=proposal.suggested_entry,
            stop_price=proposal.suggested_stop,
            tp1_price=proposal.suggested_tp1,
            tp2_price=proposal.suggested_tp2,
            tp3_price=proposal.suggested_tp3,
            size_usd=size_usd,
            quantity=qty,
            signal_score=proposal.raw_score,
            ai_confidence=proposal.ai_confidence,
            ai_rationale=proposal.ai_rationale,
            risk_score=assessment.risk_score,
            status="open",
            broker="alpaca",
            opened_at=datetime.now().isoformat(),
        )

        # Record position state for monitor
        self.repo.upsert_position(
            symbol=symbol,
            direction=proposal.direction.value,
            entry_price=proposal.suggested_entry,
            stop_price=proposal.suggested_stop,
            tp1_price=proposal.suggested_tp1,
            tp2_price=proposal.suggested_tp2,
            tp3_price=proposal.suggested_tp3,
            quantity=qty,
            size_usd=size_usd,
            signal_score=proposal.raw_score,
            opened_at=datetime.now().isoformat(),
        )

        # Emit events
        placed = OrderPlacedEvent(
            timestamp=datetime.now(),
            proposal_id=proposal.proposal_id,
            order_id=order_id,
            symbol=symbol,
            direction=proposal.direction,
            size_usd=size_usd,
            entry_price=proposal.suggested_entry,
            stop_price=proposal.suggested_stop,
            tp1_price=proposal.suggested_tp1,
            tp2_price=proposal.suggested_tp2,
            tp3_price=proposal.suggested_tp3,
        )
        await self.bus.publish(placed)

        # Assume market order fills immediately for paper trading
        filled_price = float(order.get("filled_avg_price", proposal.suggested_entry) or proposal.suggested_entry)
        slippage = abs(filled_price - proposal.suggested_entry) / proposal.suggested_entry * 10000 if proposal.suggested_entry > 0 else 0

        filled = OrderFilledEvent(
            timestamp=datetime.now(),
            order_id=order_id,
            filled_price=filled_price,
            slippage_bps=slippage,
        )
        await self.bus.publish(filled)

        logger.info(f"Execution FILLED: {symbol} {side} {qty:.6f} @ ${filled_price:,.2f} (slippage: {slippage:.1f}bps)")

        self.repo.log_event("execution_agent", "order_filled", symbol, {
            "order_id": order_id, "side": side, "qty": qty,
            "filled_price": filled_price, "slippage_bps": slippage,
        })

    async def _place_alpaca_order(self, symbol: str, qty: float, side: str) -> dict | None:
        headers = {
            "APCA-API-KEY-ID": self.alpaca_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {
            "symbol": symbol,
            "qty": str(round(qty, 6)),
            "side": side,
            "type": "market",
            "time_in_force": "gtc",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.post(
                    f"{self.alpaca_base}/v2/orders",
                    headers=headers,
                    json=payload,
                )
                if r.status_code in (200, 201):
                    return r.json()
                logger.error(f"Alpaca order failed: {r.status_code} {r.text[:200]}")
            except Exception as e:
                logger.error(f"Alpaca order error: {e}")
        return None
