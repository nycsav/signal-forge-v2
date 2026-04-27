"""Signal Forge v2 — Risk Agent

VETO POWER: Can kill any trade at any time.
Checks: position limits, daily/weekly loss, correlation, Half-Kelly sizing,
signal threshold, market regime compatibility.
Emits RiskAssessmentEvent.
"""

import math
from datetime import datetime, timedelta
from loguru import logger
import httpx

from agents.event_bus import EventBus
from agents.events import TradeProposal, RiskAssessmentEvent, RiskDecision, Direction
from db.repository import Repository
from config.settings import settings

# Correlation groups — top 50 coins by sector
CORRELATED_GROUPS = {
    "blue_chip": ["BTC", "ETH", "BNB"],
    "layer1": ["SOL", "AVAX", "NEAR", "APT", "SUI", "ADA", "DOT", "ATOM", "SEI", "FTM", "ICP", "EOS", "ALGO"],
    "layer2": ["ARB", "OP", "MATIC", "IMX", "STX"],
    "defi": ["UNI", "LINK", "AAVE", "CRV", "MKR", "COMP", "SNX", "RUNE"],
    "meme": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
    "ai_depin": ["RENDER", "FET", "INJ", "GRT"],
    "storage": ["FIL"],
    "legacy": ["LTC", "XRP", "XLM", "TRX", "HBAR", "VET"],
    "metaverse": ["SAND", "MANA"],
    "modular": ["TIA"],
    "rwa": ["ONDO"],
}

SYMBOL_GROUP = {}
for grp, syms in CORRELATED_GROUPS.items():
    for s in syms:
        SYMBOL_GROUP[s] = grp


