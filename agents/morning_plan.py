"""Signal Forge v2 — Morning Plan Generator

Generates a daily trading plan at 6:30 AM ET by:
  1. Reading overnight emails via gmail-bridge CLI
  2. Extracting signals via Ollama (qwen3:14b)
  3. Cross-referencing with Perplexity Sonar
  4. Pulling current SignalForge state (regime, F&G, whales)
  5. Formatting a comprehensive plan
  6. Posting to Slack DM

Schedule: daily at 06:30 ET via async run_forever loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from loguru import logger

from agents.email_parsers import strip_html, parse_llm_response
from config.settings import settings

# ── Constants ─────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")
PLAN_HOUR = 6
PLAN_MINUTE = 30

GMAIL_BRIDGE = "/Users/sav/gmail-mcp-server/scripts/gmail-bridge.ts"
BRIDGE_TIMEOUT = 30  # seconds per bridge call
OLLAMA_TIMEOUT = 90  # seconds per Ollama call

# Email sources to scan overnight
EMAIL_QUERIES = {
    "CoinGecko": "from:coingecko.com newer_than:12h",
    "CoinMarketCap": "from:coinmarketcap newer_than:12h",
    "altFINS": "from:altfins.com newer_than:12h",
    "Coinbase Research": "from:coinbase.com (research OR institutional) newer_than:12h",
    "StockTwits": "from:stocktwits.com newer_than:12h",
    "Private Markets Digest": "from:privatemarketsdigest newer_than:12h",
}

MAX_EMAILS_PER_SOURCE = 5

EXTRACTION_PROMPT = """\
Analyze the following email newsletter and extract trading signals.

Return a JSON array of objects, each with:
- "ticker": string (e.g. "BTC", "ETH", "SOL")
- "direction": "bullish" | "bearish" | "neutral"
- "confidence": float 0.0-1.0
- "price_target": string or null (e.g. "$95,000")
- "support": string or null
- "resistance": string or null
- "key_data": string (one-line summary of the key data point)
- "action": string (e.g. "BUY on dip to $92k", "HOLD", "AVOID")

If no trading signals are found, return an empty array: []
Do NOT include any text outside the JSON array.

