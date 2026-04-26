"""
Enso Sonar Engine — Perplexity-Powered Trading Intelligence

Three-tier Sonar architecture:
  Tier 1: sonar (fast, $1/M) — real-time sentiment checks every 10 min
  Tier 2: sonar-pro (deep, $3/M) — catalyst analysis before entry
  Tier 3: sonar-reasoning-pro ($2/M) — complex trade thesis with chain-of-thought

Integrated with Public.com API for live chains and execution.
14-gate HyperGuard-style risk engine before every trade.

Cost budget: ~$4/day = ~$120/month for both engines.
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

ET = ZoneInfo("America/New_York")
PPLX_BASE = "https://api.perplexity.ai"


def _get_key() -> str:
    return os.getenv("PPLX_API_KEY", "")


def _call_sonar(
    prompt: str,
    system: str,
    model: str = "sonar",
    json_schema: dict = None,
    recency: str = "hour",
    domains: list[str] = None,
    timeout: int = 15,
) -> dict:
    """Unified Sonar API caller with structured output."""
    api_key = _get_key()
    if not api_key:
        return {"error": "PPLX_API_KEY not set"}

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
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])

        if json_schema:
            try:
                # Strip reasoning tokens if present (sonar-reasoning-pro wraps in <think>)
                clean = content
                if "<think>" in clean:
                    think_end = clean.find("</think>")
                    if think_end > 0:
                        clean = clean[think_end + 8:].strip()
                parsed = json.loads(clean)
                parsed["_citations"] = citations
                parsed["_model"] = model
                parsed["_timestamp"] = datetime.now(ET).isoformat()
                return parsed
            except json.JSONDecodeError:
                return {"raw": content, "_citations": citations, "_model": model}

        return {"content": content, "_citations": citations, "_model": model}

    except httpx.TimeoutException:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# TIER 1: Fast Sentiment (sonar, every 10 min, $0.006/call)
# ═══════════════════════════════════════════════════════════════

FAST_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["long", "short", "skip"]},
        "confidence": {"type": "number"},
        "edge_score": {"type": "number"},
        "catalyst": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "confidence", "edge_score"],
}


def fast_check(symbol: str, asset_type: str = "crypto") -> dict:
    """Tier 1: Quick directional check. 3-5 second response."""
    if asset_type == "crypto":
        prompt = (
            f"Is {symbol.replace('-USD','')} likely to go UP or DOWN in the next 2 hours? "
            f"Check: price trend last hour, any breaking news, whale activity, funding rates. "
            f"Return direction (long/short/skip), confidence (0-100), and edge_score (-1 to +1). "
            f"If no clear edge, return skip with confidence 0."
        )
        domains = ["coindesk.com", "theblock.co", "cointelegraph.com"]
    else:
        prompt = (
            f"Is {symbol} stock likely to go UP or DOWN today? "
            f"Check: pre-market direction, any earnings/news in last 12h, sector momentum. "
            f"Return direction (long/short/skip), confidence (0-100), edge_score (-1 to +1). "
            f"If no clear edge, return skip."
        )
        domains = ["reuters.com", "bloomberg.com", "cnbc.com"]

    return _call_sonar(
        prompt=prompt,
        system="You are a trading desk analyst. Be decisive. Only say 'long' or 'short' if you have real evidence. Otherwise say 'skip'.",
        model="sonar",
        json_schema=FAST_SCHEMA,
        recency="hour",
        domains=domains,
        timeout=10,
    )


# ═══════════════════════════════════════════════════════════════
# TIER 2: Catalyst Analysis (sonar-pro, before entry, $0.02/call)
# ═══════════════════════════════════════════════════════════════

CATALYST_SCHEMA = {
    "type": "object",
    "properties": {
        "enter": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["long", "short", "skip"]},
        "confidence": {"type": "number"},
        "edge_score": {"type": "number"},
        "catalyst": {"type": "string"},
        "catalyst_date": {"type": "string"},
        "invalidation_price": {"type": "number"},
        "target_price": {"type": "number"},
        "time_horizon": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "cross_asset_signals": {"type": "string"},
        "sources_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["enter", "direction", "confidence", "invalidation_price", "target_price"],
}


def catalyst_analysis(symbol: str, asset_type: str = "stock") -> dict:
    """Tier 2: Deep catalyst analysis before committing capital."""
    if asset_type == "crypto":
        prompt = (
            f"TRADE ANALYSIS for {symbol.replace('-USD','')}. I am considering a position.\n\n"
            f"REQUIRED DATA (search last 2 hours only):\n"
            f"1. PRICE ACTION: Current price, 1h/24h change, distance from nearest support/resistance\n"
            f"2. ORDER FLOW: Whale transactions >$10M? Exchange net flow? Perpetual funding rate?\n"
            f"3. CATALYST: Any specific event in next 48h that moves price >2%?\n"
            f"4. CROSS-ASSET: DXY, S&P futures, gold — risk-on or risk-off?\n"
            f"5. RISK FLAGS: FOMC, CPI, options expiry, or regulatory action within 24h?\n\n"
            f"Return enter=true ONLY if there is a specific, identifiable catalyst. "
            f"Include invalidation_price (where thesis is wrong) and target_price (take profit). "
            f"If no clear catalyst, return enter=false."
        )
        domains = ["coindesk.com", "theblock.co", "reuters.com", "bloomberg.com"]
    else:
        prompt = (
            f"TRADE ANALYSIS for {symbol}. I am considering an options position.\n\n"
            f"REQUIRED DATA (search last 12 hours):\n"
            f"1. PRICE ACTION: Current price, pre-market direction, key support/resistance levels\n"
            f"2. EARNINGS: Did {symbol} report in last 48h? If yes: EPS actual vs estimate, beat/miss, guidance\n"
            f"3. CATALYST: Earnings date if upcoming, analyst upgrades/downgrades today, any sector news\n"
            f"4. OPTIONS FLOW: Any unusual volume or sweep orders reported?\n"
            f"5. CROSS-ASSET: Sector ETF direction, VIX level, S&P direction\n"
            f"6. RISK FLAGS: Earnings imminent? FOMC? High IV rank?\n\n"
            f"Return enter=true ONLY if there is a dated catalyst with specific price levels. "
            f"Include invalidation_price and target_price. "
            f"If no clear catalyst, return enter=false."
        )
        domains = ["reuters.com", "bloomberg.com", "cnbc.com", "seekingalpha.com", "sec.gov"]

    return _call_sonar(
        prompt=prompt,
        system="You are an institutional derivatives desk analyst. Only recommend trades with identifiable catalysts and specific price levels. Never guess. If data is unavailable, say so and return enter=false.",
        model="sonar-pro",
        json_schema=CATALYST_SCHEMA,
        recency="day",
        domains=domains,
        timeout=20,
    )


# ═══════════════════════════════════════════════════════════════
# TIER 3: Reasoning Thesis (sonar-reasoning-pro, high conviction only)
# ═══════════════════════════════════════════════════════════════

def deep_thesis(symbol: str, direction: str, catalyst: str) -> dict:
    """Tier 3: Chain-of-thought reasoning for high-conviction trades only."""
    return _call_sonar(
        prompt=(
            f"I am about to enter a {direction} position on {symbol} based on this catalyst: {catalyst}\n\n"
            f"Think step by step:\n"
            f"1. Is this catalyst real and verified from multiple sources?\n"
            f"2. Has the market already priced this in?\n"
            f"3. What is the expected magnitude of the move?\n"
            f"4. What could go wrong — what's the bear case for my thesis?\n"
            f"5. What is the optimal entry timing — now, or wait for confirmation?\n"
            f"6. Given all factors, should I enter this trade YES or NO?\n\n"
            f"Be brutally honest. If this is a bad trade, say so."
        ),
        system="You are a senior risk manager reviewing a trade proposal. Your job is to find reasons NOT to take the trade. Only approve if the evidence is overwhelming.",
        model="sonar-reasoning-pro",
        recency="day",
        timeout=30,
    )


# ═══════════════════════════════════════════════════════════════
# 14-GATE RISK ENGINE (HyperGuard-inspired)
# ═══════════════════════════════════════════════════════════════

class RiskGateEngine:
    """14-gate risk validation before any trade executes."""

    def __init__(self, config: dict = None):
        self.config = config or {
            "whitelist": ["BTC-USD", "ETH-USD", "SOL-USD",  # crypto
                         "AMZN", "TSLA", "GOOG", "CRM", "COIN", "NVDA", "AAPL",  # stocks
                         "SPY", "QQQ", "XLE", "RTX", "AMD"],
            "max_position_usd": 200,
            "max_positions": 3,
            "daily_loss_limit_usd": 200,
            "max_drawdown_pct": 5.0,
            "kill_switch_drawdown_pct": 8.0,
            "max_orders_per_hour": 6,
            "cooldown_minutes": 60,
            "min_sonar_confidence": 60,
            "min_sonar_edge": 0.20,
            "blocked_hours_et": [],  # e.g., [9, 9.5] for first 30 min
            "min_volume": 100,
            "max_iv_rv_ratio": 1.5,
        }
        self._order_timestamps: list[float] = []
        self._last_trade_time: dict[str, float] = {}
        self._daily_pnl: float = 0
        self._peak_equity: float = 0
        self._current_equity: float = 0
        self._open_positions: int = 0

    def check_all_gates(self, trade: dict) -> tuple[bool, list[str]]:
        """Run all 14 gates. Returns (approved, list_of_gate_results)."""
        results = []
        symbol = trade.get("symbol", "")
        now = time.time()
        now_et = datetime.now(ET)

        # Gate 1: Kill switch
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - self._current_equity) / self._peak_equity * 100
            if drawdown >= self.config["kill_switch_drawdown_pct"]:
                results.append(f"GATE 1 KILL SWITCH: drawdown {drawdown:.1f}% >= {self.config['kill_switch_drawdown_pct']}%")
                return False, results
        results.append("GATE 1 kill_switch: PASS")

        # Gate 2: Whitelist
        clean_sym = symbol.replace("-USD", "").replace("/USD", "")
        if symbol not in self.config["whitelist"] and clean_sym not in [w.replace("-USD", "") for w in self.config["whitelist"]]:
            results.append(f"GATE 2 whitelist: BLOCKED — {symbol} not in approved list")
            return False, results
        results.append("GATE 2 whitelist: PASS")

        # Gate 3: Position size
        cost = trade.get("premium", trade.get("cost", 0))
        if cost > self.config["max_position_usd"]:
            results.append(f"GATE 3 position_size: BLOCKED — ${cost} > ${self.config['max_position_usd']}")
            return False, results
        results.append(f"GATE 3 position_size: PASS (${cost})")

        # Gate 4: Daily loss limit
        if self._daily_pnl < -self.config["daily_loss_limit_usd"]:
            results.append(f"GATE 4 daily_loss: BLOCKED — ${self._daily_pnl:.2f}")
            return False, results
        results.append("GATE 4 daily_loss: PASS")

        # Gate 5: Drawdown monitor
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._current_equity) / self._peak_equity * 100
            if dd >= self.config["max_drawdown_pct"]:
                results.append(f"GATE 5 drawdown: BLOCKED — {dd:.1f}%")
                return False, results
        results.append("GATE 5 drawdown: PASS")

        # Gate 6: Max positions
        if self._open_positions >= self.config["max_positions"]:
            results.append(f"GATE 6 max_positions: BLOCKED — {self._open_positions}/{self.config['max_positions']}")
            return False, results
        results.append("GATE 6 max_positions: PASS")

        # Gate 7: Rate limiting
        recent_orders = [t for t in self._order_timestamps if now - t < 3600]
        if len(recent_orders) >= self.config["max_orders_per_hour"]:
            results.append(f"GATE 7 rate_limit: BLOCKED — {len(recent_orders)} orders in last hour")
            return False, results
        results.append("GATE 7 rate_limit: PASS")

        # Gate 8: Time-of-day
        hour_decimal = now_et.hour + now_et.minute / 60
        if hour_decimal in self.config.get("blocked_hours_et", []):
            results.append(f"GATE 8 time_of_day: BLOCKED — {now_et.strftime('%H:%M')} ET")
            return False, results
        results.append("GATE 8 time_of_day: PASS")

        # Gate 9: Cooldown per asset
        last = self._last_trade_time.get(symbol, 0)
        cooldown_sec = self.config["cooldown_minutes"] * 60
        if now - last < cooldown_sec:
            remaining = int((cooldown_sec - (now - last)) / 60)
            results.append(f"GATE 9 cooldown: BLOCKED — {remaining}min remaining for {symbol}")
            return False, results
        results.append("GATE 9 cooldown: PASS")

        # Gate 10: Sonar confidence
        sonar_conf = trade.get("sonar_confidence", 0)
        if sonar_conf < self.config["min_sonar_confidence"]:
            results.append(f"GATE 10 sonar_confidence: BLOCKED — {sonar_conf} < {self.config['min_sonar_confidence']}")
            return False, results
        results.append(f"GATE 10 sonar_confidence: PASS ({sonar_conf})")

        # Gate 11: Sonar edge
        sonar_edge = abs(trade.get("sonar_edge", 0))
        if sonar_edge < self.config["min_sonar_edge"]:
            results.append(f"GATE 11 sonar_edge: BLOCKED — {sonar_edge:.2f} < {self.config['min_sonar_edge']}")
            return False, results
        results.append(f"GATE 11 sonar_edge: PASS ({sonar_edge:.2f})")

        # Gate 12: Volume check (for options)
        volume = trade.get("volume", 999)
        if volume < self.config["min_volume"]:
            results.append(f"GATE 12 volume: BLOCKED — {volume} < {self.config['min_volume']}")
            return False, results
        results.append("GATE 12 volume: PASS")

        # Gate 13: IV/RV ratio (don't overpay for options)
        iv_rv = trade.get("iv_rv_ratio", 1.0)
        if iv_rv > self.config["max_iv_rv_ratio"]:
            results.append(f"GATE 13 iv_rv: BLOCKED — {iv_rv:.2f} > {self.config['max_iv_rv_ratio']}")
            return False, results
        results.append("GATE 13 iv_rv: PASS")

        # Gate 14: Invalidation price must exist
        if not trade.get("invalidation_price"):
            results.append("GATE 14 invalidation: BLOCKED — no invalidation price defined")
            return False, results
        results.append("GATE 14 invalidation: PASS")

        return True, results

    def record_order(self, symbol: str):
        self._order_timestamps.append(time.time())
        self._last_trade_time[symbol] = time.time()
        self._open_positions += 1

    def record_close(self, pnl: float):
        self._daily_pnl += pnl
        self._open_positions = max(0, self._open_positions - 1)

    def update_equity(self, equity: float):
        self._current_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity


# ═══════════════════════════════════════════════════════════════
# PREVENTED LOSS TRACKER
# ═══════════════════════════════════════════════════════════════

_prevented_log: list[dict] = []


def log_prevented_trade(trade: dict, gate_results: list[str]):
    """Log a blocked trade so we can measure if the gate saved money."""
    _prevented_log.append({
        "timestamp": datetime.now(ET).isoformat(),
        "symbol": trade.get("symbol"),
        "direction": trade.get("direction"),
        "blocked_by": [g for g in gate_results if "BLOCKED" in g],
        "would_have_cost": trade.get("premium", 0),
        "market_price_at_block": trade.get("current_price", 0),
    })
    logger.warning(f"PREVENTED: {trade.get('symbol')} {trade.get('direction')} — {[g for g in gate_results if 'BLOCKED' in g]}")


def get_prevented_summary() -> dict:
    """Summary of prevented trades for measuring gate effectiveness."""
    return {
        "total_blocked": len(_prevented_log),
        "total_prevented_exposure": sum(t.get("would_have_cost", 0) for t in _prevented_log),
        "blocked_by_gate": {},
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python sonar_engine.py fast BTC")
        print("  python sonar_engine.py catalyst AMZN")
        print("  python sonar_engine.py thesis TSLA short 'Q1 delivery miss'")
        sys.exit(0)

    cmd = sys.argv[1]
    sym = sys.argv[2] if len(sys.argv) > 2 else "BTC"

    if cmd == "fast":
        asset = "crypto" if sym in ("BTC", "ETH", "SOL") else "stock"
        r = fast_check(sym if asset == "crypto" else sym, asset)
        print(json.dumps(r, indent=2, default=str))
    elif cmd == "catalyst":
        asset = "crypto" if sym in ("BTC", "ETH", "SOL") else "stock"
        r = catalyst_analysis(sym, asset)
        print(json.dumps(r, indent=2, default=str))
    elif cmd == "thesis":
        direction = sys.argv[3] if len(sys.argv) > 3 else "long"
        catalyst = sys.argv[4] if len(sys.argv) > 4 else "unknown"
        r = deep_thesis(sym, direction, catalyst)
        print(json.dumps(r, indent=2, default=str))
