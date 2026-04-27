"""Signal Forge v2 — Email Source Definitions and LLM Extraction Prompts

Defines the 6 email newsletter sources, their Gmail search queries,
signal types, Qwen3 extraction prompts, and score bonus mappings.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from io import StringIO
from typing import Any


# ── HTML Stripping ────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    """Simple HTML tag stripper that preserves text content."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def strip_html(html: str) -> str:
    """Strip HTML tags from a string, returning plain text.

    Handles malformed HTML gracefully. Returns the original string
    if parsing fails.
    """
    if not html:
        return ""
    try:
        stripper = _HTMLStripper()
        stripper.feed(html)
        text = stripper.get_text()
        # Collapse whitespace runs
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        # Fallback: regex-based strip
        return re.sub(r"<[^>]+>", " ", html).strip()


# ── LLM Response Parsing ─────────────────────────────────────

def parse_llm_response(raw: str) -> list[dict]:
    """Extract a JSON array of signal dicts from an LLM response.

    Handles:
    - Markdown code blocks (```json ... ```)
    - Bare JSON arrays
    - JSON arrays embedded in prose
    - Qwen3 <think>...</think> tags
    - Partial/trailing JSON with recovery
    - Single JSON object (wrapped into a list)

    Returns an empty list on failure (never raises).
    """
    if not raw or not raw.strip():
        return []

    # Strip Qwen3 thinking tags
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

    # Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"```\s*", "", cleaned)

    # Strategy 1: Find a JSON array
    array_matches = re.findall(r"\[[\s\S]*?\]", cleaned)
    for match in array_matches:
        try:
            parsed = json.loads(match)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            # Try fixing trailing comma
            fixed = re.sub(r",\s*\]", "]", match)
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
            except json.JSONDecodeError:
                continue

    # Strategy 2: Find individual JSON objects and collect them
    obj_matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned)
    results: list[dict] = []
    for match in obj_matches:
        try:
            parsed = json.loads(match)
            if isinstance(parsed, dict) and ("signal_type" in parsed or "symbols" in parsed):
                results.append(parsed)
        except json.JSONDecodeError:
            continue
    if results:
        return results

    # Strategy 3: Try parsing the entire cleaned text as JSON
    try:
        parsed = json.loads(cleaned.strip())
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    return []


# ── Score Bonus Constants ─────────────────────────────────────

MAX_EMAIL_BONUS_PER_SYMBOL = 15
CROSS_VALIDATION_BONUS = 3
CROSS_VALIDATION_CONFIDENCE_BOOST = 0.15


# ── Source Definitions ────────────────────────────────────────

