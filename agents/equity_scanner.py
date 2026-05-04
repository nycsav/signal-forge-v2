"""Signal Forge v2 — Equity & Options Intelligence Scanner

Scans equity-focused email sources every 2 hours for stock/options signals.
Cross-references with Perplexity Sonar for validation.
Posts actionable picks to Slack DM.

Sources (work email via Claude MCP, personal via gmail-bridge):
  - Bloomberg Markets Daily (1.5x weight)
  - CNBC Pro (1.2x weight)
  - Markets Digest / Private Markets Digest (1.0x weight)
  - a16z Charts of the Week (0.8x weight)
  - StockTwits Daily Rip (0.8x weight)
  - FlashAlpha (0.8x weight — IV rank, GEX, 0DTE)

Schedule: every 2 hours, starting on engine boot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
from typing import Optional

import httpx
from loguru import logger

from config.settings import settings


# ── Constants ─────────────────────────────────────────────────

SCAN_INTERVAL = 7200  # 2 hours
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:14b"
OLLAMA_TIMEOUT = 120
GMAIL_BRIDGE_PATH = "/Users/sav/gmail-mcp-server/scripts/gmail-bridge.ts"
GMAIL_BRIDGE_CWD = "/Users/sav/gmail-mcp-server"

# Email sources to scan (personal gmail via bridge)
EQUITY_SOURCES = {
    "cnbc_pro": {
        "query": "from:cnbc.com newer_than:4h",
        "weight": 1.2,
        "description": "CNBC Pro analyst picks, earnings, market pulse",
    },
    "markets_digest": {
        "query": "from:privatemarketsdigest newer_than:4h",
        "weight": 1.0,
        "description": "Structural market moves, credit, macro",
    },
    "stocktwits": {
        "query": "from:stocktwits.com newer_than:4h",
        "weight": 0.8,
        "description": "Daily Rip — sector rotation, earnings, momentum",
    },
    "a16z": {
        "query": "from:a16z newer_than:12h",
        "weight": 0.8,
        "description": "Tech macro, VC perspective, sector analysis",
    },
    "flashalpha": {
        "query": "from:flashalpha newer_than:4h",
        "weight": 0.8,
        "description": "IV rank, GEX, options flow, 0DTE",
    },
    "schwab_alerts": {
        "query": "from:schwab.com (trade OR executed OR confirmation) newer_than:4h",
        "weight": 0.5,
        "description": "Schwab trade confirmations — portfolio monitoring",
    },
}

# Equity watchlist
EQUITY_WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "CRM", "ORCL", "TEAM", "LLY", "CAT", "STX", "XLE", "XOM", "CVX", "PLTR",
    "RDDT", "LNG", "MU", "EL", "SBUX",
]

EXTRACTION_PROMPT = """You are a stock market signal extraction engine. Analyze this financial newsletter and extract ALL stock/options trading signals.

EMAIL CONTENT:
{body}

Extract every actionable signal into a JSON array. For each signal include:
- "ticker": stock ticker symbol (e.g. "AAPL", "SPY")
- "direction": "bullish" or "bearish" or "neutral"
- "confidence": 0.0 to 1.0 based on analyst conviction
- "signal_type": one of "earnings_beat", "analyst_upgrade", "analyst_downgrade", "sector_rotation", "options_flow", "macro_signal", "price_target", "technical_breakout"
- "price_target": analyst price target if mentioned (number or null)
- "current_price": current price if mentioned (number or null)
- "key_data": brief summary of the actionable information (1-2 sentences)
- "options_play": suggested options strategy if applicable (e.g. "long call 30-45 DTE", "put spread")

Rules:
- Only extract signals with specific actionable information
- Include the ticker even if it's mentioned in passing with a directional view
- For earnings: note beat/miss and guidance direction
- For analyst calls: note the firm, rating change, and price target
- Return an empty array [] if no actionable signals found

