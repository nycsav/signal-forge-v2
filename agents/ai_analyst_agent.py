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


ANALYST_PROMPT = """You are a quantitative crypto trading analyst for Signal Forge v2.
Analyze this signal bundle and return a JSON trade decision.

SYMBOL: {symbol}
PRICE: ${price:,.2f}
MARKET REGIME: {regime}

TECHNICAL INDICATORS:
- RSI(14): {rsi} ({rsi_trend})
- MACD histogram: {macd_hist:+.4f}
- Bollinger position: {bb_pos:.2f} (0=lower, 1=upper){bb_squeeze}
- EMA alignment (9>21>55): {ema_aligned}
- Volume ratio: {vol_ratio:.1f}x
- Ichimoku: {ichimoku}
- Timeframe consensus: {tf_consensus}

SENTIMENT:
- Fear & Greed: {fear_greed}/100
- altFINS signal: {altfins_score:+.2f}

COMPOSITE SCORE (pre-AI): {pre_score}/100
Score breakdown: {score_breakdown}

RULES:
1. If composite < 40, lean toward SKIP
2. If F&G < 20 and technicals are oversold (RSI < 30), consider contrarian LONG
3. If F&G > 80 and RSI > 70, consider SHORT or SKIP
4. Entry should be near current price
5. Stop loss = entry - 1.5 * ATR (approximately {stop_distance:.2f} below entry)
6. TP1 = +1.5R, TP2 = +3R, TP3 = +5R from entry

Return ONLY this JSON (no markdown, no explanation outside JSON):
{{"direction": "long" or "short" or "flat", "score": 0-100, "ai_confidence": 0.0-1.0, "rationale": "2 sentences max", "entry_price": {price}, "stop_price": <number>, "tp1_price": <number>, "tp2_price": <number>, "tp3_price": <number>, "score_breakdown": {{}}, "key_risk": "1 sentence"}}"""


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

        # Build prompt
        stop_distance = market.price * tech.atr_14_pct * 1.5 if tech.atr_14_pct > 0 else market.price * 0.03
        prompt = ANALYST_PROMPT.format(
            symbol=symbol,
            price=market.price,
            regime=market.regime.value,
            rsi=tech.rsi_14,
            rsi_trend=tech.rsi_trend,
            macd_hist=tech.macd_histogram,
            bb_pos=tech.bb_position,
            bb_squeeze=" [SQUEEZE]" if tech.bb_squeeze else "",
            ema_aligned="YES" if tech.ema_alignment else "NO",
            vol_ratio=tech.volume_ratio,
            ichimoku=tech.ichimoku_signal,
            tf_consensus=json.dumps(tech.timeframe_consensus),
            fear_greed=sent.fear_greed if sent else market.fear_greed_index,
            altfins_score=market.altfins_signal_score,
            pre_score=pre_score,
            score_breakdown=json.dumps(breakdown),
            stop_distance=stop_distance,
        )

        # Use Llama 3.2 for fast initial scoring, DeepSeek R1 for high-conviction review
        response = await self._call_ollama(self.fast_model, prompt, timeout=45)
        if response is None:
            logger.warning(f"Llama timeout for {symbol}, trying DeepSeek R1")
            response = await self._call_ollama(self.primary_model, prompt, timeout=120)

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

        proposal = TradeProposal(
            timestamp=datetime.now(),
            proposal_id=str(uuid.uuid4()),
            symbol=symbol,
            direction=direction,
            raw_score=final_score,
            ai_confidence=parsed.get("ai_confidence", 0.5),
            ai_rationale=parsed.get("rationale", "")[:500],
            suggested_entry=entry,
            suggested_stop=forced_stop,
            suggested_tp1=forced_tp1,
            suggested_tp2=forced_tp2,
            suggested_tp3=forced_tp3,
            score_breakdown=final_breakdown,
        )

        logger.info(
            f"AI Analyst PROPOSAL: {symbol} {direction.value} "
            f"score={final_score:.0f} conf={proposal.ai_confidence:.0%} "
            f"— {proposal.ai_rationale[:80]}"
        )
        await self.bus.publish(proposal)

    async def _call_ollama(self, model: str, prompt: str, timeout: int = 90) -> str | None:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                r = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 600},
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
