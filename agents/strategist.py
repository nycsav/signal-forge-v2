"""Signal Forge — Strategic Learnings Engine

Monitors the system's actual behavior and generates actionable insights.
Learns from: positions, orders, signals, market conditions, agent performance.
Outputs recommendations to the dashboard in real-time.
"""

import sqlite3
import logging
import math
from datetime import datetime, timedelta
from config.settings import settings
TRADES_DB_PATH = settings.database_path

logger = logging.getLogger(__name__)


def _get_db():
    conn = sqlite3.connect(TRADES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def generate_strategic_report(
    positions: list,
    orders: list,
    portfolio_value: float,
    cash: float,
    fear_greed: int,
    prices: dict,
) -> dict:
    """Generate a full strategic analysis based on current system state."""

    findings = []
    recommendations = []
    warnings = []
    metrics = {}

    # ── 1. Portfolio Analysis ──
    total_positions = len(positions)
    total_unrealized = sum(p.get("unrealized_pl", 0) for p in positions)
    total_invested = sum(p.get("market_value", 0) for p in positions)
    cash_pct = (cash / portfolio_value * 100) if portfolio_value > 0 else 0
    invested_pct = (total_invested / portfolio_value * 100) if portfolio_value > 0 else 0

    metrics["portfolio_value"] = portfolio_value
    metrics["cash"] = cash
    metrics["cash_pct"] = round(cash_pct, 1)
    metrics["invested_pct"] = round(invested_pct, 1)
    metrics["total_unrealized"] = round(total_unrealized, 2)
    metrics["positions_count"] = total_positions

    if cash_pct > 60:
        findings.append({
            "type": "observation",
            "title": "High cash position",
            "detail": f"{cash_pct:.0f}% in cash — capital is underdeployed",
            "severity": "info",
        })
    if cash_pct < 20:
        warnings.append({
            "type": "warning",
            "title": "Low cash reserves",
            "detail": f"Only {cash_pct:.0f}% in cash — limited ability to capitalize on opportunities",
            "severity": "high",
        })

    # ── 2. Position Health ──
    winners = [p for p in positions if p.get("unrealized_pl", 0) > 0]
    losers = [p for p in positions if p.get("unrealized_pl", 0) < 0]
    flat = [p for p in positions if abs(p.get("unrealized_plpc", 0)) < 0.005]

    metrics["winners"] = len(winners)
    metrics["losers"] = len(losers)
    metrics["flat"] = len(flat)

    if len(flat) > total_positions * 0.5 and total_positions > 0:
        findings.append({
            "type": "pattern",
            "title": "Stagnant portfolio",
            "detail": f"{len(flat)}/{total_positions} positions are flat (<0.5%). Market may be ranging — consider tighter time exits.",
            "severity": "medium",
        })

    # Biggest winner and loser
    if positions:
        best = max(positions, key=lambda p: p.get("unrealized_plpc", 0))
        worst = min(positions, key=lambda p: p.get("unrealized_plpc", 0))
        metrics["best_position"] = {"symbol": best["symbol"], "pnl_pct": round(best["unrealized_plpc"] * 100, 2)}
        metrics["worst_position"] = {"symbol": worst["symbol"], "pnl_pct": round(worst["unrealized_plpc"] * 100, 2)}

    # ── 3. Market Regime Detection ──
    regime = "unknown"
    if fear_greed < 20:
        regime = "extreme_fear"
        findings.append({
            "type": "market",
            "title": "Extreme Fear regime",
            "detail": f"F&G={fear_greed}. Historically, extreme fear is the best time to accumulate. Contrarian buying opportunity.",
            "severity": "opportunity",
        })
        recommendations.append({
            "action": "Consider accumulating",
            "reasoning": "Fear & Greed at extreme levels historically precedes recovery. Scale into positions gradually.",
            "confidence": "medium",
        })
    elif fear_greed < 35:
        regime = "fear"
        findings.append({
            "type": "market",
            "title": "Fear regime",
            "detail": f"F&G={fear_greed}. Market pessimistic but not extreme. Watch for reversal signals.",
            "severity": "info",
        })
    elif fear_greed > 75:
        regime = "extreme_greed"
        warnings.append({
            "type": "warning",
            "title": "Extreme Greed — distribution risk",
            "detail": f"F&G={fear_greed}. Consider taking profits and reducing exposure.",
            "severity": "high",
        })
    elif fear_greed > 55:
        regime = "greed"
    else:
        regime = "neutral"

    metrics["regime"] = regime
    metrics["fear_greed"] = fear_greed

    # ── 4. Order Flow Analysis ──
    filled_orders = [o for o in orders if o.get("status") == "filled"]
    buy_orders = [o for o in filled_orders if o.get("side") == "buy"]
    sell_orders = [o for o in filled_orders if o.get("side") == "sell"]

    metrics["total_orders"] = len(orders)
    metrics["filled_orders"] = len(filled_orders)
    metrics["buy_count"] = len(buy_orders)
    metrics["sell_count"] = len(sell_orders)

    if len(buy_orders) > 0 and len(sell_orders) == 0:
        findings.append({
            "type": "pattern",
            "title": "All buys, no sells",
            "detail": f"{len(buy_orders)} buys, 0 sells. The exit strategy hasn't triggered yet. Review exit thresholds.",
            "severity": "medium",
        })
        recommendations.append({
            "action": "Review exit thresholds",
            "reasoning": "No sells executed suggests thresholds may be too wide for current market conditions. Consider tightening time-based exits or adding a flat-market exit.",
            "confidence": "high",
        })

    # ── 5. Signal Accuracy (from DB) ──
    try:
        conn = _get_db()
        signals = conn.execute(
            "SELECT action, score, pair FROM signals_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        conn.close()

        if signals:
            total_sigs = len(signals)
            buy_sigs = sum(1 for s in signals if s["action"] == "BUY")
            skip_sigs = sum(1 for s in signals if s["action"] in ("SKIP", "HOLD"))
            avg_score = sum(s["score"] or 0 for s in signals) / total_sigs

            metrics["signal_stats"] = {
                "total_scanned": total_sigs,
                "buy_signals": buy_sigs,
                "skip_signals": skip_sigs,
                "avg_score": round(avg_score, 1),
                "buy_rate": round(buy_sigs / total_sigs * 100, 1) if total_sigs > 0 else 0,
            }

            if avg_score < 45:
                findings.append({
                    "type": "pattern",
                    "title": "Low average signal scores",
                    "detail": f"Average AI score is {avg_score:.0f}/100. The AI is bearish on most pairs — consistent with extreme fear market.",
                    "severity": "info",
                })
    except Exception:
        pass

    # ── 6. Circuit Breaker Status ──
    if total_positions >= 5:
        warnings.append({
            "type": "warning",
            "title": "Circuit breaker active",
            "detail": f"At {total_positions}/5 max positions. No new trades can execute until positions close.",
            "severity": "high",
        })
        recommendations.append({
            "action": "Free position slots",
            "reasoning": "Close the weakest position to allow the system to act on new signals. In extreme fear, being locked out means missing the bottom.",
            "confidence": "high",
        })

    # ── 7. Concentration Risk ──
    if positions:
        values = [p.get("market_value", 0) for p in positions]
        max_val = max(values) if values else 0
        if max_val > 0 and portfolio_value > 0:
            max_concentration = max_val / portfolio_value * 100
            if max_concentration > 15:
                sym = max(positions, key=lambda p: p.get("market_value", 0))["symbol"]
                warnings.append({
                    "type": "warning",
                    "title": f"Concentrated position: {sym}",
                    "detail": f"{sym} is {max_concentration:.0f}% of portfolio. Consider trimming.",
                    "severity": "medium",
                })

    # ── 8. Strategic Recommendations Summary ──
    if not recommendations:
        recommendations.append({
            "action": "Hold and monitor",
            "reasoning": "Current positions are within normal parameters. Let the exit strategy work.",
            "confidence": "medium",
        })

    return {
        "timestamp": datetime.now().isoformat(),
        "metrics": metrics,
        "findings": findings,
        "warnings": warnings,
        "recommendations": recommendations,
        "learning_notes": [
            "Circuit breaker + passive exits = frozen portfolio in ranging markets",
            "Extreme fear (F&G<20) historically precedes 30-60 day recovery cycles",
            "Uniform $2K position sizing ignores conviction — Half-Kelly now active for future trades",
            "Time-based exits (48h flat, 72h max) are the primary exit mechanism in low-volatility fear markets",
            "No feedback loop active yet — closed trades need to feed back into AI scoring prompt",
        ],
    }
