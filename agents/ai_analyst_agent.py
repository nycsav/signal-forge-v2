"""Signal Forge v2 — AI Analyst Agent

Receives SignalBundle from orchestrator, sends structured prompt to DeepSeek R1 14B
via Ollama. Outputs TradeProposal with score 0-100, direction, and rationale.

Falls back to Llama 3.2 3B if DeepSeek times out.
"""

import json
import re
import uuid
from datetime import datetime
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import SignalBundle, TradeProposal, Direction
from agents.scoring import SignalScorer


ANALYST_PROMPT = """{symbol} ${price:,.0f} RSI={rsi:.0f} F&G={fear_greed} EMA={ema_aligned} MACD={macd_hist:+.3f} BB={bb_pos:.1f} Vol={vol_ratio:.1f}x Regime={regime} MarketChange={market_change:+.1f}% Score={pre_score:.0f}/100

If MarketChange>+2% and F&G<25, this is fear+green=strong buy signal. If regime=bull_trend, prefer long.

JSON only: {{"direction":"long/short/flat","score":0-100,"ai_confidence":0.0-1.0,"rationale":"one sentence"}}"""


class AIAnalystAgent:
    def __init__(self, event_bus: EventBus, config: dict, scorer: SignalScorer):
        self.bus = event_bus
        self.scorer = scorer
        self.ollama_host = config.get("ollama_host", "http://localhost:11434")
        self.primary_model = config.get("deepseek_model", "deepseek-r1:14b")
        self.fast_model = config.get("fast_model", "llama3.2:3b")
        self.bus.subscribe(SignalBundle, self._on_signal_bundle)

    async def _on_signal_bundle(self, bundle: SignalBundle):
        symbol = bundle.symbol
        market = bundle.market_state
        tech = bundle.technical
        sent = bundle.sentiment

        # Pre-compute scores
        tech_score = self.scorer.score_technical(tech)
        sent_score = self.scorer.score_sentiment(sent) if sent else 50
        onchain_score = self.scorer.score_onchain(bundle.on_chain) if bundle.on_chain else 50
        pre_score, breakdown = self.scorer.composite_score(tech_score, sent_score, onchain_score)

        # Build concise prompt with ALL data signals
        stop_distance = market.price * tech.atr_14_pct * 1.5 if tech.atr_14_pct > 0 else market.price * 0.03
        prompt = ANALYST_PROMPT.format(
            symbol=symbol,
            price=market.price,
            rsi=tech.rsi_14,
            fear_greed=sent.fear_greed if sent else market.fear_greed_index,
            ema_aligned="YES" if tech.ema_alignment else "NO",
            macd_hist=tech.macd_histogram,
            bb_pos=tech.bb_position,
            vol_ratio=tech.volume_ratio,
            regime=market.regime.value,
            market_change=market.price_change_24h_pct,
            pre_score=pre_score,
        )

        # SPEED-FIRST: Llama 3.2 3B primary (15s, reliable JSON)
        # Then Qwen3 14B as second opinion ONLY if score > 60 (worth the wait)
        response = await self._call_ollama(self.fast_model, prompt, timeout=20)

        consensus = False
        if response and len(response.strip()) > 10:
            # Parse Llama result first
            import re as _re
            p1 = _re.search(r'"direction"\s*:\s*"(\w+)"', response)
            llama_direction = p1.group(1) if p1 else "flat"

            # Only call Qwen3 for confirmation if Llama says trade (not flat) and score is decent
            if llama_direction != "flat" and pre_score >= 55:
                response_qwen = await self._call_ollama(self.primary_model, prompt, timeout=90)
                if response_qwen and len(response_qwen.strip()) > 10:
                    p2 = _re.search(r'"direction"\s*:\s*"(\w+)"', response_qwen)
                    if p2 and p2.group(1) == llama_direction:
                        consensus = True
                        logger.info(f"AI Analyst: {symbol} CONSENSUS — Llama + Qwen3 agree: {llama_direction}")
        else:
            # Llama failed — try Qwen3 directly
            response = await self._call_ollama(self.primary_model, prompt, timeout=90)

        if response is None:
            logger.error(f"AI Analyst: both models failed for {symbol}")
            return

        # Parse response
        parsed = self._parse_response(response, market.price, stop_distance)
        if not parsed:
            return

        # Blend AI score with pre-computed score
        ai_score = parsed.get("score", pre_score)
        final_score, final_breakdown = self.scorer.composite_score(
            tech_score, sent_score, onchain_score, ai_score
        )

        direction_str = parsed.get("direction", "flat")
        try:
            direction = Direction(direction_str)
        except ValueError:
            direction = Direction.FLAT

        # Use adaptive threshold if available (from regime engine), fallback to config
        adaptive_threshold = getattr(self, '_adaptive_threshold', None)
        threshold = adaptive_threshold or self.scorer.weights["thresholds"]["min_score_to_propose"]

        if direction == Direction.FLAT or final_score < threshold:
            logger.info(f"AI Analyst: {symbol} score={final_score:.0f} direction={direction_str} — below threshold ({threshold}), skipping")
            return

        # OVERRIDE AI stop/TP with ATR-based levels (spec Section 5.2)
        # AI suggestions are unreliable — enforce ATR×2.5 stop, 1.5R/3R/5R TPs
        entry = market.price
        atr = entry * tech.atr_14_pct if tech.atr_14_pct > 0 else entry * 0.03
        risk = atr * 2.5  # ATR×2.5 stop distance
        forced_stop = entry - risk
        forced_tp1 = entry + risk * 1.5   # +1.5R
        forced_tp2 = entry + risk * 3.0   # +3R
        forced_tp3 = entry + risk * 5.0   # +5R

        # Consensus boost: +10% confidence when both models agree
        ai_conf = parsed.get("ai_confidence", 0.5)
        if consensus:
            ai_conf = min(1.0, ai_conf + 0.1)

        proposal = TradeProposal(
            timestamp=datetime.now(),
            proposal_id=str(uuid.uuid4()),
            symbol=symbol,
            direction=direction,
            raw_score=final_score,
            ai_confidence=ai_conf,
            ai_rationale=parsed.get("rationale", "")[:500],
            suggested_entry=entry,
            suggested_stop=forced_stop,
            suggested_tp1=forced_tp1,
            suggested_tp2=forced_tp2,
            suggested_tp3=forced_tp3,
            score_breakdown=final_breakdown,
        )

        consensus_tag = " [CONSENSUS]" if consensus else ""
        logger.info(
            f"AI Analyst PROPOSAL: {symbol} {direction.value} "
            f"score={final_score:.0f} conf={proposal.ai_confidence:.0%}{consensus_tag} "
            f"— {proposal.ai_rationale[:80]}"
        )

        # Log to DB for dashboard tracking
        self.scorer.repo = getattr(self.scorer, 'repo', None)
        try:
            from db.repository import Repository
            from config.settings import settings
            repo = Repository(settings.database_path)
            repo.log_event("ai_analyst", "proposal", symbol, {
                "direction": direction.value,
                "score": round(final_score, 1),
                "ai_confidence": round(ai_conf, 2),
                "consensus": consensus,
                "qwen3_response": (response_primary or "")[:100] if response_primary else None,
                "deepseek_response": (response_secondary or "")[:100] if response_secondary else None,
                "rationale": proposal.ai_rationale[:200],
            })
        except Exception:
            pass

        await self.bus.publish(proposal)

    async def _call_ollama(self, model: str, prompt: str, timeout: int = 90) -> str | None:
        """Call Ollama generate endpoint. Qwen3 needs num_predict=2000 for thinking + output."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                r = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 2000},
                    },
                )
                if r.status_code == 200:
                    return r.json().get("response", "")
            except httpx.TimeoutException:
                logger.warning(f"Ollama timeout ({model}, {timeout}s)")
            except Exception as e:
                logger.error(f"Ollama error ({model}): {e}")
        return None

    def _parse_response(self, raw: str, price: float, stop_dist: float) -> dict | None:
        # Remove DeepSeek thinking tags
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        cleaned = re.sub(r"```json\s*", "", cleaned)
        cleaned = re.sub(r"```\s*", "", cleaned)

        # Find JSON
        matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned)
        for match in matches:
            try:
                parsed = json.loads(match)
                if "direction" in parsed or "score" in parsed:
                    # Ensure required fields
                    parsed.setdefault("direction", "flat")
                    parsed.setdefault("score", 50)
                    parsed.setdefault("ai_confidence", 0.5)
                    parsed.setdefault("rationale", "")
                    parsed.setdefault("entry_price", price)
                    parsed.setdefault("stop_price", price - stop_dist)
                    parsed.setdefault("tp1_price", price + stop_dist * 1.5)
                    parsed.setdefault("tp2_price", price + stop_dist * 3)
                    parsed.setdefault("tp3_price", price + stop_dist * 5)
                    return parsed
            except json.JSONDecodeError:
                continue

        logger.error(f"AI Analyst: failed to parse JSON from response: {cleaned[:200]}")
        return None