EMAIL_SOURCES: dict[str, dict[str, Any]] = {

    # ── 1. altFINS ────────────────────────────────────────────
    "altfins": {
        "gmail_query": "from:altfins.com OR from:noreply@altfins.com newer_than:10h",
        "signal_types": ["pattern_breakout", "smart_money_flow", "ta_summary"],
        "score_bonus_map": {
            "pattern_breakout": 8,   # if confidence >= 0.6
            "smart_money_flow": 6,   # if flow > $5M
            "ta_summary": 2,
        },
        "extract_prompt": """You are a crypto signal extraction engine. Analyze this altFINS newsletter email and extract all trading signals.

EMAIL CONTENT:
{body}

Extract every signal into a JSON array. For each signal include:
- "signal_type": one of "pattern_breakout", "smart_money_flow", "ta_summary"
- "symbols": list of crypto ticker symbols mentioned (e.g. ["BTC", "ETH"])
- "direction": "bullish" or "bearish" or "neutral"
- "confidence": 0.0 to 1.0 based on the signal strength described
- "details": object with relevant fields:
  - For pattern_breakout: {{"pattern_name": "...", "success_rate": 0.0-1.0, "target_price": number, "timeframe": "..."}}
  - For smart_money_flow: {{"flow_direction": "inflow"/"outflow", "amount_usd": number, "entity": "..."}}
  - For ta_summary: {{"indicators": "...", "trend": "..."}}

Rules:
- Only extract signals with specific actionable information
- Set confidence based on success_rate if available (success_rate > 67% = confidence 0.7+)
- For smart_money_flow, include dollar amounts when mentioned
- Return an empty array [] if no actionable signals found

Return ONLY a valid JSON array, no other text.""",
    },

    # ── 2. Coinbase Research ──────────────────────────────────
    "coinbase_research": {
        "gmail_query": "from:coinbase.com (research OR institutional OR weekly OR market) newer_than:10h",
        "signal_types": ["regime_call", "etf_flow", "institutional_purchase", "risk_event"],
        "score_bonus_map": {
            "regime_call": 10,            # if confidence >= 0.7 and direction is bullish
            "etf_flow": 5,                # if direction is inflow
            "institutional_purchase": 4,
            "risk_event": 0,
        },
        "extract_prompt": """You are a crypto signal extraction engine. Analyze this Coinbase research/institutional email and extract trading signals.

EMAIL CONTENT:
{body}

Extract every signal into a JSON array. For each signal include:
- "signal_type": one of "regime_call", "etf_flow", "institutional_purchase", "risk_event"
- "symbols": list of crypto ticker symbols (e.g. ["BTC", "ETH"])
- "direction": "bullish" or "bearish" or "neutral"
- "confidence": 0.0 to 1.0
- "details": object with relevant fields:
  - For regime_call: {{"regime": "risk-on"/"risk-off"/"neutral", "reasoning": "..."}}
  - For etf_flow: {{"flow_direction": "inflow"/"outflow", "amount_usd": number, "fund": "..."}}
  - For institutional_purchase: {{"entity": "...", "amount_usd": number, "asset": "..."}}
  - For risk_event: {{"event": "...", "severity": "high"/"medium"/"low"}}

Rules:
- regime_call: extract the overall market regime assessment with high confidence only if explicitly stated
- etf_flow: look for Bitcoin/Ethereum ETF flow data with direction and amounts
- institutional_purchase: entity name, amount, and which asset
- risk_event: regulatory actions, exchange issues, major vulnerabilities
- Return an empty array [] if no actionable signals found

Return ONLY a valid JSON array, no other text.""",
    },

    # ── 3. CoinMarketCap (CMC) ────────────────────────────────
    "cmc": {
        "gmail_query": "from:coinmarketcap.com newer_than:10h",
        "signal_types": ["fg_extreme", "funding_negative_extended", "liquidation_spike",
                         "institutional_flow", "key_price_level"],
        "score_bonus_map": {
            "fg_extreme": 4,                    # F&G < 20, contrarian buy
            "funding_negative_extended": 6,     # >30 consecutive days negative
            "liquidation_spike": 3,
            "institutional_flow": 4,
            "key_price_level": 2,
        },
        "extract_prompt": """You are a crypto signal extraction engine. Analyze this CoinMarketCap newsletter and extract market signals.

EMAIL CONTENT:
{body}

Extract every signal into a JSON array. For each signal include:
- "signal_type": one of "fg_extreme", "funding_negative_extended", "liquidation_spike", "institutional_flow", "key_price_level"
- "symbols": list of crypto ticker symbols (e.g. ["BTC"])
- "direction": "bullish" or "bearish" or "neutral"
- "confidence": 0.0 to 1.0
- "details": object with relevant fields:
  - For fg_extreme: {{"fear_greed_value": number, "classification": "extreme fear"/"extreme greed"/...}}
  - For funding_negative_extended: {{"direction": "negative"/"positive", "consecutive_days": number}}
  - For liquidation_spike: {{"total_usd": number, "direction": "long"/"short", "timeframe": "..."}}
  - For institutional_flow: {{"entity": "...", "flow_direction": "inflow"/"outflow", "amount_usd": number}}
  - For key_price_level: {{"level_type": "support"/"resistance", "price": number, "significance": "..."}}

Rules:
- fg_extreme: only if Fear & Greed index < 20 (extreme fear, contrarian bullish) or > 80 (extreme greed, bearish warning)
- funding_negative_extended: only if funding rates are negative for >30 consecutive days (bullish signal)
- liquidation_spike: significant liquidation events (>$100M)
- Return an empty array [] if no actionable signals found

Return ONLY a valid JSON array, no other text.""",
    },

    # ── 4. CoinGecko ──────────────────────────────────────────
    "coingecko": {
        "gmail_query": "from:coingecko.com newer_than:10h",
        "signal_types": ["trending_token"],
        "score_bonus_map": {
            "trending_token": 5,  # if 2+ appearances
        },
        "extract_prompt": """You are a crypto signal extraction engine. Analyze this CoinGecko newsletter and extract trending token signals.

EMAIL CONTENT:
{body}

Extract every trending token signal into a JSON array. For each signal include:
- "signal_type": "trending_token"
- "symbols": list with the token's ticker symbol (e.g. ["SOL"])
- "direction": "bullish" (trending = momentum) or "neutral"
- "confidence": 0.0 to 1.0 (higher if token appeared multiple times or has strong price change)
- "details": object with:
  - "price_change_24h_pct": number (percentage price change)
  - "price_change_7d_pct": number if available
  - "appearances": number of times this token appears in the trending list
  - "rank": position in trending list
  - "category": token category if mentioned (e.g. "DeFi", "L1", "Meme")

Rules:
- Track how many times each symbol appears across the email; set appearances count
- Tokens with 2+ appearances get higher confidence (0.7+)
- Include price change percentages when mentioned
- Return an empty array [] if no trending tokens found

Return ONLY a valid JSON array, no other text.""",
    },

    # ── 5. Stocktwits ─────────────────────────────────────────
    "stocktwits": {
        "gmail_query": "from:stocktwits.com newer_than:10h",
        "signal_types": ["macro_regime"],
        "score_bonus_map": {
            "macro_regime": 4,  # if risk-on
        },
        "extract_prompt": """You are a crypto/macro signal extraction engine. Analyze this Stocktwits newsletter and extract macro regime signals.

EMAIL CONTENT:
{body}

Extract macro regime signals into a JSON array. For each signal include:
- "signal_type": "macro_regime"
- "symbols": ["MACRO"] (use MACRO for market-wide signals, or specific tickers if mentioned)
- "direction": "bullish" (risk-on) or "bearish" (risk-off) or "neutral"
- "confidence": 0.0 to 1.0
- "details": object with:
  - "regime": "risk-on" or "risk-off" or "neutral"
  - "oil_price_direction": "up"/"down"/"stable" if mentioned
  - "equity_direction": "up"/"down"/"mixed" if mentioned
  - "key_driver": brief description of what is driving the regime
  - "vix_level": number if mentioned

Rules:
- Look for overall risk appetite signals from equities, commodities, macro data
- Oil prices rising + equities falling = risk-off
- Equities rising + low VIX = risk-on
- Return an empty array [] if no macro signals found

Return ONLY a valid JSON array, no other text.""",
    },

    # ── 6. Cheap Investor ─────────────────────────────────────
    "cheap_investor": {
        "gmail_query": "from:cheapinvestor OR from:cheap-investor newer_than:10h",
        "signal_types": ["whale_accumulation", "retail_divergence"],
        "score_bonus_map": {
            "whale_accumulation": 6,
            "retail_divergence": 3,
        },
        "extract_prompt": """You are a crypto signal extraction engine. Analyze this Cheap Investor newsletter and extract whale vs retail divergence signals.

EMAIL CONTENT:
{body}

Extract every signal into a JSON array. For each signal include:
- "signal_type": one of "whale_accumulation", "retail_divergence"
- "symbols": list of crypto ticker symbols (e.g. ["BTC", "ETH"])
- "direction": "bullish" or "bearish" or "neutral"
- "confidence": 0.0 to 1.0
- "details": object with:
  - For whale_accumulation: {{"whale_action": "accumulating"/"distributing", "retail_action": "selling"/"buying", "divergence": true/false, "description": "..."}}
  - For retail_divergence: {{"whale_sentiment": "bullish"/"bearish", "retail_sentiment": "bullish"/"bearish", "divergence_strength": "strong"/"moderate"/"weak"}}

Rules:
- whale_accumulation: whales buying while retail sells = strong bullish signal
- whale_accumulation: whales selling while retail buys = strong bearish signal
- retail_divergence: when whale and retail sentiment diverge, follow the whales
- Set confidence higher (0.7+) when there is clear divergence with data backing it
- Return an empty array [] if no divergence signals found

Return ONLY a valid JSON array, no other text.""",
    },
}
