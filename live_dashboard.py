#!/usr/bin/env python3
"""Signal Forge v2 — Live Trading Dashboard

Separate dashboard on port 8889 for real money tracking.
Paper dashboard stays on 8888.

Shows: live P&L, trade history, daily snapshots, risk status, journal.
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config.settings import settings
from db.live_repository import LiveRepository
from agents.risk_agent import RiskAgent

app = FastAPI(title="Signal Forge v2 — Live Trading")
repo = LiveRepository()


@app.get("/api/status")
async def status():
    alpaca = {}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{settings.alpaca_base_url}/v2/account",
                headers={"APCA-API-KEY-ID": settings.alpaca_api_key,
                          "APCA-API-SECRET-KEY": settings.alpaca_secret_key or settings.alpaca_api_secret})
            if r.status_code == 200:
                a = r.json()
                alpaca = {"portfolio_value": float(a.get("portfolio_value", 0)),
                          "cash": float(a.get("cash", 0)), "status": a.get("status")}
        except Exception:
            pass

    pnl = repo.get_total_pnl()
    halted, reason = repo.check_daily_halt(15.00)  # 5% of $300

    return {
        "mode": "LIVE" if not halted else "HALTED",
        "starting_capital": 300.00,
        "alpaca": alpaca,
        "pnl": pnl,
        "halted": halted,
        "halt_reason": reason,
        "rules": {
            "coins": ["BTC-USD", "ETH-USD", "SOL-USD"],
            "min_signal_score": RiskAgent.MIN_SIGNAL_SCORE,
            "min_ai_confidence": RiskAgent.MIN_AI_CONFIDENCE,
            "max_positions": RiskAgent.MAX_OPEN_POSITIONS,
            "position_size_pct": RiskAgent.MAX_POSITION_PCT * 100,
            "daily_loss_limit": 15.00,
            "pipeline": "EventBus → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent",
        },
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/trades")
async def trades():
    return {"open": repo.get_open_trades(), "closed": repo.get_closed_trades(50), "all": repo.get_all_trades(100)}


@app.get("/api/daily")
async def daily():
    return {"history": repo.get_daily_history(30)}


@app.get("/api/journal")
async def journal():
    return {"entries": repo.get_journal(100)}


@app.get("/api/pnl")
async def pnl():
    return repo.get_total_pnl()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signal Forge — LIVE</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
body { background: #0a0e17; font-family: 'JetBrains Mono', monospace; }
.pulse { animation: pulse 2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
</style>
</head>
<body class="text-gray-300 p-6">

<div class="flex items-center gap-4 mb-6">
  <div class="w-3 h-3 rounded-full bg-red-500 pulse" id="mode-dot"></div>
  <h1 class="text-2xl font-bold text-white">SIGNAL FORGE — <span id="mode" class="text-red-400">LIVE</span></h1>
  <span class="text-xs bg-gray-800 px-3 py-1 rounded-full" id="capital">--</span>
</div>

<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center">
    <div class="text-gray-500 text-xs mb-1">Total P&L</div>
    <div class="text-2xl font-bold" id="total-pnl">--</div>
  </div>
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center">
    <div class="text-gray-500 text-xs mb-1">Win Rate</div>
    <div class="text-2xl font-bold" id="win-rate">--</div>
  </div>
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center">
    <div class="text-gray-500 text-xs mb-1">Trades</div>
    <div class="text-2xl font-bold text-white" id="trade-count">--</div>
  </div>
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center">
    <div class="text-gray-500 text-xs mb-1">Total Fees</div>
    <div class="text-2xl font-bold text-yellow-400" id="total-fees">--</div>
  </div>
</div>

<div class="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-6">
  <h2 class="text-sm font-semibold text-white uppercase mb-3">Live Rules</h2>
  <div id="rules" class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs"></div>
</div>

<div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
    <h2 class="text-sm font-semibold text-white uppercase mb-3">Open Trades</h2>
    <div id="open-trades"></div>
  </div>
  <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
    <h2 class="text-sm font-semibold text-white uppercase mb-3">Closed Trades</h2>
    <div id="closed-trades" class="max-h-80 overflow-y-auto"></div>
  </div>
</div>

<div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
  <h2 class="text-sm font-semibold text-white uppercase mb-3">Live Journal</h2>
  <div id="journal" class="max-h-60 overflow-y-auto text-xs space-y-1"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const money = v => { v=Number(v)||0; return (v<0?'-':'')+'$'+Math.abs(v).toFixed(2); };
const cls = v => Number(v)>0?'text-green-400':Number(v)<0?'text-red-400':'text-gray-500';

async function refresh() {
  try {
    const [s, t, j] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/trades').then(r=>r.json()),
      fetch('/api/journal').then(r=>r.json()),
    ]);

    const pnl = s.pnl || {};
    $('mode').textContent = s.mode;
    $('mode').className = s.halted ? 'text-red-400' : 'text-green-400';
    $('mode-dot').className = s.halted ? 'w-3 h-3 rounded-full bg-red-500 pulse' : 'w-3 h-3 rounded-full bg-green-500 pulse';
    $('capital').textContent = '$' + s.starting_capital;
    $('total-pnl').textContent = money(pnl.total_pnl);
    $('total-pnl').className = 'text-2xl font-bold ' + cls(pnl.total_pnl);
    $('win-rate').textContent = (pnl.win_rate||0).toFixed(1) + '%';
    $('win-rate').className = 'text-2xl font-bold ' + ((pnl.win_rate||0)>=55?'text-green-400':'text-red-400');
    $('trade-count').textContent = pnl.total_trades || 0;
    $('total-fees').textContent = money(pnl.total_fees);

    const r = s.rules || {};
    $('rules').innerHTML = Object.entries(r).map(([k,v]) =>
      '<div class="bg-gray-800 rounded px-2 py-1"><span class="text-gray-500">'+k.replace(/_/g,' ')+'</span> <span class="text-white">'+JSON.stringify(v)+'</span></div>'
    ).join('');

    const open = t.open || [];
    $('open-trades').innerHTML = open.length ? open.map(t =>
      '<div class="flex justify-between py-1 border-b border-gray-800 text-xs"><span class="text-white">'+t.symbol+'</span><span>$'+((t.size_usd)||0).toFixed(2)+'</span><span class="'+(t.pnl_usd>0?'text-green-400':'text-red-400')+'">'+money(t.pnl_usd)+'</span></div>'
    ).join('') : '<p class="text-gray-600 text-xs py-4 text-center">No open trades</p>';

    const closed = t.closed || [];
    $('closed-trades').innerHTML = closed.length ? closed.map(t =>
      '<div class="flex justify-between py-1 border-b border-gray-800 text-xs"><span class="text-white">'+t.symbol+'</span><span>'+t.exit_reason+'</span><span class="'+cls(t.pnl_after_fees)+'">'+money(t.pnl_after_fees)+'</span></div>'
    ).join('') : '<p class="text-gray-600 text-xs py-4 text-center">No closed trades yet</p>';

    const entries = j.entries || [];
    $('journal').innerHTML = entries.slice(0,20).map(e =>
      '<div class="flex gap-2"><span class="text-gray-600 shrink-0">'+e.timestamp.slice(11,19)+'</span><span class="text-yellow-400">['+e.category+']</span><span>'+e.message+'</span></div>'
    ).join('');
  } catch(e) {}
}

refresh();
setInterval(refresh, 15000);
</script>
</body></html>"""


if __name__ == "__main__":
    print("Signal Forge v2 — LIVE Dashboard: http://localhost:8889")
    print(f"Starting capital: $300")
    print(f"Coins: BTC-USD, ETH-USD, SOL-USD")
    uvicorn.run(app, host="0.0.0.0", port=8889, log_level="warning")