Email content:
{body}
"""

SLACK_API_BASE = "https://slack.com/api"


# ── Main Class ────────────────────────────────────────────────

class MorningPlanGenerator:
    """Generates and posts a daily morning trading plan."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self.ollama_host = config.get("ollama_host", settings.ollama_host)
        self.ollama_model = config.get("ollama_model", "qwen3:14b")
        self.slack_bot_token = config.get("slack_bot_token", settings.slack_bot_token)
        self.slack_dm_user_id = config.get("slack_dm_user_id", settings.slack_dm_user_id)
        self.feedback_log_path = config.get(
            "feedback_log_path",
            "/Users/sav/signal-forge-v2/logs/feedback_loop.log",
        )

    # ── 1. Read Overnight Emails ──────────────────────────────

    async def _bridge_call(self, *args: str) -> str:
        """Call gmail-bridge.ts via subprocess, return stdout."""
        cmd = ["npx", "tsx", GMAIL_BRIDGE, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.dirname(os.path.dirname(GMAIL_BRIDGE)),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=BRIDGE_TIMEOUT
            )
            if proc.returncode != 0:
                logger.warning(
                    f"MorningPlan: bridge error ({' '.join(args[:2])}): "
                    f"{stderr.decode()[:200]}"
                )
                return ""
            return stdout.decode()
        except asyncio.TimeoutError:
            logger.warning(f"MorningPlan: bridge timeout ({' '.join(args[:2])})")
            return ""
        except Exception as e:
            logger.error(f"MorningPlan: bridge call failed: {e}")
            return ""

    async def _search_emails(self, query: str, max_results: int = 5) -> list[dict]:
        """Search Gmail via bridge, return list of message summaries."""
        raw = await self._bridge_call("search", query, str(max_results))
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("messages", data.get("results", [data]))
            return []
        except json.JSONDecodeError:
            logger.warning(f"MorningPlan: search JSON parse failed for query: {query[:40]}")
            return []

    async def _read_email(self, message_id: str) -> str:
        """Read full email body via bridge, strip HTML to plain text."""
        raw = await self._bridge_call("read", message_id)
        if not raw.strip():
            return ""
        # Try JSON parse first (bridge may return structured data)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                body = (
                    data.get("body")
                    or data.get("text")
                    or data.get("textBody")
                    or data.get("htmlBody")
                    or data.get("html")
                    or data.get("snippet")
                    or ""
                )
                return strip_html(body) if "<" in body else body
            if isinstance(data, str):
                return strip_html(data) if "<" in data else data
        except json.JSONDecodeError:
            pass
        # Raw text fallback
        return strip_html(raw) if "<" in raw else raw

    async def _fetch_all_emails(self) -> dict[str, list[dict]]:
        """Fetch emails from all sources. Returns {source: [{id, subject, body}, ...]}."""
        results: dict[str, list[dict]] = {}

        for source_name, query in EMAIL_QUERIES.items():
            logger.info(f"MorningPlan: scanning {source_name}...")
            summaries = await self._search_emails(query, MAX_EMAILS_PER_SOURCE)
            if not summaries:
                logger.debug(f"MorningPlan: {source_name} — no emails")
                continue

            emails = []
            for summary in summaries[:MAX_EMAILS_PER_SOURCE]:
                msg_id = summary.get("id", "")
                if not msg_id:
                    continue
                body = await self._read_email(msg_id)
                if body and len(body.strip()) > 50:
                    emails.append({
                        "id": msg_id,
                        "subject": summary.get("subject", ""),
                        "body": body,
                    })

            if emails:
                results[source_name] = emails
                logger.info(f"MorningPlan: {source_name} — {len(emails)} emails read")

        return results

    # ── 2. Extract Signals via Ollama ─────────────────────────

    async def _extract_signals(self, body: str) -> list[dict]:
        """Send email body to Ollama for signal extraction."""
        truncated = body[:8000].replace("{", "(").replace("}", ")")
        prompt = EXTRACTION_PROMPT.format(body=truncated)

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                r = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={
                        "model": self.ollama_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.1,
                            "num_predict": 2000,
                        },
                    },
                )
                if r.status_code != 200:
                    logger.warning(f"MorningPlan: Ollama returned {r.status_code}")
                    return []

                raw_response = r.json().get("response", "")
                if not raw_response:
                    return []

                return parse_llm_response(raw_response)

        except httpx.TimeoutException:
            logger.warning(f"MorningPlan: Ollama timeout ({OLLAMA_TIMEOUT}s)")
        except Exception as e:
            logger.error(f"MorningPlan: Ollama error: {e}")
        return []

    async def _extract_all_signals(
        self, emails_by_source: dict[str, list[dict]]
    ) -> dict[str, list[dict]]:
        """Extract signals from all emails. Returns {source: [signal_dicts]}."""
        signals_by_source: dict[str, list[dict]] = {}

        for source_name, emails in emails_by_source.items():
            source_signals = []
            for email in emails:
                extracted = await self._extract_signals(email["body"])
                for sig in extracted:
                    sig["_source"] = source_name
                    sig["_subject"] = email.get("subject", "")
                source_signals.extend(extracted)

            if source_signals:
                signals_by_source[source_name] = source_signals
                logger.info(
                    f"MorningPlan: {source_name} — {len(source_signals)} signals extracted"
                )

        return signals_by_source

    # ── 3. Cross-Reference with Perplexity ────────────────────

    async def _perplexity_cross_reference(
        self, unique_tickers: list[str]
    ) -> dict[str, dict]:
        """Get Perplexity intel for each unique ticker. Returns {ticker: intel_dict}."""
        perplexity_data: dict[str, dict] = {}

        try:
            from modules.perplexity_intel import get_market_intel
        except ImportError:
            logger.warning("MorningPlan: perplexity_intel not available, skipping cross-ref")
            return perplexity_data

        if not settings.perplexity_api_key:
            logger.info("MorningPlan: no PPLX_API_KEY, skipping Perplexity cross-ref")
            return perplexity_data

        for ticker in unique_tickers[:10]:  # cap at 10 to control cost
            try:
                intel = get_market_intel(ticker, asset_type="crypto")
                if intel:
                    perplexity_data[ticker] = intel
                    logger.debug(f"MorningPlan: Perplexity intel for {ticker}: OK")
            except Exception as e:
                logger.warning(f"MorningPlan: Perplexity failed for {ticker}: {e}")

        return perplexity_data

    # ── 4. Get Current SignalForge State ──────────────────────

    def _read_feedback_loop(self) -> dict:
        """Read the last feedback loop entry from logs."""
        state = {
            "signals": 0,
            "vetoes": 0,
            "consensus": "0%",
            "whales": "unknown",
            "details": [],
        }

        path = Path(self.feedback_log_path)
        if not path.exists():
            return state

        try:
            text = path.read_text()
            # Split on separator line and take last non-empty block
            blocks = text.split("=" * 60)
            blocks = [b.strip() for b in blocks if b.strip()]
            if not blocks:
                return state

            last_block = blocks[-1]
            lines = last_block.strip().split("\n")

            # Parse header line: "FEEDBACK LOOP — 2026-05-04 09:52"
            for line in lines:
                if "Signals:" in line and "Vetoes:" in line:
                    m = re.search(r"Signals:\s*(\d+)", line)
                    if m:
                        state["signals"] = int(m.group(1))
                    m = re.search(r"Vetoes:\s*(\d+)", line)
                    if m:
                        state["vetoes"] = int(m.group(1))
                    m = re.search(r"Consensus:\s*([\d.]+%)", line)
                    if m:
                        state["consensus"] = m.group(1)
                    m = re.search(r"Whales:\s*(\w+)", line)
                    if m:
                        state["whales"] = m.group(1)
                elif line.startswith("  "):
                    state["details"].append(line.strip())

            return state
        except Exception as e:
            logger.warning(f"MorningPlan: feedback log read failed: {e}")
            return state

    def _get_forge_state(self) -> dict:
        """Assemble current SignalForge state from feedback loop + settings."""
        feedback = self._read_feedback_loop()
        return {
            "feedback": feedback,
            "watchlist": settings.watchlist,
            "mode": settings.mode,
            "max_positions": settings.max_open_positions,
            "min_signal_score": settings.min_signal_score,
        }

    # ── 5. Generate the Plan ──────────────────────────────────

    def _find_cross_validated(
        self, signals_by_source: dict[str, list[dict]]
    ) -> list[dict]:
        """Find signals that appear in 2+ sources with same direction."""
        # Build (ticker, direction) -> set of sources
        ticker_sources: dict[tuple[str, str], set[str]] = {}
        ticker_signals: dict[tuple[str, str], list[dict]] = {}

        for source, signals in signals_by_source.items():
            for sig in signals:
                ticker = (sig.get("ticker") or "").upper()
                direction = sig.get("direction", "neutral")
                if not ticker or direction == "neutral":
                    continue
                key = (ticker, direction)
                ticker_sources.setdefault(key, set()).add(source)
                ticker_signals.setdefault(key, []).append(sig)

        # Filter to 2+ sources
        cross_validated = []
        for key, sources in ticker_sources.items():
            if len(sources) >= 2:
                best = max(
                    ticker_signals[key],
                    key=lambda s: float(s.get("confidence", 0)),
                )
                cross_validated.append({
                    "ticker": key[0],
                    "direction": key[1],
                    "sources": sorted(sources),
                    "source_count": len(sources),
                    "confidence": best.get("confidence", 0),
                    "price_target": best.get("price_target"),
                    "support": best.get("support"),
                    "resistance": best.get("resistance"),
                    "action": best.get("action", ""),
                })

        return sorted(cross_validated, key=lambda x: -x.get("confidence", 0))

    def _format_plan(
        self,
        forge_state: dict,
        signals_by_source: dict[str, list[dict]],
        cross_validated: list[dict],
        perplexity_data: dict[str, dict],
    ) -> str:
        """Format the complete morning plan as a readable string."""
        now_et = datetime.now(ET)
        lines: list[str] = []

        # Header
        lines.append(f"MORNING TRADING PLAN — {now_et.strftime('%A, %B %d, %Y %H:%M ET')}")
        lines.append("=" * 60)

        # Market overview
        fb = forge_state["feedback"]
        lines.append("")
        lines.append("MARKET OVERVIEW")
        lines.append("-" * 40)
        lines.append(f"  Whales:      {fb['whales']}")
        lines.append(f"  Consensus:   {fb['consensus']}")
        lines.append(f"  Last Signals: {fb['signals']}  |  Vetoes: {fb['vetoes']}")
        lines.append(f"  Mode:        {forge_state['mode']}  |  Max Positions: {forge_state['max_positions']}")
        if fb["details"]:
            lines.append(f"  Notes:")
            for detail in fb["details"][:5]:
                lines.append(f"    - {detail}")

        # Email signal summary
        lines.append("")
        lines.append("EMAIL SIGNAL SUMMARY")
        lines.append("-" * 40)
        total_signals = 0
        for source, signals in signals_by_source.items():
            total_signals += len(signals)
            tickers = set()
            directions = {"bullish": 0, "bearish": 0, "neutral": 0}
            for sig in signals:
                t = (sig.get("ticker") or "").upper()
                if t:
                    tickers.add(t)
                d = sig.get("direction", "neutral")
                directions[d] = directions.get(d, 0) + 1

            sentiment = []
            if directions["bullish"]:
                sentiment.append(f"{directions['bullish']} bullish")
            if directions["bearish"]:
                sentiment.append(f"{directions['bearish']} bearish")
            if directions["neutral"]:
                sentiment.append(f"{directions['neutral']} neutral")

            lines.append(f"  {source}: {', '.join(sorted(tickers)) or 'no tickers'}")
            lines.append(f"    Sentiment: {', '.join(sentiment) or 'none'}")

        if total_signals == 0:
            lines.append("  (no email signals found overnight)")

        # Cross-validated picks
        lines.append("")
        lines.append("CROSS-VALIDATED PICKS (2+ sources agree)")
        lines.append("-" * 40)
        if cross_validated:
            for pick in cross_validated:
                lines.append(
                    f"  {pick['ticker']} — {pick['direction'].upper()} "
                    f"(conf: {pick['confidence']:.0%}, {pick['source_count']} sources: "
                    f"{', '.join(pick['sources'])})"
                )
                if pick.get("price_target"):
                    lines.append(f"    Target: {pick['price_target']}")
                if pick.get("support") or pick.get("resistance"):
                    parts = []
                    if pick.get("support"):
                        parts.append(f"S: {pick['support']}")
                    if pick.get("resistance"):
                        parts.append(f"R: {pick['resistance']}")
                    lines.append(f"    Levels: {' | '.join(parts)}")
                if pick.get("action"):
                    lines.append(f"    Action: {pick['action']}")
        else:
            lines.append("  (no cross-validated signals — single-source only)")

        # Perplexity cross-reference
        if perplexity_data:
            lines.append("")
            lines.append("PERPLEXITY MARKET INTEL")
            lines.append("-" * 40)
            for ticker, intel in perplexity_data.items():
                pplx_sentiment = intel.get("sentiment", "unknown")
                edge_score = intel.get("edge_score", "N/A")
                catalysts = intel.get("catalysts", [])
                lines.append(f"  {ticker}: sentiment={pplx_sentiment}, edge={edge_score}")

                # Compare with email signals
                email_dirs = set()
                for source_signals in signals_by_source.values():
                    for sig in source_signals:
                        if (sig.get("ticker") or "").upper() == ticker.upper():
                            email_dirs.add(sig.get("direction", "neutral"))
                if email_dirs:
                    lines.append(f"    Email says: {', '.join(email_dirs)} | Perplexity says: {pplx_sentiment}")

                if catalysts:
                    for c in catalysts[:3]:
                        lines.append(f"    - {c}")

        # Recommended trades
        lines.append("")
        lines.append("RECOMMENDED TRADES")
        lines.append("-" * 40)
        if cross_validated:
            for pick in cross_validated[:5]:
                entry_line = f"  {pick['ticker']} {pick['direction'].upper()}"
                if pick.get("support") and pick["direction"] == "bullish":
                    entry_line += f" — Entry near {pick['support']}"
                elif pick.get("resistance") and pick["direction"] == "bearish":
                    entry_line += f" — Entry near {pick['resistance']}"
                if pick.get("price_target"):
                    entry_line += f" — Target {pick['price_target']}"
                lines.append(entry_line)
                if pick.get("action"):
                    lines.append(f"    >> {pick['action']}")
        else:
            # Fall back to highest-confidence single-source signals
            all_sigs = []
            for source, sigs in signals_by_source.items():
                for s in sigs:
                    s["_src"] = source
                    all_sigs.append(s)
            top = sorted(all_sigs, key=lambda x: -float(x.get("confidence", 0)))[:3]
            if top:
                lines.append("  (single-source, lower confidence):")
                for s in top:
                    ticker = (s.get("ticker") or "?").upper()
                    direction = s.get("direction", "neutral")
                    conf = float(s.get("confidence", 0))
                    action = s.get("action", "")
                    lines.append(
                        f"  {ticker} {direction.upper()} (conf: {conf:.0%}, src: {s.get('_src', '?')})"
                    )
                    if action:
                        lines.append(f"    >> {action}")
            else:
                lines.append("  No actionable trades identified.")

        # Risk warnings
        lines.append("")
        lines.append("RISK WARNINGS")
        lines.append("-" * 40)
        warnings = []
        if fb["whales"] == "distribution":
            warnings.append("Whales distributing — reduce position sizes")
        if fb["consensus"] == "0%":
            warnings.append("Zero model consensus — market direction unclear")
        if fb["vetoes"] > 0:
            warnings.append(f"{fb['vetoes']} recent vetoes — risk limits may be binding")
        if not cross_validated and total_signals > 0:
            warnings.append("No cross-validation — signals are single-source only")
        if total_signals == 0:
            warnings.append("No overnight email signals — limited intel")

        if warnings:
            for w in warnings:
                lines.append(f"  !! {w}")
        else:
            lines.append("  No major risk flags.")

        lines.append("")
        lines.append("=" * 60)
        lines.append("Generated by Signal Forge v2 — Morning Plan Agent")

        return "\n".join(lines)

    # ── 6. Post to Slack ──────────────────────────────────────

    async def _post_to_slack(self, plan_text: str) -> bool:
        """Post the morning plan to Slack DM channel."""
        if not self.slack_bot_token or not self.slack_dm_user_id:
            logger.warning("MorningPlan: Slack not configured (missing token or DM user ID)")
            return False

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Open DM conversation
                dm_resp = await client.post(
                    f"{SLACK_API_BASE}/conversations.open",
                    headers={
                        "Authorization": f"Bearer {self.slack_bot_token}",
                        "Content-Type": "application/json",
                    },
                    json={"users": self.slack_dm_user_id},
                )
                dm_data = dm_resp.json()
                if not dm_data.get("ok"):
                    logger.error(
                        f"MorningPlan: Slack DM open failed: {dm_data.get('error', 'unknown')}"
                    )
                    return False

                channel_id = dm_data["channel"]["id"]

                # Post message
                msg_resp = await client.post(
                    f"{SLACK_API_BASE}/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {self.slack_bot_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "channel": channel_id,
                        "text": f"```\n{plan_text}\n```",
                    },
                )
                msg_data = msg_resp.json()
                if not msg_data.get("ok"):
                    logger.error(
                        f"MorningPlan: Slack post failed: {msg_data.get('error', 'unknown')}"
                    )
                    return False

                logger.info("MorningPlan: plan posted to Slack DM")
                return True

        except Exception as e:
            logger.error(f"MorningPlan: Slack error: {e}")
            return False

    # ── Orchestration ─────────────────────────────────────────

    async def generate_plan(self) -> str:
        """Run the full morning plan pipeline. Returns the formatted plan."""
        logger.info("MorningPlan: starting plan generation...")

        # 1. Fetch overnight emails
        emails_by_source = await self._fetch_all_emails()

        # 2. Extract signals via Ollama
        signals_by_source = await self._extract_all_signals(emails_by_source)

        # 3. Collect unique tickers for Perplexity
        unique_tickers: list[str] = []
        seen: set[str] = set()
        for signals in signals_by_source.values():
            for sig in signals:
                t = (sig.get("ticker") or "").upper()
                if t and t not in seen:
                    unique_tickers.append(t)
                    seen.add(t)

        # 4. Cross-reference with Perplexity
        perplexity_data = await self._perplexity_cross_reference(unique_tickers)

        # 5. Get SignalForge state
        forge_state = self._get_forge_state()

        # 6. Find cross-validated picks
        cross_validated = self._find_cross_validated(signals_by_source)

        # 7. Format the plan
        plan = self._format_plan(
            forge_state, signals_by_source, cross_validated, perplexity_data
        )

        logger.info(
            f"MorningPlan: plan generated — {len(signals_by_source)} sources, "
            f"{sum(len(s) for s in signals_by_source.values())} signals, "
            f"{len(cross_validated)} cross-validated"
        )

        return plan

    async def run_once(self) -> str:
        """Generate the plan and post to Slack. Returns the plan text."""
        plan = await self.generate_plan()

        # Post to Slack
        await self._post_to_slack(plan)

        # Also save to logs
        log_path = Path("/Users/sav/signal-forge-v2/logs/morning_plan.log")
        try:
            with log_path.open("a") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(plan)
                f.write("\n")
            logger.info(f"MorningPlan: plan saved to {log_path}")
        except Exception as e:
            logger.warning(f"MorningPlan: failed to save plan log: {e}")

        return plan

    # ── 7. Scheduler ──────────────────────────────────────────

    def _seconds_until_next_run(self) -> float:
        """Calculate seconds until next 6:30 AM ET."""
        now_et = datetime.now(ET)
        target = now_et.replace(
            hour=PLAN_HOUR, minute=PLAN_MINUTE, second=0, microsecond=0
        )
        if target <= now_et:
            target += timedelta(days=1)
        delta = (target - now_et).total_seconds()
        return delta

    async def run_forever(self) -> None:
        """Async loop: sleep until 6:30 AM ET, generate plan, repeat."""
        logger.info("MorningPlan: scheduler started (daily at 6:30 AM ET)")

        while True:
            try:
                sleep_secs = self._seconds_until_next_run()
                next_run_et = datetime.now(ET) + timedelta(seconds=sleep_secs)
                logger.info(
                    f"MorningPlan: next run at {next_run_et.strftime('%Y-%m-%d %H:%M ET')} "
                    f"({sleep_secs / 3600:.1f}h from now)"
                )
                await asyncio.sleep(sleep_secs)
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("MorningPlan: scheduler cancelled")
                return
            except Exception as e:
                logger.error(f"MorningPlan: scheduler error: {e}")
                # Wait 10 min before retrying on unexpected error
                await asyncio.sleep(600)
