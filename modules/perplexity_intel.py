"""
Signal Forge V2 + Enso Trading — Perplexity Sonar Intelligence Layer

Real-time market intelligence via Perplexity's Sonar API.
Consensus design from Perplexity (CTO) + Claude Code (Engineer) + User.

Features:
  1. Adaptive polling: 30 min calm, 10 min normal, 2 min spike
  2. Multi-factor JSON: sentiment + catalysts + earnings + edge_score
  3. Hard gate: block trades when conf <0.6 AND edge opposes thesis
  4. Freshness validation: reject if data >1 hour old
  5. NYSE support: 6 AM - 8 PM ET weekdays for options
  6. Cost-optimized: ~$130/month for both engines

Uses OpenAI-compatible endpoint with structured JSON output.
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

PPLX_BASE = "https://api.perplexity.ai"
MODEL = "sonar"
ET = ZoneInfo("America/New_York")

# Adaptive polling state
_last_call_time: dict[str, float] = {}
_vol_cache: dict[str, float] = {}


def _get_key() -> str:
    """Lazy-load PPLX key — ensures .env is loaded first."""
    return os.getenv("PPLX_API_KEY", "")


def _call_sonar(
    prompt: str,
    system: str = "You are a financial market analyst. Be concise, data-driven, cite sources.",
    json_schema: dict = None,
    recency: str = "hour",
    domains: list[str] = None,
    model: str = MODEL,
    timeout: int = 15,
) -> dict:
    """Call Perplexity Sonar API with structured output. Non-blocking timeout."""
    api_key = _get_key()
    if not api_key:
        return {"error": "PPLX_API_KEY not set"}

    headers = {
        "Authorization": f"Bearer {api_key}",
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

    if recency:
        body["search_recency_filter"] = recency
    if domains:
        body["search_domain_filter"] = domains

    try:
        resp = httpx.post(
            f"{PPLX_BASE}/chat/completions",
            headers=headers,
            json=body,
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Sonar API error: {resp.status_code} {resp.text[:200]}")
            return {"error": f"HTTP {resp.status_code}"}

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])

        if json_schema:
            try:
                parsed = json.loads(content)
                parsed["_citations"] = citations
                parsed["_timestamp"] = datetime.now().isoformat()
                return parsed
            except json.JSONDecodeError:
                return {"raw": content, "_citations": citations}

        return {"content": content, "_citations": citations}

    except httpx.TimeoutException:
        logger.warning(f"Sonar timeout ({timeout}s) — skipping this cycle")
        return {"error": "timeout"}
    except Exception as e:
        logger.warning(f"Sonar call failed: {e}")
        return {"error": str(e)}


# ── Adaptive Polling ──────────────────────────────────────────

def get_adaptive_interval(symbol: str, current_vol: float = 0, avg_vol: float = 1, price_change_pct: float = 0) -> int:
    """
    Adaptive polling frequency based on volatility.
    Returns interval in seconds.
    Calm: 1800s (30 min), Normal: 600s (10 min), Spike: 120s (2 min)
    """
    if avg_vol <= 0:
        return 600

    ratio = current_vol / avg_vol

    if ratio > 3 or abs(price_change_pct) > 2.0:
        return 120   # 2 min — high volatility
    elif ratio > 1.5:
        return 600   # 10 min — normal
    else:
        return 1800  # 30 min — calm

    return 600


def should_call_sonar(symbol: str, interval: int = 600) -> bool:
    """Check if enough time has passed since last Sonar call for this symbol."""
    last = _last_call_time.get(symbol, 0)
    if time.time() - last >= interval:
        return True
    return False


def mark_called(symbol: str):
    """Record that we just called Sonar for this symbol."""
    _last_call_time[symbol] = time.time()


# ── Multi-Factor Intelligence (upgraded prompt) ──────────────

MULTI_FACTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "description": "-100 to +100"},
                "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                "confidence": {"type": "number", "description": "0 to 1"},
            },
        },
        "edge_score": {"type": "number", "description": "-1 to +1 trade bias"},
        "catalysts": {
            "type": "object",
            "properties": {
                "earnings": {"type": "string"},
                "regulatory": {"type": "string"},
                "macro": {"type": "string"},
            },
        },
        "news_summary": {"type": "string", "description": "Top 3 headlines + impact"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentiment", "edge_score"],
}


def get_market_intel(symbol: str, asset_type: str = "crypto") -> dict:
    """
    Multi-factor intelligence for any asset (crypto or stock).
    Returns sentiment + catalysts + edge_score in one call.
    This is the primary Sonar function — replaces get_crypto_sentiment.
    """
    clean_sym = symbol.replace("-USD", "").replace("/USD", "")

    if asset_type == "crypto":
        prompt = (
            f"Analyze {clean_sym} cryptocurrency RIGHT NOW. Return:\n"
            f"1. Sentiment score (-100 bearish to +100 bullish) with confidence (0-1)\n"
            f"2. Edge score (-1 to +1): your trade recommendation bias\n"
            f"3. Top catalysts: any earnings/regulatory/macro factors\n"
            f"4. News summary: top 3 headlines from the last hour with impact (H/M/L)\n"
            f"5. Risk flags: any concerns (high_vol, stale_data, conflicting_signals)\n"
            f"Sources: real-time only. Timeframe: last 1 hour."
        )
        domains = ["coindesk.com", "theblock.co", "decrypt.co", "cointelegraph.com", "reuters.com"]
    else:
        prompt = (
            f"Analyze {clean_sym} stock/ETF RIGHT NOW for options trading. Return:\n"
            f"1. Sentiment score (-100 bearish to +100 bullish) with confidence (0-1)\n"
            f"2. Edge score (-1 to +1): directional trade recommendation\n"
            f"3. Catalysts: recent earnings (beat/miss/guidance), regulatory news, macro impact\n"
            f"4. News summary: top 3 headlines with market impact (H/M/L)\n"
            f"5. Risk flags: earnings_imminent, high_iv, stale_data, conflicting_signals\n"
            f"Sources: real-time only. Timeframe: last 1 hour."
        )
        domains = ["reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "seekingalpha.com"]

    mark_called(symbol)

    result = _call_sonar(
        prompt=prompt,
        json_schema=MULTI_FACTOR_SCHEMA,
        recency="hour",
        domains=domains,
        timeout=15,
    )

    if "error" not in result:
        sent = result.get("sentiment", {})
        edge = result.get("edge_score", 0)
        logger.info(
            f"PPLX INTEL: {symbol} → {sent.get('direction', '?')} "
            f"(score={sent.get('score', 0)}, conf={sent.get('confidence', 0):.2f}, "
            f"edge={edge:+.2f})"
        )

    return result


# ── Hard Gate Logic ───────────────────────────────────────────

def should_block_trade(intel: dict, technical_direction: str) -> tuple[bool, str]:
    """
    Hard gate: block trade if Sonar confidence <0.6 AND edge opposes thesis.

    Args:
        intel: output from get_market_intel()
        technical_direction: "long" or "short" from our TA

    Returns:
        (should_block, reason)
    """
    if "error" in intel:
        return False, "sonar_unavailable"

    sent = intel.get("sentiment", {})
    confidence = sent.get("confidence", 0)
    edge = intel.get("edge_score", 0)

    # Hard gate: low confidence AND edge opposes direction
    if confidence < 0.6:
        if technical_direction == "long" and edge < -0.3:
            return True, f"SONAR GATE: conf={confidence:.2f} + edge={edge:+.2f} opposes LONG"
        elif technical_direction == "short" and edge > 0.3:
            return True, f"SONAR GATE: conf={confidence:.2f} + edge={edge:+.2f} opposes SHORT"

    return False, ""


def compute_sonar_bonus(intel: dict) -> float:
    """
    Soft signal: compute score adjustment from Sonar intel.
    Weighted: sentiment 40%, edge 60%. Max ±5 pts.
    """
    if "error" in intel:
        return 0

    sent = intel.get("sentiment", {})
    score = sent.get("score", 0)  # -100 to +100
    confidence = sent.get("confidence", 0)
    edge = intel.get("edge_score", 0)  # -1 to +1

    if confidence < 0.6:
        return 0  # low confidence = no bonus

    # Blend: 40% sentiment + 60% edge
    sentiment_contrib = (score / 100) * 2.0  # ±2 pts
    edge_contrib = edge * 3.0  # ±3 pts
    bonus = sentiment_contrib + edge_contrib

    return round(max(-5, min(5, bonus)), 1)


# ── Freshness Validation ──────────────────────────────────────

def is_fresh(intel: dict, max_age_minutes: int = 60) -> bool:
    """Check if Sonar data is fresh enough to use."""
    ts = intel.get("_timestamp")
    if not ts:
        return True  # no timestamp = assume fresh (first call)
    try:
        call_time = datetime.fromisoformat(ts)
        age = (datetime.now() - call_time).total_seconds() / 60
        return age <= max_age_minutes
    except Exception:
        return True


# ── NYSE Market Hours Check ───────────────────────────────────

def is_nyse_active_hours() -> bool:
    """Check if within NYSE extended hours (6 AM - 8 PM ET weekdays)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return 6 <= now.hour < 20


