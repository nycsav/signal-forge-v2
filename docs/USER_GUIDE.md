# SignalForge v2 — altFINS Edition — User Guide

## What is this?

A multi-agent AI crypto trading system running 24/7 on a Mac Mini M4. Uses local LLMs (Llama 3.2 + Qwen3 14B via Ollama) for signal generation, altFINS for enrichment, and a deterministic RiskAgent gate for all trade decisions.

Two engines run in parallel:
- **Paper engine** (`main.py`): $100K virtual portfolio via Alpaca, 50-coin watchlist
- **Live engine** (`live.py`): $300 real money via Coinbase, 3-coin watchlist

---

## Prerequisites

- Mac Mini M4 Pro (or similar — needs ~10GB RAM for Ollama models)
- Python 3.11+
- Ollama installed with: `qwen3:14b`, `llama3.2:3b`
- API keys: Alpaca, Coinbase, altFINS, CoinMarketCap (all free tier)

## Setup

```bash
cd ~/signal-forge-v2
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy env template and fill in your keys
cp .env.example .env
# Edit .env: ALPACA_API_KEY, COINBASE_API_KEY, ALTFINS_API_KEY, etc.
```

## Starting the System

Everything runs via launchd (auto-restart on crash):

```bash
# Load all daemons
launchctl load ~/Library/LaunchAgents/com.signalforge.v2.plist
launchctl load ~/Library/LaunchAgents/com.signalforge.live.plist
launchctl load ~/Library/LaunchAgents/com.signalforge.altfins-shadow.plist
launchctl load ~/Library/LaunchAgents/com.signalforge.ollama.plist

# Check status
launchctl list | grep signalforge

# View logs
tail -f logs/daemon-stderr.log        # main.py
tail -f logs/live-daemon-stderr.log   # live.py
tail -f logs/altfins-shadow-daemon-stdout.log
```

## Manual start (without launchd)

```bash
python main.py                    # Paper engine
python live.py                    # Live engine (real money!)
python altfins_shadow.py          # altFINS data collector
python mcp_server.py              # MCP server for Perplexity
```

## Running Backtests

### Our signals — 90-day replay

```bash
PYTHONPATH=. python scripts/historical_backtest.py
PYTHONPATH=. python scripts/historical_backtest.py --days 30 --symbols BTC,ETH
PYTHONPATH=. python scripts/historical_backtest.py --thresholds 50,55,60,65,70
```

### altFINS comparison (requires accumulated shadow data)

```bash
PYTHONPATH=. python scripts/altfins_comparison.py
```

### View backtest results

```bash
PYTHONPATH=. python scripts/backtest_report.py
PYTHONPATH=. python scripts/backtest_report.py --db data/altfins_comparison.db
PYTHONPATH=. python scripts/backtest_report.py --days 7
PYTHONPATH=. python scripts/backtest_report.py --regime capitulation
```

## Architecture

```
MarketDataAgent (5min) ──┐
TechnicalAgent          │     EventBus (priority queue)
SentimentAgent (15min)  ├───► CRITICAL → HIGH → NORMAL → LOW
OnChainAgent (1hr)      │          │
                        │    SignalBundle → AIAnalyst (3-step LLM)
                        │          │
                        │    TradeProposal → RiskAgent (10 checks)
                        │          │
                        │    if APPROVED → ExecutionAgent → Alpaca
                        │          │
                        │    OrderFilled → MonitorAgent (7-layer exits)
                        │          │
                        └──  TradeClosedEvent → LearningAgent (weekly)

Side channels:
  WhaleTrigger (60s Arkham) → bearish block / bullish scan
  ChartPatternAgent (4h)    → IHS / H&S / double bottom
  altFINS Enrichment (15m)  → patterns, oversold filter, crossovers
```

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Paper engine orchestrator ($100K, 50 coins) |
| `live.py` | Live engine orchestrator ($300, 3 coins) |
| `agents/risk_agent.py` | Deterministic gate — 10 checks, absolute floors |
| `agents/scoring.py` | Composite scorer (tech 35% + sent 15% + onchain 10% + AI 40%) |
| `agents/altfins_enrichment.py` | altFINS patterns, oversold, crossovers, TA, news |
| `agents/monitor_agent.py` | 7-layer exit strategy with hybrid ATR trailing |
| `agents/regime_engine.py` | Fear & Greed → regime → adaptive thresholds |
| `CLAUDE.md` | System reference (auto-loaded by Claude Code) |

## Risk Controls

### Absolute floors (cannot be overridden)
- Min signal score: **62** (75 during F&G < 20)
- Min AI confidence: **0.62**
- Max position: **1%** of portfolio (10% for sub-$1K accounts)
- Max open positions: **5**
- Daily loss limit: **5%**
- Weekly loss limit: **10%**

### altFINS gates (new)
- **News check**: veto entry if >40% of last 4h articles are negative
- **TA confirmation**: halve position size if altFINS TA disagrees with our direction
- **Patterns**: +12 pts to score for qualifying chart patterns (≥67% success rate)
- **Oversold in Uptrend**: +20 pts when RSI<30 + SMA200 UP + mcap>$100M
- **Crossover signals**: +6 to +12 pts per confirmed crossover (Golden Cross, EMA, MACD, RSI)

### Trailing stop (improved)
- Trail from highest CLOSE (not wick)
- Activate after profit ≥ 1.5×ATR
- Hybrid: start at 3×ATR, tighten to 2×ATR after +1.5R
- Regime-calibrated: low_vol=2.0×ATR, normal=2.5×, high_vol=3.5×
- Never widens — stop can only move up

## Monitoring

```bash
# Process status
ps aux | grep -E "main.py|live.py|altfins_shadow|ollama" | grep -v grep

# Launchd daemons
launchctl list | grep signalforge

# Recent signals
sqlite3 data/trades.db "SELECT * FROM signals_log ORDER BY timestamp DESC LIMIT 10"

# Open positions (Alpaca)
curl -s -H "APCA-API-KEY-ID: $ALPACA_KEY" -H "APCA-API-SECRET-KEY: $ALPACA_SECRET" \
  https://paper-api.alpaca.markets/v2/positions | python3 -m json.tool

# altFINS shadow data
sqlite3 live_trades.db "SELECT * FROM altfins_shadow ORDER BY captured_at DESC LIMIT 10"
```

## Troubleshooting

| Issue | Fix |
|---|---|
| main.py not restarting | `launchctl list \| grep v2` — if missing, `launchctl load ~/Library/LaunchAgents/com.signalforge.v2.plist` |
| Ollama OOM | `ollama ps` — if both models loaded, stop one: `ollama stop qwen3:14b` |
| Alpaca 401 | Check clock sync: `date` vs `date -u`. Restart the engine. |
| altFINS rate limit | Shadow logger has false-positive detection — ignore if data is flowing |
| Binance HTTP 451 | Normal — US geo-block. Coinbase fallback is automatic. |
| No trades executing | Check: F&G may be <20 (threshold raised to 75). Run `PYTHONPATH=. python scripts/backtest_report.py --days 1` |