Return ONLY a valid JSON array, no other text."""


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    try:
        stripper = _HTMLStripper()
        stripper.feed(html)
        text = stripper.get_text()
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


class EquityScanner:
    """Scans equity-focused emails for stock/options signals."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self.ollama_host = config.get("ollama_host", OLLAMA_HOST)
        self.slack_token = config.get("slack_bot_token", "")
        self.slack_dm = config.get("slack_dm_user_id", "")
        self.enabled = True
        self._last_scan_ts: float = 0
        self._seen_signals: dict[str, float] = {}  # ticker:direction -> timestamp
        self._signal_cooldown = 14400  # 4 hours

    # ── Gmail Bridge ──────────────────────────────────────────

    async def _bridge_search(self, query: str, max_results: int = 10) -> list[dict]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "npx", "tsx", GMAIL_BRIDGE_PATH, "search", query, str(max_results),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=GMAIL_BRIDGE_CWD,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0 and stdout:
                return json.loads(stdout.decode())
        except Exception as e:
            logger.debug(f"EquityScanner: bridge search error: {e}")
        return []

    async def _bridge_read(self, message_id: str) -> dict | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "npx", "tsx", GMAIL_BRIDGE_PATH, "read", message_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=GMAIL_BRIDGE_CWD,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0 and stdout:
                return json.loads(stdout.decode())
        except Exception as e:
            logger.debug(f"EquityScanner: bridge read error: {e}")
        return None

    # ── Ollama Extraction ─────────────────────────────────────

    async def _extract_signals(self, body: str, source: str) -> list[dict]:
        if not body or len(body) < 100:
            return []

        prompt = EXTRACTION_PROMPT.format(body=body[:6000])

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={
                        "model": OLLAMA_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 4096},
                    },
                )
                if resp.status_code != 200:
                    return []

                raw = resp.json().get("response", "")
                # Strip thinking tags and code fences
                cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
                cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
                cleaned = re.sub(r"```\s*", "", cleaned)

                # Find JSON array
                match = re.search(r"\[[\s\S]*\]", cleaned)
                if match:
                    signals = json.loads(match.group())
                    for s in signals:
                        s["source"] = source
                    return signals
        except Exception as e:
            logger.error(f"EquityScanner: Ollama extraction error: {e}")
        return []

    # ── Perplexity Cross-Reference ────────────────────────────

    async def _perplexity_check(self, ticker: str) -> dict | None:
        try:
            from modules.perplexity_intel import get_market_intel
            result = get_market_intel(ticker)
            return result
        except Exception as e:
            logger.debug(f"EquityScanner: Perplexity check failed for {ticker}: {e}")
        return None

    # ── Slack Posting ─────────────────────────────────────────

    async def _post_to_slack(self, message: str):
        if not self.slack_token or not self.slack_dm:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {self.slack_token}",
                        "Content-Type": "application/json",
                    },
                    json={"channel": self.slack_dm, "text": message},
                )
        except Exception as e:
            logger.error(f"EquityScanner: Slack post error: {e}")

    # ── Main Scan ─────────────────────────────────────────────

    async def _scan(self):
        logger.info("EquityScanner: scan started")
        scan_start = time.time()
        all_signals: list[dict] = []

        for source_key, source_config in EQUITY_SOURCES.items():
            try:
                emails = await self._bridge_search(source_config["query"], 5)
                if not emails:
                    logger.debug(f"EquityScanner: {source_key} — no emails")
                    continue

                logger.info(f"EquityScanner: {source_key} — {len(emails)} emails found")

                for email in emails[:3]:  # Max 3 per source
                    msg_id = email.get("id", "")
                    subject = email.get("subject", "")

                    full_msg = await self._bridge_read(msg_id)
                    if not full_msg:
                        continue

                    body = full_msg.get("body", full_msg.get("text", full_msg.get("snippet", "")))
                    body = _strip_html(body) if body else ""

                    if len(body) < 100:
                        logger.debug(f"EquityScanner: {source_key} {msg_id[:8]} — body too short")
                        continue

                    signals = await self._extract_signals(body, source_key)
                    if signals:
                        weight = source_config["weight"]
                        for s in signals:
                            s["weight"] = weight
                            s["email_subject"] = subject
                        all_signals.extend(signals)
                        logger.info(
                            f"EquityScanner: {source_key} — extracted {len(signals)} signals from '{subject[:50]}'"
                        )

            except Exception as e:
                logger.error(f"EquityScanner: {source_key} error: {e}")

        if not all_signals:
            logger.info("EquityScanner: no signals found this scan")
            self._last_scan_ts = time.time()
            return

        # Deduplicate and rank
        ticker_signals: dict[str, list[dict]] = {}
        for s in all_signals:
            ticker = s.get("ticker", "").upper()
            if ticker:
                ticker_signals.setdefault(ticker, []).append(s)

        # Cross-validate: tickers appearing in 2+ sources get boosted
        cross_validated = []
        single_source = []
        for ticker, sigs in ticker_signals.items():
            sources = set(s.get("source", "") for s in sigs)
            if len(sources) >= 2:
                cross_validated.append((ticker, sigs, sources))
            else:
                single_source.append((ticker, sigs, sources))

        # Perplexity cross-reference for top picks
        pplx_results = {}
        top_tickers = [t for t, _, _ in cross_validated[:5]] + [t for t, _, _ in single_source[:5]]
        for ticker in top_tickers[:8]:
            pplx = await self._perplexity_check(ticker)
            if pplx:
                pplx_results[ticker] = pplx

        # Format and post to Slack
        now_str = datetime.now().strftime("%b %d, %H:%M ET")
        lines = [f"*EQUITY INTELLIGENCE SCAN* — {now_str}"]
        lines.append(f"Scanned {len(EQUITY_SOURCES)} sources | {len(all_signals)} signals extracted")
        lines.append("")

        if cross_validated:
            lines.append("*CROSS-VALIDATED (2+ sources agree):*")
            for ticker, sigs, sources in sorted(cross_validated, key=lambda x: -len(x[2])):
                directions = [s.get("direction", "?") for s in sigs]
                dominant = max(set(directions), key=directions.count)
                confidences = [s.get("confidence", 0.5) for s in sigs]
                avg_conf = sum(confidences) / len(confidences)
                src_names = ", ".join(sources)
                key_data = sigs[0].get("key_data", "")[:100]
                pt = sigs[0].get("price_target")
                pt_str = f" | PT: ${pt}" if pt else ""

                # Check cooldown
                cache_key = f"{ticker}:{dominant}"
                if cache_key in self._seen_signals and time.time() - self._seen_signals[cache_key] < self._signal_cooldown:
                    continue
                self._seen_signals[cache_key] = time.time()

                pplx_note = ""
                if ticker in pplx_results:
                    p = pplx_results[ticker]
                    psent = p.get("sentiment", {})
                    pplx_note = f" | PPLX: {psent.get('direction', '?')} ({psent.get('score', 0)})"

                lines.append(
                    f"  *{ticker}* — {dominant.upper()} (conf: {avg_conf:.0%}) "
                    f"[{src_names}]{pt_str}{pplx_note}"
                )
                lines.append(f"    {key_data}")

        if single_source:
            lines.append("")
            lines.append("*SINGLE-SOURCE SIGNALS (watch list):*")
            for ticker, sigs, sources in single_source[:10]:
                s = sigs[0]
                direction = s.get("direction", "?")
                conf = s.get("confidence", 0.5)
                src = list(sources)[0]
                key_data = s.get("key_data", "")[:80]

                cache_key = f"{ticker}:{direction}"
                if cache_key in self._seen_signals and time.time() - self._seen_signals[cache_key] < self._signal_cooldown:
                    continue
                self._seen_signals[cache_key] = time.time()

                lines.append(f"  {ticker} — {direction} ({conf:.0%}) [{src}] {key_data}")

        message = "\n".join(lines)
        await self._post_to_slack(message)
        logger.info(
            f"EquityScanner: scan complete — {len(all_signals)} signals, "
            f"{len(cross_validated)} cross-validated, posted to Slack"
        )

        # Log to file
        log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "equity_scanner.log")
        with open(log_path, "a") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"EQUITY SCAN — {now_str}\n")
            f.write(message + "\n")

        self._last_scan_ts = time.time()

    async def run_forever(self):
        """Main loop: scan every 2 hours."""
        logger.info(f"EquityScanner: started (interval={SCAN_INTERVAL}s)")

        # Initial scan
        await self._safe_scan()

        while True:
            try:
                await asyncio.sleep(SCAN_INTERVAL)
                await self._safe_scan()
            except asyncio.CancelledError:
                logger.info("EquityScanner: cancelled")
                return
            except Exception as e:
                logger.error(f"EquityScanner: loop error: {e}")
                await asyncio.sleep(300)

    async def _safe_scan(self):
        try:
            await self._scan()
        except Exception as e:
            logger.error(f"EquityScanner: scan error: {e}")

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "last_scan": datetime.fromtimestamp(self._last_scan_ts).isoformat() if self._last_scan_ts else "never",
            "sources": len(EQUITY_SOURCES),
            "watchlist": len(EQUITY_WATCHLIST),
            "cached_signals": len(self._seen_signals),
        }
