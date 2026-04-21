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


# Step 1: Llama pre-filter — is there even a setup worth analyzing?
PRE_FILTER_PROMPT = """{symbol} ${price:,.0f} RSI={rsi:.0f} F&G={fear_greed} EMA={ema_aligned} MACD={macd_hist:+.3f} MarketChange={market_change:+.1f}%

Is there a tradeable setup? JSON: {{"setup_quality":"strong/weak/none","direction":"long/short/flat","reason":"5 words max"}}"""

# Step 2: Qwen3 full analysis — the authoritative decision
FULL_ANALYSIS_PROMPT = """{symbol} ${price:,.0f} RSI={rsi:.0f} F&G={fear_greed} EMA={ema_aligned} MACD={macd_hist:+.3f} BB={bb_pos:.1f} Vol={vol_ratio:.1f}x Regime={regime} MarketChange={market_change:+.1f}% Score={pre_score:.0f}/100

Rules: If MarketChange>+2% and F&G<25, fear+green=strong buy. If move already >3%, wait for pullback not chase. If regime=bull_trend, prefer long.

JSON: {{"direction":"long/short/flat","score":0-100,"ai_confidence":0.0-1.0,"rationale":"one sentence"}}"""

# Step 3: Llama sanity check — does the Qwen3 decision make sense?
SANITY_CHECK_PROMPT = """Qwen3 says {direction} {symbol} at ${price:,.0f} with confidence {confidence}%. RSI={rsi:.0f} F&G={fear_greed} MarketChange={market_change:+.1f}%.

Does this make sense? JSON: {{"agrees":true/false,"reason":"5 words max"}}"""


