"""Signal Forge v2 — Slack Trading Notifier

Posts trade proposals, smart money alerts, and whale triggers to Slack.
Supports approve/reject via emoji reactions (👍/👎).

Setup:
  1. Create Slack app at https://api.slack.com/apps
  2. Add bot scopes: chat:write, channels:read, reactions:read
  3. Install to workspace, copy Bot User OAuth Token
  4. Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in .env

Notification tiers:
  CRITICAL  → DM to user (trade proposals needing approval)
  HIGH      → #trading-signals (whale/smart money alerts, batched)
  NORMAL    → #trading-signals (morning briefings, regime changes)
  LOW       → suppressed unless error
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from agents.event_bus import EventBus, Priority
from agents.events import (
    TradeProposal,
    RiskAssessmentEvent,
    SmartMoneyEvent,
    EmailSignalEvent,
)
from config.settings import settings


# ── Constants ─────────────────────────────────────────────────

SLACK_API_BASE = "https://slack.com/api"
REQUEST_TIMEOUT = 10
MAX_PROPOSALS_PER_HOUR = 5
PROPOSAL_EXPIRY_SECONDS = 1800  # 30 min auto-veto
HIGH_PRIORITY_BATCH_SECONDS = 300  # 5 min batch window for HIGH alerts


class SlackNotifier:
    """Posts trading signals to Slack and manages approval flow."""

    def __init__(self, event_bus: EventBus, config: dict | None = None):
        self.bus = event_bus
        config = config or {}
        self.bot_token = config.get("slack_bot_token", "")
        self.channel_id = config.get("slack_channel_id", "")
        self.dm_user_id = config.get("slack_dm_user_id", "")
        self.enabled = bool(self.bot_token and self.channel_id)

        # Rate limiting
        self._proposal_timestamps: list[float] = []
        self._pending_proposals: dict[str, dict] = {}  # proposal_id -> {ts, message_ts, ...}

        # Batching for HIGH priority alerts
        self._high_priority_queue: list[dict] = []
        self._last_batch_flush: float = 0

        if not self.enabled:
            logger.warning(
                "SlackNotifier: disabled (set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in .env)"
            )

    # ── Slack API ─────────────────────────────────────────────

    async def _post(self, method: str, payload: dict) -> dict | None:
        """Make an authenticated Slack API call."""
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{SLACK_API_BASE}/{method}",
                    headers=headers,
                    json=payload,
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.error(f"SlackNotifier: {method} failed: {data.get('error', 'unknown')}")
                    return None
                return data
        except Exception as e:
            logger.error(f"SlackNotifier: API error on {method}: {e}")
            return None

    async def send_message(self, channel: str, text: str, blocks: list | None = None) -> str | None:
        """Send a message to a Slack channel. Returns message timestamp."""
        payload = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        result = await self._post("chat.postMessage", payload)
        if result:
            return result.get("ts")
        return None

    async def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None):
        """Update an existing Slack message."""
        payload = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        await self._post("chat.update", payload)

    # ── Message Formatters ────────────────────────────────────

    def _format_trade_proposal(self, proposal: TradeProposal) -> tuple[str, list]:
        """Format a trade proposal with full P&L numbers and exit strategy."""
        direction_label = "LONG" if proposal.direction.value == "long" else "SHORT"
        confidence_bar = "█" * int(proposal.ai_confidence * 10) + "░" * (10 - int(proposal.ai_confidence * 10))

        entry = proposal.suggested_entry
        stop = proposal.suggested_stop
        tp1 = proposal.suggested_tp1
        tp2 = proposal.suggested_tp2
        tp3 = proposal.suggested_tp3

        # Risk/reward and P&L calculations
        risk_per_unit = abs(entry - stop) if stop else 0
        reward_tp1 = abs(tp1 - entry) if tp1 else 0
        reward_tp2 = abs(tp2 - entry) if tp2 else 0
        reward_tp3 = abs(tp3 - entry) if tp3 else 0
        rr_ratio = f"{reward_tp1 / risk_per_unit:.1f}:1" if risk_per_unit > 0 else "N/A"

        # Percentage P&L at each level
        risk_pct = (risk_per_unit / entry * 100) if entry > 0 else 0
        profit_tp1_pct = (reward_tp1 / entry * 100) if entry > 0 else 0
        profit_tp2_pct = (reward_tp2 / entry * 100) if entry > 0 else 0
        profit_tp3_pct = (reward_tp3 / entry * 100) if entry > 0 else 0

        # Score breakdown summary
        breakdown_lines = []
        for k, v in sorted(proposal.score_breakdown.items(), key=lambda x: -abs(x[1])):
            if v != 0:
                breakdown_lines.append(f"{k}: {v:+.1f}")
        score_summary = " | ".join(breakdown_lines[:6]) if breakdown_lines else "N/A"

        text = (
            f"*TRADE PROPOSAL: {proposal.symbol} {direction_label}*\n"
            f"Score: {proposal.raw_score:.1f} | R:R {rr_ratio} | Max loss: {risk_pct:.1f}%\n"
            f"Reply APPROVE or REJECT"
        )

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"TRADE PROPOSAL — {proposal.symbol} {direction_label}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction_label}"},
                    {"type": "mrkdwn", "text": f"*Signal Score:*\n{proposal.raw_score:.1f}"},
                    {"type": "mrkdwn", "text": f"*AI Confidence:*\n{proposal.ai_confidence:.0%} [{confidence_bar}]"},
                    {"type": "mrkdwn", "text": f"*Risk/Reward:*\n{rr_ratio}"},
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Entry and Targets*"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Entry:*\n${entry:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Stop Loss:*\n${stop:,.2f} (-{risk_pct:.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Target 1:*\n${tp1:,.2f} (+{profit_tp1_pct:.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Target 2:*\n${tp2:,.2f} (+{profit_tp2_pct:.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Target 3:*\n${tp3:,.2f} (+{profit_tp3_pct:.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Max Loss per Unit:*\n${risk_per_unit:,.2f}"},
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": (
                    "*Exit Strategy (Automated)*\n"
                    f"• Stop loss at ${stop:,.2f} (-{risk_pct:.1f}%) — hard stop, executes immediately\n"
                    f"• TP1 at ${tp1:,.2f} (+{profit_tp1_pct:.1f}%) — close 40% of position\n"
                    f"• TP2 at ${tp2:,.2f} (+{profit_tp2_pct:.1f}%) — close 30%, move stop to breakeven\n"
                    f"• TP3 at ${tp3:,.2f} (+{profit_tp3_pct:.1f}%) — close remaining 30%\n"
                    f"• Trailing stop activates after TP1 hit (2.5x ATR)"
                )}
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Rationale:*\n>{proposal.ai_rationale[:400]}"}
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Score breakdown: {score_summary}"}
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Reply to this message with APPROVE or REJECT*\nAuto-expires in 30 minutes if no response."},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Proposal: `{proposal.proposal_id}`"}
                ]
            },
        ]

        return text, blocks

    def _format_risk_decision(self, event: RiskAssessmentEvent) -> str:
        """Format a risk assessment decision."""
        if event.decision.value == "approved":
            status = "APPROVED"
            size_info = f" | Size: ${event.approved_size_usd:,.0f} ({event.approved_size_pct_portfolio:.1%} of portfolio)" if event.approved_size_usd else ""
        elif event.decision.value == "vetoed":
            status = "REJECTED"
            size_info = f" | Reason: {event.veto_reason}" if event.veto_reason else ""
        else:
            status = "MODIFIED"
            size_info = ""

        return (
            f"*RISK DECISION: {status}* — `{event.proposal_id}`{size_info}\n"
            f"Risk score: {event.risk_score:.1f} | Open positions: {event.open_positions_count}"
        )

    def _format_smart_money(self, event: SmartMoneyEvent) -> str:
        """Format a smart money alert."""
        direction = event.direction.upper()
        symbols = ", ".join(event.symbols)
        return (
            f"*SMART MONEY [{event.signal_type.upper()}]* — {symbols} ({direction})\n"
            f"Chain: {event.chain} | Confidence: {event.confidence:.0%} | "
            f"Price: ${event.price_usd:,.4f} ({event.price_change_24h:+.1f}%)\n"
            f">{event.reason}"
        )

    def _format_whale_signal(self, signal: dict) -> str:
        """Format a whale trigger alert."""
        direction = signal.get("direction", "neutral").upper()
        strength = signal.get("strength", 0)
        return (
            f"*WHALE TRIGGER* [{direction}] strength={strength}/5\n"
            f">{signal.get('reason', 'Unknown')}"
        )

    # ── Event Handlers ────────────────────────────────────────

    async def on_trade_proposal(self, proposal: TradeProposal):
        """Post trade proposal to Slack DM for approval."""
        if not self.enabled:
            return

        # Rate limit: max 5 proposals per hour
        now = time.time()
        self._proposal_timestamps = [t for t in self._proposal_timestamps if now - t < 3600]
        if len(self._proposal_timestamps) >= MAX_PROPOSALS_PER_HOUR:
            logger.warning(f"SlackNotifier: rate limited, skipping proposal {proposal.proposal_id}")
            return
        self._proposal_timestamps.append(now)

        text, blocks = self._format_trade_proposal(proposal)
        target = self.dm_user_id or self.channel_id
        msg_ts = await self.send_message(target, text, blocks)

        if msg_ts:
            self._pending_proposals[proposal.proposal_id] = {
                "ts": now,
                "message_ts": msg_ts,
                "channel": target,
                "symbol": proposal.symbol,
                "direction": proposal.direction.value,
            }
            logger.info(f"SlackNotifier: proposal {proposal.proposal_id} posted to Slack")

    async def on_risk_decision(self, event: RiskAssessmentEvent):
        """Post risk decision and update the original proposal message."""
        if not self.enabled:
            return

        text = self._format_risk_decision(event)
        target = self.dm_user_id or self.channel_id

        # Update original proposal message if we have it
        pending = self._pending_proposals.pop(event.proposal_id, None)
        if pending:
            status = "✅ APPROVED" if event.decision.value == "approved" else "❌ VETOED"
            await self.update_message(
                pending["channel"],
                pending["message_ts"],
                f"[{status}] {pending['symbol']} {pending['direction'].upper()} — {text}",
            )
        else:
            await self.send_message(target, text)

    async def on_smart_money(self, event: SmartMoneyEvent):
        """Queue smart money alert for batched delivery."""
        if not self.enabled:
            return

        # Only post high-confidence signals
        if event.confidence < 0.55:
            return

        self._high_priority_queue.append({
            "type": "smart_money",
            "text": self._format_smart_money(event),
            "ts": time.time(),
        })
        await self._maybe_flush_batch()

    async def on_whale_signal(self, signal: dict):
        """Queue whale alert for batched delivery."""
        if not self.enabled:
            return

        strength = signal.get("strength", 0)
        if strength < 3:
            return

        self._high_priority_queue.append({
            "type": "whale",
            "text": self._format_whale_signal(signal),
            "ts": time.time(),
        })
        await self._maybe_flush_batch()

    async def _maybe_flush_batch(self):
        """Flush batched HIGH priority alerts every 5 minutes."""
        now = time.time()
        if now - self._last_batch_flush < HIGH_PRIORITY_BATCH_SECONDS:
            return
        if not self._high_priority_queue:
            return

        self._last_batch_flush = now
        alerts = self._high_priority_queue[:]
        self._high_priority_queue.clear()

        # Group and send
        lines = [a["text"] for a in alerts[:10]]  # Cap at 10 per batch
        header = f"*{len(lines)} Signal Alert{'s' if len(lines) > 1 else ''}* — {datetime.now().strftime('%H:%M ET')}"
        message = header + "\n\n" + "\n\n".join(lines)

        await self.send_message(self.channel_id, message)
        logger.info(f"SlackNotifier: flushed {len(lines)} batched alerts")

    # ── Morning Briefing ──────────────────────────────────────

    async def post_morning_briefing(self, briefing: dict):
        """Post the morning briefing summary."""
        if not self.enabled:
            return

        bias = briefing.get("bias", "NEUTRAL")
        score = briefing.get("score", 0)
        sections = [f"*MORNING BRIEFING* — {datetime.now().strftime('%b %d, %Y')}"]
        sections.append(f"*Market Bias:* {bias} (score: {score:+d})")

        if briefing.get("key_levels"):
            levels = "\n".join(f"  {k}: ${v:,.2f}" for k, v in briefing["key_levels"].items())
            sections.append(f"*Key Levels:*\n```{levels}```")

        if briefing.get("watchlist"):
            picks = "\n".join(
                f"  {p.get('ticker', '?')} — {p.get('direction', '?')} ({p.get('confidence', 0)}/100)"
                for p in briefing["watchlist"][:5]
            )
            sections.append(f"*Options Watchlist:*\n```{picks}```")

        await self.send_message(self.channel_id, "\n\n".join(sections))

    # ── Proposal Expiry Loop ──────────────────────────────────

    async def run_expiry_loop(self):
        """Auto-veto proposals that haven't been approved within 30 minutes."""
        if not self.enabled:
            return

        logger.info("SlackNotifier: expiry loop started (30min auto-veto)")
        while True:
            await asyncio.sleep(60)  # Check every minute
            now = time.time()
            expired = [
                pid for pid, info in self._pending_proposals.items()
                if now - info["ts"] > PROPOSAL_EXPIRY_SECONDS
            ]
            for pid in expired:
                info = self._pending_proposals.pop(pid)
                await self.update_message(
                    info["channel"],
                    info["message_ts"],
                    f"*EXPIRED* — {info['symbol']} {info['direction'].upper()} proposal auto-rejected (no response in 30 min)",
                )
                logger.info(f"SlackNotifier: proposal {pid} expired")

    # ── Lifecycle ─────────────────────────────────────────────

    def subscribe_to_events(self):
        """Subscribe to relevant events on the EventBus."""
        self.bus.subscribe(TradeProposal, self.on_trade_proposal, priority=Priority.NORMAL)
        self.bus.subscribe(RiskAssessmentEvent, self.on_risk_decision, priority=Priority.NORMAL)
        self.bus.subscribe(SmartMoneyEvent, self.on_smart_money, priority=Priority.NORMAL)

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "channel_id": self.channel_id,
            "pending_proposals": len(self._pending_proposals),
            "queued_alerts": len(self._high_priority_queue),
            "proposals_this_hour": len(self._proposal_timestamps),
        }