# ── Earnings Check (shared by both engines) ───────────────────

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
    """Check if a stock reported earnings recently. Prevents RTX-type errors."""
    result = _call_sonar(
        prompt=f"Did {symbol} report earnings in the last 48 hours? "
               f"If yes, what were the actual EPS and revenue vs estimates? "
               f"Did they beat or miss? Did they raise, maintain, or lower guidance?",
        system="You are a financial analyst. Return precise numbers from the earnings report.",
        json_schema=EARNINGS_SCHEMA,
        recency="week",
        domains=["sec.gov", "reuters.com", "bloomberg.com", "seekingalpha.com"],
        timeout=15,
    )

    if "error" not in result and result.get("reported"):
        logger.info(
            f"PPLX EARNINGS: {symbol} {'BEAT' if result.get('beat') else 'MISS'} "
            f"EPS={result.get('eps_actual')} vs {result.get('eps_estimate')} "
            f"guidance={result.get('guidance', '?')}"
        )

    return result


# ── Backward Compatibility ────────────────────────────────────

def get_crypto_sentiment(symbol: str) -> dict:
    """Legacy wrapper — calls get_market_intel and reshapes for old callers."""
    intel = get_market_intel(symbol, asset_type="crypto")
    if "error" in intel:
        return intel

    sent = intel.get("sentiment", {})
    return {
        "sentiment_score": sent.get("score", 0),
        "direction": sent.get("direction", "neutral"),
        "confidence": int(sent.get("confidence", 0) * 100),
        "key_catalysts": [intel.get("news_summary", "")] if intel.get("news_summary") else [],
        "risk_flags": intel.get("risk_flags", []),
        "edge_score": intel.get("edge_score", 0),
        "_citations": intel.get("_citations", []),
    }


# ── CLI Test ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        sym = sys.argv[2] if len(sys.argv) > 2 else "BTC"
        if cmd == "intel":
            asset = "stock" if sym.isalpha() and len(sym) <= 5 and sym not in ("BTC", "ETH", "SOL") else "crypto"
            r = get_market_intel(sym, asset_type=asset)
            print(json.dumps(r, indent=2, default=str))
        elif cmd == "earnings":
            r = check_earnings(sym)
            print(json.dumps(r, indent=2, default=str))
        elif cmd == "gate":
            r = get_market_intel(sym)
            blocked, reason = should_block_trade(r, "long")
            print(f"Blocked: {blocked} | {reason}")
    else:
        print("Usage: python perplexity_intel.py intel BTC")
        print("       python perplexity_intel.py intel AAPL")
        print("       python perplexity_intel.py earnings RTX")
        print("       python perplexity_intel.py gate ETH")
