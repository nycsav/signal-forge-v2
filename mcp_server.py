#!/usr/bin/env python3
"""Signal Forge v2 — Local MCP Server

Exposes Signal Forge state to Perplexity (or any MCP client) via stdio.

Tools:
  - get_trade_summary       — last 24h activity from both engines
  - get_open_positions      — currently open positions (Alpaca source of truth)
  - get_recent_signals      — last N signals from signals_log
  - get_whale_events        — whale trigger events in last N hours
  - get_system_health       — engine PIDs, event bus, F&G, errors
  - get_risk_audit          — RiskAgent thresholds and floors, veto rate
  - run_morning_audit       — consolidated morning audit

Run:
  python mcp_server.py             # stdio transport (for MCP clients)
"""

import sqlite3
import subprocess
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

# Make project imports work when launched from anywhere
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.risk_agent import RiskAgent  # noqa: E402
from config.settings import settings      # noqa: E402

PAPER_DB = PROJECT_ROOT / "data" / "trades.db"
LIVE_DB = PROJECT_ROOT / "data" / "live_trades.db"
LIVE_LOG = PROJECT_ROOT / "logs" / "live_engine.log"

mcp = FastMCP("signal-forge")


# ── DB helpers ────────────────────────────────────────────────────

def _query(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _scalar(db_path: Path, sql: str, params: tuple = ()) -> Any:
    rows = _query(db_path, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_secret_key or settings.alpaca_api_secret,
    }


# ── Tools ─────────────────────────────────────────────────────────

@mcp.tool
def get_trade_summary() -> dict:
    """Last 24h trade activity across both paper and live engines."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()

    paper_opened = _scalar(PAPER_DB, "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (cutoff,))
    paper_closed = _scalar(PAPER_DB, "SELECT COUNT(*) FROM trades WHERE closed_at >= ?", (cutoff.replace("T", " "),))
    paper_outcomes = _query(
        PAPER_DB,
        "SELECT was_profitable, pnl_usd, pnl_pct, exit_reason FROM trade_outcomes "
        "WHERE created_at >= ? AND exit_reason != 'manual_reset'",
        (cutoff.replace("T", " "),),
    )
    paper_wins = sum(1 for o in paper_outcomes if o["was_profitable"])
    paper_losses = len(paper_outcomes) - paper_wins
    paper_pnl = sum(o["pnl_usd"] or 0 for o in paper_outcomes)

    live_opened = _scalar(LIVE_DB, "SELECT COUNT(*) FROM live_trades WHERE opened_at >= ?", (cutoff,))
    live_closed_rows = _query(
        LIVE_DB,
        "SELECT pnl_usd, pnl_after_fees, exit_reason FROM live_trades "
        "WHERE closed_at >= ? AND exit_reason != 'manual_reset'",
        (cutoff.replace("T", " "),),
    )
    live_wins = sum(1 for r in live_closed_rows if (r["pnl_after_fees"] or 0) > 0)
    live_losses = len(live_closed_rows) - live_wins
    live_pnl = sum(r["pnl_after_fees"] or 0 for r in live_closed_rows)

    risk_approved = _scalar(
        PAPER_DB,
        "SELECT COUNT(*) FROM agent_events WHERE agent_name='risk_agent' AND event_type='approved' AND timestamp >= ?",
        (cutoff,),
    )
    risk_vetoed = _scalar(
        PAPER_DB,
        "SELECT COUNT(*) FROM agent_events WHERE agent_name='risk_agent' AND event_type='vetoed' AND timestamp >= ?",
        (cutoff,),
    )

    paper_total = paper_wins + paper_losses
    live_total = live_wins + live_losses
    return {
        "window_hours": 24,
        "paper": {
            "opened": paper_opened or 0,
            "closed": len(paper_outcomes),
            "wins": paper_wins,
            "losses": paper_losses,
            "win_rate_pct": round(paper_wins / paper_total * 100, 1) if paper_total else None,
            "total_pnl_usd": round(paper_pnl, 2),
        },
        "live": {
            "opened": live_opened or 0,
            "closed": len(live_closed_rows),
            "wins": live_wins,
            "losses": live_losses,
            "win_rate_pct": round(live_wins / live_total * 100, 1) if live_total else None,
            "total_pnl_usd": round(live_pnl, 2),
        },
        "risk_agent": {
            "approved": risk_approved or 0,
            "vetoed": risk_vetoed or 0,
            "veto_rate_pct": round((risk_vetoed or 0) / max((risk_approved or 0) + (risk_vetoed or 0), 1) * 100, 1),
        },
    }


@mcp.tool
def get_open_positions() -> dict:
    """All currently open positions from Alpaca (source of truth)."""
    try:
        r = httpx.get(
            f"{settings.alpaca_base_url}/v2/positions",
            headers=_alpaca_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return {"error": f"Alpaca {r.status_code}: {r.text[:200]}", "positions": []}
        positions = r.json()
    except Exception as e:
        return {"error": str(e), "positions": []}

    # Pull paper trade open times to compute hold duration
    paper_open = {
        t["symbol"]: t["opened_at"]
        for t in _query(PAPER_DB, "SELECT symbol, opened_at FROM trades WHERE status='open'")
    }

    out = []
    for p in positions:
        sym = p.get("symbol", "")
        sym_dash = sym.replace("USD", "-USD") if "USD" in sym and "-" not in sym else sym
        opened_at = paper_open.get(sym_dash) or paper_open.get(sym) or None
        hold_h = None
        if opened_at:
            try:
                hold_h = round((datetime.now() - datetime.fromisoformat(opened_at)).total_seconds() / 3600, 1)
            except Exception:
                pass
        out.append({
            "symbol": sym,
            "qty": float(p.get("qty", 0) or 0),
            "entry_price": float(p.get("avg_entry_price", 0) or 0),
            "current_price": float(p.get("current_price", 0) or 0),
            "market_value": float(p.get("market_value", 0) or 0),
            "unrealized_pnl_usd": float(p.get("unrealized_pl", 0) or 0),
            "unrealized_pnl_pct": float(p.get("unrealized_plpc", 0) or 0) * 100,
            "hold_hours": hold_h,
            "side": p.get("side", "long"),
        })

    return {"count": len(out), "positions": out}


@mcp.tool
def get_recent_signals(limit: int = 20) -> dict:
    """Last N signals from signals_log with score, direction, decision, regime."""
    rows = _query(
        PAPER_DB,
        "SELECT id, timestamp, symbol, raw_score, ai_confidence, direction, decision, "
        "veto_reason, fear_greed, market_regime, score_breakdown "
        "FROM signals_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        if r.get("score_breakdown"):
            try:
                r["score_breakdown"] = json.loads(r["score_breakdown"])
            except Exception:
                pass
        if r.get("raw_score") is not None:
            r["raw_score"] = round(r["raw_score"], 1)
    return {"count": len(rows), "signals": rows}


@mcp.tool
def get_whale_events(hours: int = 24) -> dict:
    """Whale trigger events in the last N hours with direction, USD, entity."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = _query(
        PAPER_DB,
        "SELECT id, timestamp, event_type, payload FROM agent_events "
        "WHERE agent_name='whale_trigger' AND timestamp >= ? ORDER BY id DESC",
        (cutoff,),
    )
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"]) if r.get("payload") else {}
        except Exception:
            p = {}
        out.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "direction": p.get("direction", "unknown"),
            "type": p.get("type", ""),
            "strength": p.get("strength", 0),
            "usd_value": p.get("usd_value", 0),
            "from_entity": p.get("from_entity") or p.get("from", ""),
            "to_entity": p.get("to_entity") or p.get("to", ""),
            "token": p.get("token", ""),
            "chain": p.get("chain", ""),
            "reason": p.get("reason", ""),
        })

    bullish = sum(1 for e in out if e["direction"] == "bullish")
    bearish = sum(1 for e in out if e["direction"] == "bearish")
    return {
        "window_hours": hours,
        "count": len(out),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": len(out) - bullish - bearish,
        "events": out,
    }