class AIAnalystAgent:
    # Quantitative threshold — no LLM needed above this score
    QUANT_ENTRY_THRESHOLD = 75
    BASE_COOLDOWN_MINUTES = 60
    MIN_COOLDOWN_MINUTES = 15    # fastest we'll trade in a hot streak
    MAX_COOLDOWN_MINUTES = 240   # slowest when losing

    def __init__(self, event_bus: EventBus, config: dict, scorer: SignalScorer, altfins=None):
        self.bus = event_bus
        self.scorer = scorer
        self.altfins = altfins
        self.ollama_host = config.get("ollama_host", "http://localhost:11434")
        self.primary_model = config.get("deepseek_model", "deepseek-r1:14b")
        self.fast_model = config.get("fast_model", "llama3.2:3b")
        self._last_entry: dict[str, datetime] = {}  # symbol → last entry time
        self._recent_results: list[float] = []       # last N trade P&Ls for adaptive cooldown
        self._current_cooldown = self.BASE_COOLDOWN_MINUTES
        self._symbol_pnl: dict[str, list[float]] = {}  # per-symbol P&L tracking
        self._blocked_symbols: set[str] = set()         # symbols with 3+ consecutive losses
        self.bus.subscribe(SignalBundle, self._on_signal_bundle)

    def record_trade_result(self, pnl_pct: float, symbol: str = ""):
        """Called after trade closes. Feeds both global and per-symbol adaptation."""
        self._recent_results.append(pnl_pct)
        if len(self._recent_results) > 10:
            self._recent_results = self._recent_results[-10:]

        # Per-symbol tracking
        if symbol:
            if symbol not in self._symbol_pnl:
                self._symbol_pnl[symbol] = []
            self._symbol_pnl[symbol].append(pnl_pct)
            if len(self._symbol_pnl[symbol]) > 10:
                self._symbol_pnl[symbol] = self._symbol_pnl[symbol][-10:]

            # Block symbol after 3 consecutive losses
            last_3 = self._symbol_pnl[symbol][-3:]
            if len(last_3) >= 3 and all(p <= 0 for p in last_3):
                if symbol not in self._blocked_symbols:
                    self._blocked_symbols.add(symbol)
                    logger.warning(f"SYMBOL BLOCKED: {symbol} — 3 consecutive losses, pausing 2 hours")

            # Unblock after a win
            if pnl_pct > 0 and symbol in self._blocked_symbols:
                self._blocked_symbols.discard(symbol)
                logger.info(f"SYMBOL UNBLOCKED: {symbol} — profitable trade, resuming")

        self._adapt_cooldown()

    def _adapt_cooldown(self):
        """RL-inspired adaptive policy: adjust cooldown AND entry threshold
        based on recent reward signal (trade P&L).

        Reward function: R = sum(recent P&L) weighted by recency.
        Policy: higher reward → lower cooldown + lower threshold (trade more)
                lower reward → higher cooldown + higher threshold (trade less)
        """
        if len(self._recent_results) < 3:
            return

        # Exponential recency weighting (most recent trades matter more)
        weights = [0.5 ** i for i in range(len(self._recent_results) - 1, -1, -1)]
        total_w = sum(weights)
        weighted_reward = sum(r * w for r, w in zip(self._recent_results, weights)) / total_w

        last_3 = self._recent_results[-3:]
        wins = sum(1 for p in last_3 if p > 0)
        losses = sum(1 for p in last_3 if p <= 0)

        old_cooldown = self._current_cooldown
        old_threshold = self.QUANT_ENTRY_THRESHOLD

        if losses == 3:
            # 3 losses → defensive: double cooldown, raise threshold
            self._current_cooldown = min(self.MAX_COOLDOWN_MINUTES, old_cooldown * 2)
            self.QUANT_ENTRY_THRESHOLD = min(90, old_threshold + 3)
            logger.warning(f"RL ADAPT: 3 losses → cooldown {old_cooldown}→{self._current_cooldown}min, threshold {old_threshold}→{self.QUANT_ENTRY_THRESHOLD}")
        elif wins == 3:
            # 3 wins → aggressive: halve cooldown, lower threshold
            self._current_cooldown = max(self.MIN_COOLDOWN_MINUTES, old_cooldown // 2)
            self.QUANT_ENTRY_THRESHOLD = max(65, old_threshold - 3)
            logger.warning(f"RL ADAPT: 3 wins → cooldown {old_cooldown}→{self._current_cooldown}min, threshold {old_threshold}→{self.QUANT_ENTRY_THRESHOLD}")
        elif weighted_reward < -0.3:
            # Losing trend → gradual tightening
            self._current_cooldown = min(self.MAX_COOLDOWN_MINUTES, int(old_cooldown * 1.5))
            self.QUANT_ENTRY_THRESHOLD = min(90, old_threshold + 1)
            logger.info(f"RL ADAPT: reward={weighted_reward:+.2f}% → cooldown {old_cooldown}→{self._current_cooldown}min, threshold {old_threshold}→{self.QUANT_ENTRY_THRESHOLD}")
        elif weighted_reward > 0.1:
            # Winning trend → gradual loosening
            self._current_cooldown = max(self.MIN_COOLDOWN_MINUTES, int(old_cooldown * 0.75))
            self.QUANT_ENTRY_THRESHOLD = max(65, old_threshold - 1)
            logger.info(f"RL ADAPT: reward={weighted_reward:+.2f}% → cooldown {old_cooldown}→{self._current_cooldown}min, threshold {old_threshold}→{self.QUANT_ENTRY_THRESHOLD}")

    async def _on_signal_bundle(self, bundle: SignalBundle):
        symbol = bundle.symbol

        # Block symbols with 3+ consecutive losses
        if symbol in self._blocked_symbols:
            return

        # Adaptive cooldown: slows down when losing, speeds up when winning
        last = self._last_entry.get(symbol)
        if last and (datetime.now() - last).total_seconds() < self._current_cooldown * 60:
            return  # too soon — cooldown active
        market = bundle.market_state
        tech = bundle.technical
        sent = bundle.sentiment

        # Pre-compute scores
        tech_score = self.scorer.score_technical(tech)
        sent_score = self.scorer.score_sentiment(sent) if sent else 50
        onchain_score = self.scorer.score_onchain(bundle.on_chain) if bundle.on_chain else 50
        pre_score, breakdown = self.scorer.composite_score(tech_score, sent_score, onchain_score)

        # Use the orchestrator's composite score if available (includes altFINS bonus)
        # The orchestrator logs the full score to signals_log before publishing the bundle.
        # We check signals_log for the most recent score for this symbol.
        orchestrator_score = pre_score
        try:
            import sqlite3
            from config.settings import settings as _s
            conn = sqlite3.connect(str(_s.database_path), timeout=3)
            try:
                row = conn.execute(
                    "SELECT raw_score FROM signals_log WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)
                ).fetchone()
                if row and row[0] and row[0] > pre_score:
                    orchestrator_score = row[0]
            finally:
                conn.close()
        except Exception:
            pass

        fg = sent.fear_greed if sent else market.fear_greed_index

        # ═══ QUANTITATIVE FAST PATH ═══
        # Score >= threshold: trade immediately, but ONLY if momentum confirms.
        # Day 2 lesson: high scores from stale altFINS bonus + falling price = losing trades.
        if orchestrator_score >= self.QUANT_ENTRY_THRESHOLD:

            # MOMENTUM FILTER 1: RSI must be > 35 and not deeply oversold/falling
            if tech.rsi_14 < 35:
                logger.debug(f"QUANT SKIP: {symbol} score={orchestrator_score:.0f} but RSI={tech.rsi_14:.0f} < 35")
                return

            # MOMENTUM FILTER 2: MACD histogram must not be deeply negative
            if tech.macd_histogram < -0.5 * market.price * 0.01:  # more than -0.5% of price
                logger.debug(f"QUANT SKIP: {symbol} score={orchestrator_score:.0f} but MACD={tech.macd_histogram:.4f} deeply negative")
                return

            # MOMENTUM FILTER 3: Price must be above lower Bollinger Band (not falling knife)
            if tech.bb_position < 0.15:
                logger.debug(f"QUANT SKIP: {symbol} score={orchestrator_score:.0f} but BB={tech.bb_position:.2f} — below lower band")
                return

            entry = market.price
            atr = entry * tech.atr_14_pct if tech.atr_14_pct > 0 else entry * 0.03
            risk = atr * 2.5
            confidence = min(0.95, orchestrator_score / 100)

            proposal = TradeProposal(
                timestamp=datetime.now(),
                proposal_id=str(uuid.uuid4()),
                symbol=symbol,
                direction=Direction.LONG,
                raw_score=orchestrator_score,
                ai_confidence=confidence,
                ai_rationale=f"QUANT ENTRY: score={orchestrator_score:.0f} (tech={tech_score:.0f} sent={sent_score:.0f}) F&G={fg}",
                suggested_entry=entry,
                suggested_stop=entry - risk,
                suggested_tp1=entry + risk * 2.0,
                suggested_tp2=entry + risk * 4.0,
                suggested_tp3=entry + risk * 6.0,
                score_breakdown=breakdown,
            )
            logger.warning(f"QUANT ENTRY: {symbol} score={orchestrator_score:.0f} conf={confidence:.0%} — bypassing LLM, sending to RiskAgent")
            self._last_entry[symbol] = datetime.now()
            from agents.event_bus import Priority
            await self.bus.publish(proposal, priority=Priority.HIGH)
            return

        # ═══ Below 75: use LLM pipeline as before ═══

        # Build prompt fields
        stop_distance = market.price * tech.atr_14_pct * 1.5 if tech.atr_14_pct > 0 else market.price * 0.03
        prompt_fields = dict(
            symbol=symbol, price=market.price, rsi=tech.rsi_14,
            fear_greed=fg, ema_aligned="YES" if tech.ema_alignment else "NO",
            macd_hist=tech.macd_histogram, bb_pos=tech.bb_position,
            vol_ratio=tech.volume_ratio, regime=market.regime.value,
            market_change=market.price_change_24h_pct, pre_score=pre_score,
        )

        # ═══ STEP 1: Llama 3.2 pre-filter — discard obvious non-setups ═══
        pre_filter_prompt = PRE_FILTER_PROMPT.format(**prompt_fields)
        pre_response = await self._call_ollama(self.fast_model, pre_filter_prompt, timeout=20)

        setup_quality = "none"
        pre_direction = "flat"
        if pre_response:
            import re as _re
            m = _re.search(r'"setup_quality"\s*:\s*"(\w+)"', pre_response)
            if m:
                setup_quality = m.group(1)
            m2 = _re.search(r'"direction"\s*:\s*"(\w+)"', pre_response)
            if m2:
                pre_direction = m2.group(1)

        if setup_quality == "none":
            logger.debug(f"AI Step1: {symbol} no setup (Llama pre-filter)")
            return

        logger.info(f"AI Step1: {symbol} setup={setup_quality} direction={pre_direction}")

        # ═══ STEP 2: Qwen3 14B full analysis — the authoritative decision ═══
        full_prompt = FULL_ANALYSIS_PROMPT.format(**prompt_fields)
        response = await self._call_ollama(self.primary_model, full_prompt, timeout=90)

        # If Qwen3 fails, use Llama's pre-filter response as fallback
        if not response or len(response.strip()) < 10:
            logger.info(f"AI Step2: Qwen3 empty for {symbol}, using Llama pre-filter")
            response = pre_response

        # ═══ STEP 3: Llama sanity check on Qwen3 output ═══
        consensus = False
        parsed_direction = "flat"
        parsed_confidence = 0

        if response:
            import re as _re
            m_dir = _re.search(r'"direction"\s*:\s*"(\w+)"', response)
            m_conf = _re.search(r'"ai_confidence"\s*:\s*([\d.]+)', response)
            if m_dir:
                parsed_direction = m_dir.group(1)
            if m_conf:
                parsed_confidence = float(m_conf.group(1))

            if parsed_direction != "flat" and parsed_confidence >= 0.5:
                sanity_prompt = SANITY_CHECK_PROMPT.format(
                    direction=parsed_direction, symbol=symbol, price=market.price,
                    confidence=int(parsed_confidence * 100), rsi=tech.rsi_14,
                    fear_greed=fg, market_change=market.price_change_24h_pct,
                )
                sanity_response = await self._call_ollama(self.fast_model, sanity_prompt, timeout=20)

                if sanity_response:
                    agrees_match = _re.search(r'"agrees"\s*:\s*(true|false)', sanity_response, _re.IGNORECASE)
                    if agrees_match and agrees_match.group(1).lower() == "true":
                        consensus = True
                        logger.info(f"AI Step3: {symbol} SANITY PASS — Llama confirms Qwen3's {parsed_direction}")
                    else:
                        logger.info(f"AI Step3: {symbol} SANITY FAIL — Llama disagrees with Qwen3")

        # Check staleness-based confidence cap
        max_conf = getattr(bundle, 'max_allowed_confidence', 1.0)
        if max_conf < 1.0:
            logger.info(f"AI: {symbol} confidence capped at {max_conf:.0%} (stale data)")

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

        # Consensus gate: require both models to agree (validated by data)
        if not consensus:
            logger.info(f"AI Analyst: {symbol} score={final_score:.0f} — NO CONSENSUS, skipping")
            return

        # ATR-based levels (tuned 2026-04-16, backtest validated: PF 1.43, Sharpe 2.84)
        entry = market.price
        atr = entry * tech.atr_14_pct if tech.atr_14_pct > 0 else entry * 0.03
        risk = atr * 2.0  # ATR×2.0 stop distance (was 2.5)
        forced_stop = entry - risk
        forced_tp1 = entry + risk * 2.0   # +2R (was 1.5R)
        forced_tp2 = entry + risk * 4.0   # +4R (was 3R)
        forced_tp3 = entry + risk * 6.0   # +6R (was 5R)

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
