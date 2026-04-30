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
        """Format a trade proposal as a rich Slack message."""
        direction_emoji = "🟢" if proposal.direction.value == "long" else "🔴"
        confidence_bar = "█" * int(proposal.ai_confidence * 10) + "░" * (10 - int(proposal.ai_confidence * 10))

        text = (
            f"{direction_emoji} *TRADE PROPOSAL: {proposal.symbol} {proposal.direction.value.upper()}*\n"
            f"Score: {proposal.raw_score:.1f} | Confidence: {proposal.ai_confidence:.0%} [{confidence_bar}]"
        )

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{proposal.symbol} — {proposal.direction.value.upper()} Proposal"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction_emoji} {proposal.direction.value.upper()}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{proposal.raw_score:.1f}"},
                    {"type": "mrkdwn", "text": f"*AI Confidence:*\n{proposal.ai_confidence:.0%} [{confidence_bar}]"},
                    {"type": "mrkdwn", "text": f"*Entry:*\n${proposal.suggested_entry:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Stop:*\n${proposal.suggested_stop:,.2f}"},
                    {"type": "mrkdwn", "text": f"*TP1 / TP2 / TP3:*\n${proposal.suggested_tp1:,.2f} / ${proposal.suggested_tp2:,.2f} / ${proposal.suggested_tp3:,.2f}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Rationale:*\n>{proposal.ai_rationale[:500]}"}
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Proposal ID: `{proposal.proposal_id}` | Auto-expires in 30 min | React :thumbsup: to approve, :thumbsdown: to reject"}
                ]
            },
            {"type": "divider"},
        ]

        return text, blocks

    def _format_risk_decision(self, event: RiskAssessmentEvent) -> str:
        """Format a risk assessment decision."""
        if event.decision.value == "approved":
            emoji = "✅"
            size_info = f" | Size: ${event.approved_size_usd:,.0f} ({event.approved_size_pct_portfolio:.1%} of portfolio)" if event.approved_size_usd else ""
        elif event.decision.value == "vetoed":
            emoji = "❌"
            size_info = f" | Reason: {event.veto_reason}" if event.veto_reason else ""
        else:
            emoji = "⚠️"
            size_info = ""

        return (
            f"{emoji} *RISK DECISION: {event.decision.value.upper()}* — `{event.proposal_id}`{size_info}\n"
            f"Risk score: {event.risk_score:.1f} | Open positions: {event.open_positions_count}"
        )

    def _format_smart_money(self, event: SmartMoneyEvent) -> str:
        """Format a smart money alert."""
        dir_emoji = "🟢" if event.direction == "bullish" else "🔴" if event.direction == "bearish" else "⚪"
        symbols = ", ".join(event.symbols)
        return (
            f"{dir_emoji} *SMART MONEY [{event.signal_type.upper()}]*: {symbols}\n"
            f"Chain: {event.chain} | Confidence: {event.confidence:.0%} | "
            f"Price: ${event.price_usd:,.4f} ({event.price_change_24h:+.1f}%)\n"
            f">{event.reason}"
        )

    def _format_whale_signal(self, signal: dict) -> str:
        """Format a whale trigger alert."""
        direction = signal.get("direction", "neutral")
        emoji = "🐋🟢" if direction == "bullish" else "🐋🔴" if direction == "bearish" else "🐋"
        strength = signal.get("strength", 0)
        return (
            f"{emoji} *WHALE TRIGGER* [{direction.upper()}] str={strength}/5\n"
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
        bias_emoji = "🟢" if "BULL" in bias else "🔴" if "BEAR" in bias else "⚪"

        sections = [f"{bias_emoji} *MORNING BRIEFING* — {datetime.now().strftime('%b %d, %Y')}"]
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
                    f"⏰ *EXPIRED* — {info['symbol']} {info['direction'].upper()} proposal auto-vetoed (no response in 30 min)",
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
