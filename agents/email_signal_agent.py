"""Signal Forge v2 — Email Signal Agent

Polls Gmail via MCP subprocess for 6 newsletter sources, extracts trading
signals via Ollama (Qwen3), cross-validates across sources, and publishes
EmailSignalEvents to the EventBus.

Schedule: 3x/day at 06:00, 14:00, 22:00 EST + immediate scan on startup.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from agents.event_bus import EventBus, Priority
from agents.email_parsers import (
    EMAIL_SOURCES,
    MAX_EMAIL_BONUS_PER_SYMBOL,
    CROSS_VALIDATION_BONUS,
    CROSS_VALIDATION_CONFIDENCE_BOOST,
    parse_llm_response,
    strip_html,
)
from config.settings import settings

# ── Constants ─────────────────────────────────────────────────

EST = ZoneInfo("America/New_York")
SCAN_HOURS_EST_DEFAULT = [6, 14, 22]

GMAIL_MCP_SERVER_PATH_DEFAULT = "/Users/sav/gmail-mcp-server/src/server.ts"
GMAIL_MCP_COMMAND = "npx"

MAX_EMAILS_PER_SOURCE = 20
LOOKBACK_HOURS = 10
GMAIL_CALL_TIMEOUT = 30      # seconds per Gmail MCP call
OLLAMA_CALL_TIMEOUT = 60     # seconds per Ollama extraction call
SIGNAL_CACHE_TTL = 8 * 3600  # 8 hours — signals expire between scans

DB_TABLE = "email_signals_processed"


# ── Event Model ───────────────────────────────────────────────

# Import the canonical event definition from events.py (single source of truth)
from agents.events import EmailSignalEvent


# ── Main Agent ────────────────────────────────────────────────

class EmailSignalAgent:
    """Polls Gmail newsletters for trading signals, extracts via Ollama,
    cross-validates across sources, and publishes to the EventBus.

    Lookup methods (get_email_bonus, get_regime_adjustment, get_fragility_flag)
    are consumed by the orchestrator and scoring pipeline.
    """

    def __init__(self, event_bus: EventBus, config: dict | None = None):
        self.bus = event_bus
        config = config or {}
        self.ollama_host = config.get("ollama_host", settings.ollama_host)
        self.extract_model = config.get("extract_model", "qwen3:14b")
        self.db_path = config.get("database_path", settings.database_path)
        self.gmail_mcp_server_path = config.get("gmail_mcp_server_path", GMAIL_MCP_SERVER_PATH_DEFAULT)
        self.gmail_mcp_args = ["tsx", self.gmail_mcp_server_path]
        self.scan_hours_est = config.get("email_scan_hours_est", SCAN_HOURS_EST_DEFAULT)
        self.enabled = config.get("email_signal_enabled", True)

        # In-memory signal cache: symbol -> list[EmailSignalEvent]
        self._signal_cache: dict[str, list[EmailSignalEvent]] = {}
        self._cache_updated_at: float = 0.0

        # Regime / fragility state derived from latest scan
        self._regime_adjustment: float = 0.0     # +/- modifier for regime
        self._fragility_flag: bool = False        # True if risk events detected

        # MCP session (created per scan)
        self._mcp_session = None

        self._ensure_db_table()

    # ── Database Setup ────────────────────────────────────────

    def _ensure_db_table(self) -> None:
        """Create the dedup table if it does not exist."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {DB_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_message_id TEXT UNIQUE,
                    source TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    signal_type TEXT,
                    symbols TEXT,
                    direction TEXT,
                    confidence REAL,
                    raw_extraction TEXT
                )
            """)
            conn.commit()
            conn.close()
            logger.debug("EmailSignalAgent: DB table ensured")
        except Exception as e:
            logger.error(f"EmailSignalAgent: DB setup failed: {e}")

    def _is_processed(self, gmail_message_id: str) -> bool:
        """Check if a message has already been processed."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            row = conn.execute(
                f"SELECT 1 FROM {DB_TABLE} WHERE gmail_message_id = ? LIMIT 1",
                (gmail_message_id,),
            ).fetchone()
            conn.close()
            return row is not None
        except Exception as e:
            logger.error(f"EmailSignalAgent: dedup check failed: {e}")
            return False

    def _mark_processed(self, gmail_message_id: str, source: str,
                        signal_type: str, symbols: list[str],
                        direction: str, confidence: float,
                        raw_extraction: str) -> None:
        """Record a processed message in the dedup table."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute(
                f"""INSERT OR IGNORE INTO {DB_TABLE}
                    (gmail_message_id, source, processed_at, signal_type,
                     symbols, direction, confidence, raw_extraction)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    gmail_message_id,
                    source,
                    datetime.now(timezone.utc).isoformat(),
                    signal_type,
                    json.dumps(symbols),
                    direction,
                    confidence,
                    raw_extraction[:2000],  # cap raw storage
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"EmailSignalAgent: mark_processed failed: {e}")

    # ── Background Loop ───────────────────────────────────────

    async def start(self) -> None:
        """Launch the scheduled polling loop. Call once from orchestrator."""
        if not self.enabled:
            logger.info("EmailSignalAgent disabled via config (email_signal_enabled=False)")
            return
        asyncio.create_task(self._run_forever())
        logger.info(
            f"EmailSignalAgent started (schedule: {self.scan_hours_est} EST, "
            f"sources: {len(EMAIL_SOURCES)}, model: {self.extract_model})"
        )

    async def _run_forever(self) -> None:
        """Main loop: immediate scan on startup, then scheduled at EST hours."""
        # Immediate scan on startup
        await self._safe_scan("startup")

        while True:
            try:
                sleep_secs = self._seconds_until_next_scan()
                logger.info(
                    f"EmailSignalAgent: next scan in {sleep_secs / 60:.0f} min"
                )
                await asyncio.sleep(sleep_secs)
                await self._safe_scan("scheduled")
            except asyncio.CancelledError:
                logger.info("EmailSignalAgent: cancelled")
                return
            except Exception as e:
                logger.error(f"EmailSignalAgent: loop error: {e}")
                await asyncio.sleep(300)  # wait 5 min on unexpected error

    def _seconds_until_next_scan(self) -> float:
        """Calculate seconds until the next scheduled scan time in EST."""
        now_est = datetime.now(EST)
        for hour in sorted(self.scan_hours_est):
            target = now_est.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target > now_est:
                return (target - now_est).total_seconds()

        # All today's scans passed — next is first scan tomorrow
        tomorrow = now_est + timedelta(days=1)
        target = tomorrow.replace(
            hour=self.scan_hours_est[0], minute=0, second=0, microsecond=0
        )
        return (target - now_est).total_seconds()

    async def _safe_scan(self, trigger: str) -> None:
        """Run a full scan with top-level error handling."""
        try:
            logger.info(f"EmailSignalAgent: scan started (trigger={trigger})")
            await self._full_scan()
            logger.info("EmailSignalAgent: scan complete")
        except Exception as e:
            logger.error(f"EmailSignalAgent: scan failed: {e}")

    # ── Full Scan Pipeline ────────────────────────────────────

    async def _full_scan(self) -> None:
        """Scan all sources, extract signals, cross-validate, publish."""
        all_signals: list[EmailSignalEvent] = []

        for source_name, source_cfg in EMAIL_SOURCES.items():
            try:
                signals = await self._scan_source(source_name, source_cfg)
                all_signals.extend(signals)
            except Exception as e:
                logger.error(f"EmailSignalAgent: source {source_name} failed: {e}")

        if not all_signals:
            logger.info("EmailSignalAgent: no new signals found across all sources")
            return

        # Cross-validate signals
        cross_validated = self._cross_validate(all_signals)

        # Update caches
        self._update_signal_cache(cross_validated)
        self._update_regime_and_fragility(cross_validated)

        # Publish events
        published = 0
        for signal in cross_validated:
            priority = (
                Priority.HIGH
                if signal.signal_type == "pattern_breakout"
                else Priority.NORMAL
            )
            try:
                await self.bus.publish(signal, priority=priority)
                published += 1
            except Exception as e:
                logger.error(f"EmailSignalAgent: publish failed: {e}")

        logger.info(
            f"EmailSignalAgent: published {published} signals "
            f"({sum(1 for s in cross_validated if s.cross_validated)} cross-validated)"
        )

    # ── Per-Source Scanning ───────────────────────────────────

    async def _scan_source(
        self, source_name: str, source_cfg: dict
    ) -> list[EmailSignalEvent]:
        """Search Gmail for a single source, fetch bodies, extract signals."""
        gmail_query = source_cfg["gmail_query"]
        extract_prompt_template = source_cfg["extract_prompt"]
        bonus_map = source_cfg["score_bonus_map"]

        # Search Gmail via MCP
        search_results = await self._gmail_mcp_call(
            "search_emails",
            {"query": gmail_query, "max_results": MAX_EMAILS_PER_SOURCE},
        )

        if not search_results:
            logger.debug(f"EmailSignalAgent: {source_name} — no emails found")
            return []

        # Parse search results
        emails = self._parse_mcp_result(search_results)
        if not emails:
            return []

        signals: list[EmailSignalEvent] = []
        processed_count = 0

        for email_summary in emails[:MAX_EMAILS_PER_SOURCE]:
            msg_id = email_summary.get("id", "")
            if not msg_id:
                continue

            # Dedup check
            if self._is_processed(msg_id):
                continue

            # Fetch full email body
            try:
                full_email = await self._gmail_mcp_call(
                    "read_email",
                    {"message_id": msg_id, "format": "full"},
                )
                body_text = self._extract_body_text(full_email)
            except Exception as e:
                logger.debug(
                    f"EmailSignalAgent: {source_name} read {msg_id} failed: {e}"
                )
                continue

            if not body_text or len(body_text.strip()) < 50:
                logger.debug(
                    f"EmailSignalAgent: {source_name} {msg_id} — body too short, skipping"
                )
                # Still mark as processed to avoid re-fetching
                self._mark_processed(msg_id, source_name, "skip", [], "neutral", 0.0, "")
                continue

            # Truncate and sanitize body to avoid Ollama context limits and .format() issues
            body_truncated = body_text[:8000].replace("{", "(").replace("}", ")").replace("$", "USD ")

            # Extract signals via Ollama
            prompt = extract_prompt_template.format(body=body_truncated)
            extracted = await self._extract_signals_ollama(prompt)

            if not extracted:
                self._mark_processed(msg_id, source_name, "no_signals", [], "neutral", 0.0, "")
                continue

            # Convert extracted dicts into EmailSignalEvents
            for sig_dict in extracted:
                signal_type = sig_dict.get("signal_type", "unknown")
                symbols = sig_dict.get("symbols", [])
                direction = sig_dict.get("direction", "neutral")
                confidence = float(sig_dict.get("confidence", 0.5))
                details = sig_dict.get("details", {})

                # Calculate score bonus
                base_bonus = float(bonus_map.get(signal_type, 0))
                score_bonus = self._apply_bonus_conditions(
                    signal_type, base_bonus, confidence, direction, details, source_name
                )

                event = EmailSignalEvent(
                    timestamp=datetime.now(timezone.utc),
                    source=source_name,
                    signal_type=signal_type,
                    symbols=[s.upper() for s in symbols if isinstance(s, str)],
                    direction=direction,
                    confidence=min(1.0, max(0.0, confidence)),
                    score_bonus=score_bonus,
                    cross_validated=False,  # set later
                    details=details,
                    gmail_message_id=msg_id,
                )
                signals.append(event)

            # Mark message as processed with first signal info
            first = extracted[0] if extracted else {}
            self._mark_processed(
                msg_id, source_name,
                first.get("signal_type", "unknown"),
                first.get("symbols", []),
                first.get("direction", "neutral"),
                float(first.get("confidence", 0.0)),
                json.dumps(extracted)[:2000],
            )

            # Label the email as "crypto-signal" and mark as read in Gmail
            await self._label_email(msg_id)
            processed_count += 1

        if processed_count > 0:
            logger.info(
                f"EmailSignalAgent: {source_name} — processed {processed_count} emails, "
                f"{len(signals)} signals extracted"
            )
        return signals

    # ── Bonus Conditions ──────────────────────────────────────

    @staticmethod
    def _apply_bonus_conditions(
        signal_type: str,
        base_bonus: float,
        confidence: float,
        direction: str,
        details: dict,
        source_name: str,
    ) -> float:
        """Apply conditional logic to determine if a signal qualifies for its bonus.

        Returns the score bonus (0 if conditions not met).
        """
        if base_bonus <= 0:
            return 0.0

        # altFINS pattern_breakout: confidence >= 0.6
        if signal_type == "pattern_breakout":
            return base_bonus if confidence >= 0.6 else 0.0

        # altFINS smart_money_flow: flow > $5M
        if signal_type == "smart_money_flow":
            amount = details.get("amount_usd", 0)
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = 0
            return base_bonus if amount > 5_000_000 else 0.0

        # Coinbase regime_call: bullish + confidence >= 0.7
        if signal_type == "regime_call":
            return base_bonus if direction == "bullish" and confidence >= 0.7 else 0.0

        # Coinbase etf_flow: inflow direction
        if signal_type == "etf_flow":
            flow_dir = details.get("flow_direction", "")
            return base_bonus if flow_dir == "inflow" or direction == "bullish" else 0.0

        # CMC fg_extreme: F&G < 20 (contrarian bullish)
        if signal_type == "fg_extreme":
            fg_val = details.get("fear_greed_value", 50)
            try:
                fg_val = float(fg_val)
            except (TypeError, ValueError):
                fg_val = 50
            return base_bonus if fg_val < 20 else 0.0

        # CMC funding_negative_extended: >30 consecutive days
        if signal_type == "funding_negative_extended":
            days = details.get("consecutive_days", 0)
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = 0
            return base_bonus if days > 30 else 0.0

        # CoinGecko trending_token: 2+ appearances
        if signal_type == "trending_token":
            appearances = details.get("appearances", 1)
            try:
                appearances = int(appearances)
            except (TypeError, ValueError):
                appearances = 1
            return base_bonus if appearances >= 2 else 0.0

        # Stocktwits macro_regime: risk-on
        if signal_type == "macro_regime":
            regime = details.get("regime", "neutral")
            return base_bonus if regime == "risk-on" or direction == "bullish" else 0.0

        # Cheap Investor whale_accumulation: always if signal detected
        if signal_type == "whale_accumulation":
            return base_bonus

        # Default: return base if signal exists
        return base_bonus

    # ── Cross-Validation ──────────────────────────────────────

    def _cross_validate(
        self, signals: list[EmailSignalEvent]
    ) -> list[EmailSignalEvent]:
        """Mark signals as cross-validated when 2+ sources agree on
        the same symbol + direction. Boost confidence by +0.15 and
        add +3 bonus points.

        Returns the same list with cross_validated fields updated.
        """
        # Build index: (symbol, direction) -> set of sources
        agreement_map: dict[tuple[str, str], set[str]] = {}
        for sig in signals:
            if sig.direction == "neutral":
                continue
            for sym in sig.symbols:
                key = (sym, sig.direction)
                if key not in agreement_map:
                    agreement_map[key] = set()
                agreement_map[key].add(sig.source)

        # Find cross-validated pairs
        validated_keys: set[tuple[str, str]] = set()
        for key, sources in agreement_map.items():
            if len(sources) >= 2:
                validated_keys.add(key)
                logger.info(
                    f"EmailSignalAgent: CROSS-VALIDATED {key[0]} {key[1]} "
                    f"({len(sources)} sources: {', '.join(sorted(sources))})"
                )

        # Apply boosts
        for sig in signals:
            for sym in sig.symbols:
                if (sym, sig.direction) in validated_keys:
                    sig.cross_validated = True
                    sig.confidence = min(1.0, sig.confidence + CROSS_VALIDATION_CONFIDENCE_BOOST)
                    sig.score_bonus = min(
                        MAX_EMAIL_BONUS_PER_SYMBOL,
                        sig.score_bonus + CROSS_VALIDATION_BONUS,
                    )

        return signals

    # ── Cache Management ──────────────────────────────────────

    def _update_signal_cache(self, signals: list[EmailSignalEvent]) -> None:
        """Update the in-memory signal cache. Old entries are evicted by TTL."""
        now = time.time()

        # Evict stale entries
        for sym in list(self._signal_cache.keys()):
            self._signal_cache[sym] = [
                s for s in self._signal_cache[sym]
                if (now - s.timestamp.timestamp()) < SIGNAL_CACHE_TTL
            ]
            if not self._signal_cache[sym]:
                del self._signal_cache[sym]

        # Add new signals
        for sig in signals:
            for sym in sig.symbols:
                if sym not in self._signal_cache:
                    self._signal_cache[sym] = []
                self._signal_cache[sym].append(sig)

        self._cache_updated_at = now

    def _update_regime_and_fragility(self, signals: list[EmailSignalEvent]) -> None:
        """Derive regime adjustment and fragility flag from the latest signals."""
        regime_signals = [s for s in signals if s.signal_type in ("regime_call", "macro_regime")]
        risk_signals = [s for s in signals if s.signal_type == "risk_event"]

        # Regime: average bullish (+1) / bearish (-1) direction
        if regime_signals:
            directions = []
            for s in regime_signals:
                if s.direction == "bullish":
                    directions.append(1.0 * s.confidence)
                elif s.direction == "bearish":
                    directions.append(-1.0 * s.confidence)
            self._regime_adjustment = sum(directions) / len(directions) if directions else 0.0
        else:
            self._regime_adjustment = 0.0

        # Fragility: any high-severity risk events
        self._fragility_flag = any(
            s.details.get("severity") == "high" for s in risk_signals
        )

    # ── Orchestrator Lookup API ───────────────────────────────

    def get_email_bonus(self, symbol: str) -> float:
        """Return the total email-derived score bonus for a symbol.

        Capped at MAX_EMAIL_BONUS_PER_SYMBOL (15 points).
        Called by scoring.py or the orchestrator.
        """
        base = symbol.replace("-USD", "").replace("/USD", "").upper()
        cached = self._signal_cache.get(base, [])
        if not cached:
            return 0.0

        total = sum(s.score_bonus for s in cached)
        return min(float(MAX_EMAIL_BONUS_PER_SYMBOL), total)

    def get_regime_adjustment(self) -> float:
        """Return a position-sizing multiplier derived from email regime signals.

        Returns 0.5 (max bearish) to 1.3 (max bullish), default 1.0 (neutral).
        Used as: position_size *= get_regime_adjustment()
        """
        # _regime_adjustment ranges from -1.0 (bearish) to +1.0 (bullish)
        # Convert to multiplier: -1.0 -> 0.5, 0.0 -> 1.0, +1.0 -> 1.3
        raw = self._regime_adjustment
        if raw >= 0:
            return 1.0 + raw * 0.3   # 1.0 to 1.3
        else:
            return 1.0 + raw * 0.5   # 1.0 to 0.5

    def get_fragility_flag(self) -> bool:
        """Return True if recent emails contain high-severity risk events."""
        return self._fragility_flag

    def get_status(self) -> dict:
        """Return agent status for dashboards."""
        return {
            "cached_symbols": len(self._signal_cache),
            "total_cached_signals": sum(
                len(v) for v in self._signal_cache.values()
            ),
            "cache_age_min": (
                (time.time() - self._cache_updated_at) / 60
                if self._cache_updated_at > 0
                else None
            ),
            "regime_adjustment": self._regime_adjustment,
            "fragility_flag": self._fragility_flag,
            "schedule_est": self.scan_hours_est,
            "sources": list(EMAIL_SOURCES.keys()),
        }

    # ── Gmail Labeling ──────────────────────────────────────────

    CRYPTO_SIGNAL_LABEL_ID = "Label_8"  # "crypto-signal" label created in Gmail

    async def _label_email(self, message_id: str) -> None:
        """Label a processed email as 'crypto-signal' and mark as read.

        Uses the gmail-bridge.ts CLI directly (simpler than MCP for label ops).
        """
        try:
            bridge = os.path.join(
                os.path.dirname(self.gmail_mcp_server_path),
                "..", "scripts", "gmail-bridge.ts"
            )
            proc = await asyncio.create_subprocess_exec(
                "npx", "tsx", bridge, "label", message_id, self.CRYPTO_SIGNAL_LABEL_ID,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.dirname(os.path.dirname(self.gmail_mcp_server_path)),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.debug(f"Labeled + marked read: {message_id}")
            else:
                logger.warning(f"Failed to label {message_id}: {stderr.decode()[:200]}")
        except Exception as e:
            logger.warning(f"Label error for {message_id}: {e}")

    # ── Gmail MCP Integration ─────────────────────────────────

    async def _gmail_mcp_call(self, tool_name: str, arguments: dict) -> list | dict | None:
        """Call a Gmail MCP tool via stdio subprocess.

        Starts a fresh MCP subprocess connection per call.
        Timeout: GMAIL_CALL_TIMEOUT seconds.
        """
        try:
            from mcp.client.stdio import stdio_client, StdioServerParameters
            from mcp import ClientSession
        except ImportError:
            logger.error("EmailSignalAgent: mcp package not installed")
            return None

        server_params = StdioServerParameters(
            command=GMAIL_MCP_COMMAND,
            args=self.gmail_mcp_args,
        )

        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(
                        session.initialize(),
                        timeout=GMAIL_CALL_TIMEOUT,
                    )
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments=arguments),
                        timeout=GMAIL_CALL_TIMEOUT,
                    )
                    return self._parse_mcp_result_raw(result)
        except asyncio.TimeoutError:
            logger.warning(
                f"EmailSignalAgent: Gmail MCP timeout ({tool_name}, {GMAIL_CALL_TIMEOUT}s)"
            )
        except Exception as e:
            logger.error(f"EmailSignalAgent: Gmail MCP error ({tool_name}): {e}")
        return None

    @staticmethod
    def _parse_mcp_result_raw(result) -> list | dict | None:
        """Parse raw MCP result into Python data."""
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if not text:
                continue
            try:
                data = json.loads(text)
                return data
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _parse_mcp_result(data) -> list[dict]:
        """Normalize MCP search results into a list of email summary dicts."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Handle paginated or wrapped results
            if "messages" in data:
                return data["messages"]
            if "results" in data:
                return data["results"]
            return [data]
        return []

    @staticmethod
    def _extract_body_text(email_data) -> str:
        """Extract plain text body from a full email MCP response."""
        if not email_data:
            return ""

        if isinstance(email_data, str):
            return strip_html(email_data)

        if isinstance(email_data, dict):
            # Try common body field paths
            body = (
                email_data.get("body")
                or email_data.get("text")
                or email_data.get("textBody")
                or email_data.get("text_body")
                or ""
            )
            if body:
                return strip_html(body) if "<" in body else body

            # Try HTML body
            html_body = (
                email_data.get("htmlBody")
                or email_data.get("html_body")
                or email_data.get("html")
                or ""
            )
            if html_body:
                return strip_html(html_body)

            # Try payload structure (Gmail API format)
            payload = email_data.get("payload", {})
            if isinstance(payload, dict):
                parts = payload.get("parts", [])
                for part in parts:
                    mime = part.get("mimeType", "")
                    part_body = part.get("body", {}).get("data", "")
                    if mime == "text/plain" and part_body:
                        import base64
                        try:
                            decoded = base64.urlsafe_b64decode(part_body + "==").decode("utf-8", errors="replace")
                            return decoded
                        except Exception:
                            pass
                    elif mime == "text/html" and part_body:
                        import base64
                        try:
                            decoded = base64.urlsafe_b64decode(part_body + "==").decode("utf-8", errors="replace")
                            return strip_html(decoded)
                        except Exception:
                            pass

                # Single-part message
                body_data = payload.get("body", {}).get("data", "")
                if body_data:
                    import base64
                    try:
                        decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
                        return strip_html(decoded) if "<" in decoded else decoded
                    except Exception:
                        pass

            # Fallback: stringify the whole thing
            snippet = email_data.get("snippet", "")
            if snippet:
                return snippet

        return ""

    # ── Ollama Extraction ─────────────────────────────────────

    async def _extract_signals_ollama(self, prompt: str) -> list[dict]:
        """Send extraction prompt to Ollama and parse the JSON response.

        Uses Qwen3 with thinking enabled for better extraction quality.
        Falls back gracefully on timeout or parse failure.
        """
        try:
            async with httpx.AsyncClient(timeout=OLLAMA_CALL_TIMEOUT) as client:
                r = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={
                        "model": self.extract_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.1,
                            "num_predict": 2000,
                        },
                    },
                )
                if r.status_code != 200:
                    logger.warning(
                        f"EmailSignalAgent: Ollama returned {r.status_code}"
                    )
                    return []

                raw_response = r.json().get("response", "")
                if not raw_response:
                    return []

                parsed = parse_llm_response(raw_response)
                return parsed

        except httpx.TimeoutException:
            logger.warning(
                f"EmailSignalAgent: Ollama timeout ({OLLAMA_CALL_TIMEOUT}s)"
            )
        except Exception as e:
            logger.error(f"EmailSignalAgent: Ollama error: {e}")
        return []
