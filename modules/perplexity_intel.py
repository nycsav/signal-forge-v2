"""
Signal Forge V2 — Perplexity Sonar Intelligence Layer

Real-time market intelligence via Perplexity's Sonar API.
Replaces stale data sources with live web-grounded analysis.

Three functions:
  1. get_crypto_sentiment(symbol)  — per-scan sentiment for BTC/ETH/SOL
  2. enrich_whale_context(event)   — context for whale trigger events
  3. get_regulatory_alerts()       — stablecoin/crypto regulatory monitoring

Uses OpenAI-compatible endpoint with structured JSON output.
Cost: ~$2.40/day at 10-min scan intervals.
"""

import json
import os
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

PPLX_API_KEY = os.getenv("PPLX_API_KEY", "")
PPLX_BASE = "https://api.perplexity.ai"
MODEL = "sonar"


def _call_sonar(
    prompt: str,
    system: str = "You are a crypto market analyst. Be concise and data-driven.",
    json_schema: dict = None,
    recency: str = "hour",
    domains: list[str] = None,
    model: str = MODEL,
) -> dict:
    """Call Perplexity Sonar API with structured output."""
    if not PPLX_API_KEY:
        return {"error": "PPLX_API_KEY not set"}

    headers = {
        "Authorization": f"Bearer {PPLX_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }

    if json_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema},
        }

    # Search filters
    extra = {}
    if recency:
        extra["search_recency_filter"] = recency
    if domains:
        extra["search_domain_filter"] = domains
    body.update(extra)

    try:
        resp = httpx.post(
            f"{PPLX_BASE}/chat/completions",
            headers=headers,
            json=body,
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(f"Sonar API error: {resp.status_code} {resp.text[:200]}")
            return {"error": f"HTTP {resp.status_code}"}

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])

        # Parse JSON if schema was requested
        if json_schema:
            try:
                parsed = json.loads(content)
                parsed["_citations"] = citations
                return parsed
            except json.JSONDecodeError:
                return {"raw": content, "_citations": citations}

        return {"content": content, "_citations": citations}

    except Exception as e:
        logger.warning(f"Sonar call failed: {e}")
        return {"error": str(e)}


# ── Per-Scan Crypto Sentiment ─────────────────────────────────

SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment_score": {"type": "number", "description": "-100 bearish to +100 bullish"},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "number", "description": "0-100"},
        "key_catalysts": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "price_target_24h": {"type": "string"},
    },
    "required": ["sentiment_score", "direction", "confidence", "key_catalysts"],
}


def get_crypto_sentiment(symbol: str) -> dict:
    """
    Real-time sentiment for a crypto asset.
    Called once per scan cycle (every 10 min).
    """
    clean_sym = symbol.replace("-USD", "").replace("/USD", "")

    result = _call_sonar(
        prompt=f"What is the current market sentiment for {clean_sym} cryptocurrency right now? "
               f"Consider: price action in the last hour, any breaking news, whale activity, "
               f"exchange flows, social media sentiment, and macro factors. "
               f"Give a sentiment score from -100 (extremely bearish) to +100 (extremely bullish).",
        json_schema=SENTIMENT_SCHEMA,
        recency="hour",
        domains=["coindesk.com", "theblock.co", "decrypt.co", "cointelegraph.com", "reuters.com"],
    )

    if "error" not in result:
        logger.info(
            f"PPLX SENTIMENT: {symbol} → {result.get('direction', '?')} "
            f"(score={result.get('sentiment_score', 0)}, conf={result.get('confidence', 0)})"
        )

    return result


# ── Whale Context Enrichment ──────────────────────────────────

WHALE_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_type": {"type": "string", "description": "fund, exchange, whale, unknown"},
        "likely_intent": {"type": "string", "description": "accumulation, distribution, rebalancing, unknown"},
        "market_impact": {"type": "string", "enum": ["high", "medium", "low"]},
        "related_news": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["entity_type", "likely_intent", "market_impact"],
}


