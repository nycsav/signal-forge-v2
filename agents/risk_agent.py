"""Signal Forge v2 — Risk Agent

VETO POWER: Can kill any trade at any time.
Checks: position limits, daily/weekly loss, correlation, Half-Kelly sizing,
signal threshold, market regime compatibility.
Emits RiskAssessmentEvent.
"""

import math
from datetime import datetime, timedelta
from loguru import logger

from agents.event_bus import EventBus
from agents.events import TradeProposal, RiskAssessmentEvent, RiskDecision, Direction
from db.repository import Repository

# Correlation groups
CORRELATED_GROUPS = {
    "blue_chip": ["BTC", "ETH"],
    "layer1": ["SOL", "AVAX", "NEAR", "APT", "SUI", "ADA", "DOT", "ATOM"],
    "layer2": ["ARB", "OP"],
    "defi": ["UNI", "LINK"],
    "meme": ["DOGE"],
    "storage": ["FIL"],
    "legacy": ["LTC", "XRP"],
    "ai_oracle": ["INJ"],
}

SYMBOL_GROUP = {}
for grp, syms in CORRELATED_GROUPS.items():
    for s in syms:
        SYMBOL_GROUP[s] = grp


class RiskAgent:
    # Risk parameters (from spec Section 4)
    MAX_POSITION_PCT = 0.02          # 2% per trade
    HIGH_CONVICTION_PCT = 0.025      # 2.5% for score >= 75
    MAX_OPEN_POSITIONS = 5
    DAILY_LOSS_LIMIT = 0.05          # 5%
    WEEKLY_LOSS_LIMIT = 0.10         # 10%
    MIN_SIGNAL_SCORE = 55
    MAX_SAME_GROUP = 2
    MIN_RISK_REWARD = 2.0

    def __init__(self, event_bus: EventBus, db_path: str, portfolio_value: float):
        self.bus = event_bus
        self.repo = Repository(db_path)
        self.portfolio_value = portfolio_value
        self.bus.subscribe(TradeProposal, self._on_proposal)

    async def _on_proposal(self, proposal: TradeProposal):
        """Run all 7 risk checks. Any failure = VETO."""
        checks = [
            self._check_signal_threshold(proposal),
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

        # All checks passed — approve with sizing
        size_pct = self._calculate_position_size(proposal)
        size_usd = self.portfolio_value * size_pct
        kelly = self._half_kelly(proposal.raw_score)

        event = RiskAssessmentEvent(
            timestamp=datetime.now(),
            proposal_id=proposal.proposal_id,
            decision=RiskDecision.APPROVED,
            approved_size_usd=size_usd,
            approved_size_pct_portfolio=size_pct,
            kelly_fraction=kelly,
            risk_score=self._compute_risk_score(proposal),
            correlation_warning=False,
            open_positions_count=len(self.repo.get_open_trades()),
        )

        logger.info(
            f"Risk APPROVED: {proposal.symbol} {proposal.direction.value} "
            f"size=${size_usd:,.0f} ({size_pct:.1%})"
        )
        await self.bus.publish(event)

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
            open_positions_count=len(self.repo.get_open_trades()),
        )
        logger.warning(f"Risk VETOED: {proposal.symbol} — {reason}")
        await self.bus.publish(event)

        self.repo.log_event("risk_agent", "vetoed", proposal.symbol, {
            "proposal_id": proposal.proposal_id,
            "reason": reason,
            "score": proposal.raw_score,
        })

    # ── Risk Checks ──

    def _check_signal_threshold(self, p: TradeProposal) -> tuple[bool, str]:
        if p.raw_score < self.MIN_SIGNAL_SCORE:
            return False, f"Score {p.raw_score:.0f} < minimum {self.MIN_SIGNAL_SCORE}"
        return True, ""

    def _check_position_count(self) -> tuple[bool, str]:
        open_trades = self.repo.get_open_trades()
        if len(open_trades) >= self.MAX_OPEN_POSITIONS:
            return False, f"Max positions reached ({len(open_trades)}/{self.MAX_OPEN_POSITIONS})"
        return True, ""

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

        open_trades = self.repo.get_open_trades()
        same_group = 0
        for t in open_trades:
            t_base = t["symbol"].replace("-USD", "").replace("/USD", "").replace("USD", "").upper()
            if SYMBOL_GROUP.get(t_base, "?") == new_group:
                same_group += 1

        if same_group >= self.MAX_SAME_GROUP:
            return False, f"Correlation limit: {same_group} positions in {new_group} group"
        return True, ""

    def _check_risk_reward(self, p: TradeProposal) -> tuple[bool, str]:
        if p.suggested_entry <= 0 or p.suggested_stop <= 0:
            return True, ""  # Can't check without valid prices
        risk = abs(p.suggested_entry - p.suggested_stop)
        reward = abs(p.suggested_tp1 - p.suggested_entry) if p.suggested_tp1 else risk * 2
        if risk <= 0:
            return True, ""
        rr = reward / risk
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
        base_pct = self.HIGH_CONVICTION_PCT if p.raw_score >= 75 else self.MAX_POSITION_PCT
        kelly = self._half_kelly(p.raw_score)
        score_mult = 0.5 + (p.raw_score / 100) * 0.5
        return min(base_pct, kelly * score_mult)

    def _half_kelly(self, score: float, win_rate: float = 0.55, avg_win: float = 0.06, avg_loss: float = 0.03) -> float:
        b = avg_win / avg_loss
        full_kelly = (win_rate * b - (1 - win_rate)) / b
        return max(0.005, full_kelly / 2)

    def _compute_risk_score(self, p: TradeProposal) -> float:
        """0 = safe, 1 = maximum risk."""
        risk = 0.0
        open_count = len(self.repo.get_open_trades())
        risk += open_count / self.MAX_OPEN_POSITIONS * 0.3
        risk += (100 - p.raw_score) / 100 * 0.3
        risk += (1 - p.ai_confidence) * 0.4
        return min(1.0, risk)