class RiskAgent:
    # Risk parameters (from spec Section 4)
    MAX_POSITION_PCT = 0.01          # 1% per trade — Quarter-Kelly (research: crypto needs lower sizing)
    HIGH_CONVICTION_PCT = 0.015      # 1.5% for score >= 85 (Quarter-Kelly high conviction)
    MAX_OPEN_POSITIONS = 5
    DAILY_LOSS_LIMIT = 0.05          # 5%
    WEEKLY_LOSS_LIMIT = 0.10         # 10%
    MIN_SIGNAL_SCORE = 62
    MIN_AI_CONFIDENCE = 0.62
    # Sub-$1K account sizing path:
    #   Accounts < $1,000 bypass Half-Kelly and use a flat 10% of equity,
    #   capped at 10% and floored at MIN_ORDER_USD so the order clears the
    #   exchange minimum even on very small accounts.
    SMALL_ACCOUNT_THRESHOLD = 1000.0   # equity below this triggers the small-account path
    MIN_ORDER_USD = 10.0               # Coinbase minimum order size (USD)
    SMALL_ACCOUNT_POSITION_PCT = 0.10  # 10% of equity for sub-$1K accounts
    # Absolute floors — RegimeEngine can lower instance MIN_SIGNAL_SCORE/MIN_AI_CONFIDENCE
    # for sizing logic, but the threshold checks below ALWAYS enforce these floors.
    MIN_SIGNAL_SCORE_FLOOR = 62
    MIN_AI_CONFIDENCE_FLOOR = 0.62
    MAX_SAME_GROUP = 3               # Max per sector (spec: 3)
    MIN_RISK_REWARD = 2.0

    # altFINS TA confirmation — disagreement halves position size
    ALTFINS_DISAGREE_SIZE_MULT = 0.50

    def __init__(self, event_bus: EventBus, db_path: str, portfolio_value: float):
        self.bus = event_bus
        self.repo = Repository(db_path)
        self.portfolio_value = portfolio_value
        self._alpaca_key = settings.alpaca_api_key
        self._alpaca_secret = settings.alpaca_secret_key or settings.alpaca_api_secret
        self._alpaca_base = settings.alpaca_base_url
        self._cached_position_count: int = 0
        # Set by orchestrator after init — ref to altFINS enrichment agent
        self.altfins = None  # Optional[AltFINSEnrichment]
        self._cached_positions: list = []
        self._cache_time: float = 0
        self.bus.subscribe(TradeProposal, self._on_proposal)

    async def _on_proposal(self, proposal: TradeProposal):
        """Run all risk checks + altFINS pre-execution gates. Any failure = VETO."""
        checks = [
            self._check_signal_threshold(proposal),
            self._check_ai_confidence(proposal),
            self._check_position_count(),
            self._check_daily_loss(),
            self._check_weekly_loss(),
            self._check_correlation(proposal),
            self._check_risk_reward(proposal),
            self._check_regime_compatibility(proposal),
        ]

        for passed, reason in checks:
            if not passed:
                await self._veto(proposal, reason)
                return

        # ── altFINS pre-execution: news check (veto on negative) ──
        news_veto = await self._check_altfins_news(proposal)
        if news_veto:
            await self._veto(proposal, news_veto)
            return

        # All checks passed — approve with sizing
        size_pct = self._calculate_position_size(proposal)

        # ── altFINS pre-execution: TA confirmation (halve size on disagree) ──
        size_pct = await self._apply_altfins_ta_adjustment(proposal, size_pct)

        size_usd = self.portfolio_value * size_pct

        event = RiskAssessmentEvent(
            timestamp=datetime.now(),
            proposal_id=proposal.proposal_id,
            decision=RiskDecision.APPROVED,
            approved_size_usd=size_usd,
            approved_size_pct_portfolio=size_pct,
            kelly_fraction=size_pct,
            risk_score=self._compute_risk_score(proposal),
            correlation_warning=False,
            open_positions_count=self._cached_position_count,
        )

        logger.info(
            f"Risk APPROVED: {proposal.symbol} {proposal.direction.value} "
            f"size=${size_usd:,.0f} ({size_pct:.1%})"
        )
        from agents.event_bus import Priority
        await self.bus.publish(event, priority=Priority.HIGH)

        # Log the approval
        self.repo.log_event("risk_agent", "approved", proposal.symbol, {
            "proposal_id": proposal.proposal_id,
            "size_usd": size_usd,
            "score": proposal.raw_score,
        })

    async def _veto(self, proposal: TradeProposal, reason: str):
        event = RiskAssessmentEvent(
            timestamp=datetime.now(),
            proposal_id=proposal.proposal_id,
            decision=RiskDecision.VETOED,
            veto_reason=reason,
            risk_score=1.0,
            open_positions_count=self._cached_position_count,
        )
        logger.warning(f"Risk VETOED: {proposal.symbol} — {reason}")
        await self.bus.publish(event)

        self.repo.log_event("risk_agent", "vetoed", proposal.symbol, {
            "proposal_id": proposal.proposal_id,
            "reason": reason,
            "score": proposal.raw_score,
        })

    # ── Risk Checks ──

    # (F) Raise signal threshold to 75 while Fear & Greed < 20
    CAPITULATION_SCORE_OVERRIDE = 75
    CAPITULATION_FG_THRESHOLD = 20

    def _check_signal_threshold(self, p: TradeProposal) -> tuple[bool, str]:
        threshold = max(self.MIN_SIGNAL_SCORE_FLOOR, self.MIN_SIGNAL_SCORE)
        # (F) Override: tighter threshold during capitulation (F&G < 20).
        #     Read F&G from the most recent signal_log entry.
        try:
            recent = self.repo.get_recent_events(1)
            fg = 50
            for e in recent:
                payload = e.get("payload", {})
                if isinstance(payload, str):
                    import json
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                fg = payload.get("fear_greed", 50)
                break
            if fg < self.CAPITULATION_FG_THRESHOLD:
                threshold = max(threshold, self.CAPITULATION_SCORE_OVERRIDE)
        except Exception:
            pass  # don't break the check if F&G lookup fails
        if p.raw_score < threshold:
            return False, f"Score {p.raw_score:.0f} < minimum {threshold} (floor={self.MIN_SIGNAL_SCORE_FLOOR})"
        return True, ""

    def _check_ai_confidence(self, p: TradeProposal) -> tuple[bool, str]:
        threshold = max(self.MIN_AI_CONFIDENCE_FLOOR, self.MIN_AI_CONFIDENCE)
        if p.ai_confidence < threshold:
            return False, f"AI confidence {p.ai_confidence:.2f} < minimum {threshold:.2f} (floor={self.MIN_AI_CONFIDENCE_FLOOR})"
        return True, ""

    def _check_position_count(self) -> tuple[bool, str]:
        count = self._get_alpaca_position_count()
        if count >= self.MAX_OPEN_POSITIONS:
            return False, f"Max positions reached ({count}/{self.MAX_OPEN_POSITIONS})"
        return True, ""

    def _get_alpaca_position_count(self) -> int:
        """Get live position count from Alpaca (cached 30s)."""
        import time
        now = time.time()
        if now - self._cache_time < 30:
            return self._cached_position_count
        try:
            r = httpx.get(
                f"{self._alpaca_base}/v2/positions",
                headers={"APCA-API-KEY-ID": self._alpaca_key, "APCA-API-SECRET-KEY": self._alpaca_secret},
                timeout=10,
            )
            if r.status_code == 200:
                positions = r.json()
                self._cached_position_count = len(positions)
                self._cached_positions = [{"symbol": p.get("symbol", "")} for p in positions]
                self._cache_time = now
        except Exception as e:
            logger.error(f"Risk: Alpaca position fetch failed: {e}")
        return self._cached_position_count

    def _check_daily_loss(self) -> tuple[bool, str]:
        since = (datetime.now() - timedelta(days=1)).isoformat()
        closed = self.repo.get_closed_trades_since(since)
        total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed)
        loss_pct = abs(total_pnl) / self.portfolio_value if total_pnl < 0 else 0
        if loss_pct >= self.DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit: {loss_pct:.1%} >= {self.DAILY_LOSS_LIMIT:.0%}"
        return True, ""

    def _check_weekly_loss(self) -> tuple[bool, str]:
        since = (datetime.now() - timedelta(days=7)).isoformat()
        closed = self.repo.get_closed_trades_since(since)
        total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed)
        loss_pct = abs(total_pnl) / self.portfolio_value if total_pnl < 0 else 0
        if loss_pct >= self.WEEKLY_LOSS_LIMIT:
            return False, f"Weekly loss limit: {loss_pct:.1%} >= {self.WEEKLY_LOSS_LIMIT:.0%}"
        return True, ""

    def _check_correlation(self, p: TradeProposal) -> tuple[bool, str]:
        base = p.symbol.replace("-USD", "").replace("/USD", "").upper()
        new_group = SYMBOL_GROUP.get(base, "unknown")

        # Use cached Alpaca positions (refreshed by _get_alpaca_position_count)
        self._get_alpaca_position_count()
        same_group = 0
        for t in self._cached_positions:
            t_base = t["symbol"].replace("-USD", "").replace("/USD", "").replace("USD", "").upper()
            if SYMBOL_GROUP.get(t_base, "?") == new_group:
                same_group += 1

        if same_group >= self.MAX_SAME_GROUP:
            return False, f"Correlation limit: {same_group} positions in {new_group} group"
        return True, ""

    def _check_risk_reward(self, p: TradeProposal) -> tuple[bool, str]:
        if p.suggested_entry <= 0 or p.suggested_stop <= 0:
            return True, ""
        risk = abs(p.suggested_entry - p.suggested_stop)
        if risk <= 0:
            return True, ""
        # Use weighted average reward across TP ladder (33% TP1, 33% TP2, 34% TP3)
        # Fallback TPs match tuned MonitorAgent values (2R/4R/6R, updated 2026-04-16)
        tp1_reward = abs(p.suggested_tp1 - p.suggested_entry) if p.suggested_tp1 else risk * 2.0
        tp2_reward = abs(p.suggested_tp2 - p.suggested_entry) if p.suggested_tp2 else risk * 4.0
        tp3_reward = abs(p.suggested_tp3 - p.suggested_entry) if p.suggested_tp3 else risk * 6.0
        weighted_reward = tp1_reward * 0.33 + tp2_reward * 0.33 + tp3_reward * 0.34
        rr = weighted_reward / risk
        if rr < self.MIN_RISK_REWARD:
            return False, f"Risk/reward {rr:.1f} < minimum {self.MIN_RISK_REWARD}"
        return True, ""

    def _check_regime_compatibility(self, p: TradeProposal) -> tuple[bool, str]:
        # Don't go long in confirmed bear trend with low scores
        if p.direction == Direction.LONG and p.raw_score < 60:
            # Check if most recent market state indicated bear regime
            events = self.repo.get_recent_events(10)
            for e in events:
                if e.get("event_type") == "market_state" and "bear" in str(e.get("payload", "")).lower():
                    return False, "Long in bear regime with score < 60"
        return True, ""

    # ── Sizing ──

    def _calculate_position_size(self, p: TradeProposal) -> float:
        """Half-Kelly with conviction scaling per spec Section 4.1."""
        # Win probability from score: p = 0.40 + (score/100 × 0.35)
        win_prob = 0.40 + (p.raw_score / 100) * 0.35
        q = 1 - win_prob

        # Reward/risk ratio from weighted TP ladder
        risk_dist = abs(p.suggested_entry - p.suggested_stop) if p.suggested_stop else p.suggested_entry * 0.03
        tp1_r = abs(p.suggested_tp1 - p.suggested_entry) if p.suggested_tp1 else risk_dist * 1.5
        tp2_r = abs(p.suggested_tp2 - p.suggested_entry) if p.suggested_tp2 else risk_dist * 3.0
        tp3_r = abs(p.suggested_tp3 - p.suggested_entry) if p.suggested_tp3 else risk_dist * 5.0
        reward_dist = tp1_r * 0.33 + tp2_r * 0.33 + tp3_r * 0.34
        b = reward_dist / risk_dist if risk_dist > 0 else 3.17

        # Full Kelly then half
        full_kelly = win_prob - (q / b) if b > 0 else 0
        half_kelly = max(0, full_kelly / 2)

        # Conviction scale: 55-70→0.50, 70-85→0.75, 85-100→1.00
        if p.raw_score >= 85:
            conviction = 1.00
        elif p.raw_score >= 70:
            conviction = 0.75
        else:
            conviction = 0.50

        # AI confidence modifier: 0.70 + (ai_confidence × 0.30)
        ai_mod = 0.70 + (p.ai_confidence * 0.30)

        final = half_kelly * conviction * ai_mod
        final_pct = min(self.MAX_POSITION_PCT, max(0.005, final))

        # Email Signal Agent: regime adjustment and fragility flag
        email_regime_mult = self.email_signal.get_regime_adjustment() if hasattr(self, 'email_signal') and self.email_signal else 1.0
        final_pct *= email_regime_mult

        fragility = self.email_signal.get_fragility_flag() if hasattr(self, 'email_signal') and self.email_signal else False
        if fragility:
            max_position = self.MAX_POSITION_PCT * 0.5
            final_pct = min(final_pct, max_position)

        # Sub-$1K account path: bypass Half-Kelly and size at a flat 10% of
        # equity. If 10% is below the exchange minimum (MIN_ORDER_USD), bump
        # the percentage up just enough to clear it. Larger accounts (>=$1K)
        # keep the original Half-Kelly behavior.
        if self.portfolio_value < self.SMALL_ACCOUNT_THRESHOLD and self.portfolio_value > 0:
            small_pct = self.SMALL_ACCOUNT_POSITION_PCT  # 10% by default
            min_order_pct = self.MIN_ORDER_USD / self.portfolio_value
            if small_pct < min_order_pct:
                # Account so small that 10% won't meet the exchange minimum;
                # raise the pct to exactly hit MIN_ORDER_USD.
                small_pct = min_order_pct
            sized_usd = self.portfolio_value * small_pct
            logger.info(
                f"Risk: sub-$1K sizing path fired for {p.symbol} "
                f"portfolio=${self.portfolio_value:.2f} kelly_pct={final_pct:.4f} "
                f"-> sized={small_pct:.4f} (${sized_usd:.2f})"
            )
            return small_pct

        return final_pct

    # ── altFINS Pre-Execution Checks ──

    async def _check_altfins_news(self, proposal: TradeProposal) -> str:
        """Check altFINS news for negative sentiment. Returns veto reason or ''."""
        if not self.altfins:
            return ""
        try:
            news = await self.altfins.check_news_sentiment(proposal.symbol, lookback_hours=4)
            if news.get("negative"):
                reason = (
                    f"altFINS news negative ({news['negative_count']}/{news['total_articles']} articles) "
                    f"— delaying entry: {'; '.join(news.get('headlines', [])[:2])}"
                )
                logger.warning(f"Risk: {proposal.symbol} {reason}")
                return reason
        except Exception as e:
            logger.debug(f"altFINS news check failed for {proposal.symbol}: {e}")
        return ""

    async def _apply_altfins_ta_adjustment(self, proposal: TradeProposal, size_pct: float) -> float:
        """Check altFINS TA direction. Halve size if they disagree with our direction."""
        if not self.altfins:
            return size_pct
        try:
            ta = await self.altfins.check_ta_confirmation(proposal.symbol)
            altfins_dir = ta.get("direction", "neutral")
            our_dir = proposal.direction.value  # "long" or "short"

            # Map our direction to comparable string
            agrees = (
                (our_dir == "long" and altfins_dir == "bullish") or
                (our_dir == "short" and altfins_dir == "bearish") or
                altfins_dir == "neutral"  # neutral = no opinion, don't penalize
            )

            if not agrees:
                adjusted = size_pct * self.ALTFINS_DISAGREE_SIZE_MULT
                logger.warning(
                    f"Risk: altFINS TA disagrees for {proposal.symbol} "
                    f"(ours={our_dir}, altfins={altfins_dir}) — "
                    f"reducing size {size_pct:.4f} → {adjusted:.4f} (50% cut)"
                )
                return adjusted
            elif altfins_dir != "neutral":
                logger.info(
                    f"Risk: altFINS TA confirms {proposal.symbol} "
                    f"(both={altfins_dir}) — size unchanged"
                )
        except Exception as e:
            logger.debug(f"altFINS TA check failed for {proposal.symbol}: {e}")
        return size_pct

    def _compute_risk_score(self, p: TradeProposal) -> float:
        """0 = safe, 1 = maximum risk."""
        risk = 0.0
        open_count = len(self.repo.get_open_trades())
        risk += open_count / self.MAX_OPEN_POSITIONS * 0.3
        risk += (100 - p.raw_score) / 100 * 0.3
        risk += (1 - p.ai_confidence) * 0.4
        return min(1.0, risk)