def enrich_whale_context(event: dict) -> dict:
    """
    Add context to a whale trigger event using Perplexity.
    Called on each whale signal (max ~10/day).
    """
    entity = event.get("entity", "unknown")
    amount = event.get("amount_usd", 0)
    direction = event.get("direction", "unknown")
    asset = event.get("asset", event.get("symbol", "crypto"))

    result = _call_sonar(
        prompt=f"A whale ({entity}) just moved ${amount:,.0f} worth of {asset} "
               f"(direction: {direction}). What is the likely intent behind this move? "
               f"Is this entity known? What's the probable market impact?",
        json_schema=WHALE_SCHEMA,
        recency="day",
        domains=["arkham.intel", "coindesk.com", "theblock.co", "whale-alert.io"],
    )

    if "error" not in result:
        logger.info(
            f"PPLX WHALE: {asset} {direction} ${amount:,.0f} → "
            f"{result.get('likely_intent', '?')} (impact={result.get('market_impact', '?')})"
        )

    return result


# ── Regulatory / Stablecoin Monitoring ────────────────────────

REGULATORY_SCHEMA = {
    "type": "object",
    "properties": {
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "impact": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "affected_assets": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "overall_risk": {"type": "string", "enum": ["elevated", "normal", "low"]},
    },
    "required": ["alerts", "overall_risk"],
}


def get_regulatory_alerts() -> dict:
    """
    Check for crypto regulatory developments.
    Called every 4 hours.
    """
    result = _call_sonar(
        prompt="What are the latest cryptocurrency regulatory developments in the US and globally? "
               "Focus on: SEC actions, stablecoin regulations, exchange enforcement, "
               "new legislation, and any emergency orders. Only include items from the last 24 hours.",
        json_schema=REGULATORY_SCHEMA,
        recency="day",
        domains=["sec.gov", "reuters.com", "bloomberg.com", "theblock.co", "coindesk.com"],
    )

    if "error" not in result:
        alerts = result.get("alerts", [])
        risk = result.get("overall_risk", "normal")
        logger.info(f"PPLX REGULATORY: {len(alerts)} alerts, risk={risk}")

    return result


# ── Earnings Check (for Enso cross-use) ───────────────────────

EARNINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "reported": {"type": "boolean"},
        "eps_actual": {"type": "number"},
        "eps_estimate": {"type": "number"},
        "revenue_actual": {"type": "number"},
        "revenue_estimate": {"type": "number"},
        "beat": {"type": "boolean"},
        "guidance": {"type": "string", "enum": ["raised", "maintained", "lowered", "none"]},
        "key_highlights": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["symbol", "reported", "beat"],
}


def check_earnings(symbol: str) -> dict:
    """
    Check if a stock reported earnings recently and whether it beat/missed.
    Prevents the RTX error. Uses SEC mode.
    """
    result = _call_sonar(
        prompt=f"Did {symbol} report earnings in the last 48 hours? "
               f"If yes, what were the actual EPS and revenue vs estimates? "
               f"Did they beat or miss? Did they raise, maintain, or lower guidance?",
        system="You are a financial analyst. Return precise numbers from the earnings report.",
        json_schema=EARNINGS_SCHEMA,
        recency="week",
        domains=["sec.gov", "reuters.com", "bloomberg.com", "seekingalpha.com"],
    )

    if "error" not in result and result.get("reported"):
        logger.info(
            f"PPLX EARNINGS: {symbol} {'BEAT' if result.get('beat') else 'MISS'} "
            f"EPS={result.get('eps_actual')} vs {result.get('eps_estimate')} "
            f"guidance={result.get('guidance', '?')}"
        )

    return result


# ── CLI Test ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    import modules.perplexity_intel as _self
    _self.PPLX_API_KEY = os.getenv("PPLX_API_KEY", "")

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "sentiment":
            sym = sys.argv[2] if len(sys.argv) > 2 else "BTC"
            r = get_crypto_sentiment(sym)
            print(json.dumps(r, indent=2))
        elif cmd == "regulatory":
            r = get_regulatory_alerts()
            print(json.dumps(r, indent=2))
        elif cmd == "earnings":
            sym = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
            r = check_earnings(sym)
            print(json.dumps(r, indent=2))
    else:
        print("Usage: python perplexity_intel.py sentiment BTC")
        print("       python perplexity_intel.py regulatory")
        print("       python perplexity_intel.py earnings AAPL")