@mcp.tool
def get_system_health() -> dict:
    """Engine PIDs, last event bus activity, F&G, regime, recent errors."""
    # Process check
    try:
        ps = subprocess.run(
            ["ps", "axo", "pid,command"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        ps = ""

    paper_pids = [
        int(line.strip().split()[0])
        for line in ps.splitlines()
        if "main.py" in line and "grep" not in line and "signal-forge" in line
    ]
    live_pids = [
        int(line.strip().split()[0])
        for line in ps.splitlines()
        if "live.py" in line and "grep" not in line
    ]

    # Last event bus activity
    last_event = _query(
        PAPER_DB,
        "SELECT timestamp, agent_name, event_type FROM agent_events ORDER BY id DESC LIMIT 1",
    )
    last_event_row = last_event[0] if last_event else None

    # Latest market state
    latest_snap = _query(
        PAPER_DB,
        "SELECT fear_greed, market_regime, timestamp FROM market_snapshots ORDER BY id DESC LIMIT 1",
    )
    snap = latest_snap[0] if latest_snap else {}

    # Error count from live log in last hour
    error_count = 0
    error_samples = []
    if LIVE_LOG.exists():
        cutoff = datetime.now() - timedelta(hours=1)
        for line in LIVE_LOG.read_text().splitlines()[-1000:]:
            if "ERROR" in line or "Exception" in line:
                # parse leading timestamp
                try:
                    ts = datetime.fromisoformat(line[:23].replace(" ", "T"))
                    if ts >= cutoff:
                        error_count += 1
                        if len(error_samples) < 5:
                            error_samples.append(line[:200])
                except Exception:
                    pass

    return {
        "paper_engine": {
            "running": len(paper_pids) > 0,
            "pids": paper_pids,
            "instance_count": len(paper_pids),
            "warning": "MULTIPLE INSTANCES — DB race risk" if len(paper_pids) > 1 else None,
        },
        "live_engine": {
            "running": len(live_pids) > 0,
            "pids": live_pids,
            "instance_count": len(live_pids),
        },
        "event_bus": {
            "last_activity": last_event_row,
        },
        "market": {
            "fear_greed": snap.get("fear_greed"),
            "regime": snap.get("market_regime"),
            "snapshot_time": snap.get("timestamp"),
        },
        "errors_last_hour": {
            "count": error_count,
            "samples": error_samples,
        },
    }


@mcp.tool
def get_risk_audit() -> dict:
    """RiskAgent thresholds (with floors), 24h veto rate, regime state."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()

    approved = _scalar(
        PAPER_DB,
        "SELECT COUNT(*) FROM agent_events WHERE agent_name='risk_agent' "
        "AND event_type='approved' AND timestamp >= ?",
        (cutoff,),
    ) or 0
    vetoed = _scalar(
        PAPER_DB,
        "SELECT COUNT(*) FROM agent_events WHERE agent_name='risk_agent' "
        "AND event_type='vetoed' AND timestamp >= ?",
        (cutoff,),
    ) or 0

    # Top veto reasons
    veto_reason_rows = _query(
        PAPER_DB,
        "SELECT json_extract(payload, '$.reason') AS reason, COUNT(*) AS cnt "
        "FROM agent_events WHERE agent_name='risk_agent' AND event_type='vetoed' "
        "AND timestamp >= ? GROUP BY reason ORDER BY cnt DESC LIMIT 10",
        (cutoff,),
    )

    # Latest regime params
    regime_rows = _query(
        PAPER_DB,
        "SELECT timestamp, payload FROM agent_events WHERE agent_name='regime_engine' "
        "ORDER BY id DESC LIMIT 1",
    )
    regime = {}
    if regime_rows:
        try:
            regime = json.loads(regime_rows[0]["payload"])
            regime["updated_at"] = regime_rows[0]["timestamp"]
        except Exception:
            pass

    return {
        "thresholds": {
            "MIN_SIGNAL_SCORE_FLOOR": RiskAgent.MIN_SIGNAL_SCORE_FLOOR,
            "MIN_AI_CONFIDENCE_FLOOR": RiskAgent.MIN_AI_CONFIDENCE_FLOOR,
            "MIN_SIGNAL_SCORE_class_default": RiskAgent.MIN_SIGNAL_SCORE,
            "MIN_AI_CONFIDENCE_class_default": RiskAgent.MIN_AI_CONFIDENCE,
            "MAX_OPEN_POSITIONS": RiskAgent.MAX_OPEN_POSITIONS,
            "MAX_POSITION_PCT": RiskAgent.MAX_POSITION_PCT,
            "MIN_RISK_REWARD": RiskAgent.MIN_RISK_REWARD,
            "DAILY_LOSS_LIMIT": RiskAgent.DAILY_LOSS_LIMIT,
            "WEEKLY_LOSS_LIMIT": RiskAgent.WEEKLY_LOSS_LIMIT,
            "floor_rule": "max(FLOOR, instance_value) — RegimeEngine cannot lower below floor",
        },
        "last_24h": {
            "approved": approved,
            "vetoed": vetoed,
            "total": approved + vetoed,
            "veto_rate_pct": round(vetoed / max(approved + vetoed, 1) * 100, 1),
            "top_veto_reasons": veto_reason_rows,
        },
        "regime_state": regime,
    }


@mcp.tool
def run_morning_audit() -> dict:
    """Consolidated morning audit — calls trade summary, risk audit, system health, whale events."""
    return {
        "generated_at": datetime.now().isoformat(),
        "trade_summary": get_trade_summary(),
        "risk_audit": get_risk_audit(),
        "system_health": get_system_health(),
        "whale_events": get_whale_events(hours=12),
        "recent_signals": get_recent_signals(limit=10),
        "open_positions": get_open_positions(),
    }


if __name__ == "__main__":
    mcp.run()
