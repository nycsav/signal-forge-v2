# SignalForge v2 — altFINS Edition — System Reference

This file is auto-loaded into every Claude Code session in this repo. Read it first.

---

## 1. Architecture

Both `main.py` (paper, $100K) and `live.py` (live, $300) run the **identical** agent pipeline. There is one EventBus, one set of agents, one threshold source.

```
                                ┌──────────────────┐
                                │  RegimeAdaptive  │  (params, never gates)
                                │      Engine      │
                                └────────┬─────────┘
                                         │ writes thresholds
                                         ▼
  MarketDataAgent  ──┐                                      ┌── ExecutionAgent ── Alpaca/Coinbase
       (5min)        │                                      │      (places order)
                     │   ┌──── EventBus (priority queue) ───┤
  TechnicalAgent  ───┤   │   CRITICAL → HIGH → NORMAL → LOW │
                     │   │                                  │
  SentimentAgent  ───┼───┴── SignalBundle ──► AIAnalyst ────┴── TradeProposal ──► RiskAgent
       (15min)       │                       (3-step LLM)                          (8 checks)
                     │                                                                  │
  OnChainAgent    ───┘                                                                  ▼
       (1hr)                                                                   RiskAssessmentEvent
                                                                              (APPROVED / VETOED)
                                                                                        │
                                                                                  if APPROVED
                                                                                        ▼
                                                                                ExecutionAgent
                                                                                        │
                                                                                        ▼
                                                                                OrderFilledEvent
                                                                                        │
                                                                                        ▼
                                                                                MonitorAgent
                                                                              (7-layer exit loop)
                                                                                        │
                                                                                        ▼
                                                                                TradeClosedEvent
                                                                                        │
                                                                                        ▼
                                                                                LearningAgent
                                                                            (guard-railed, weekly)

  Side channels (publish to same EventBus at HIGH priority):
    WhaleTrigger     — Arkham polls (60s global, 15min per-asset)
                       bullish → boost+scan, bearish → rolling 12h net-flow block
    ChartPatternAgent — every 4h, IHS / H&S / Double Bottom (scipy)

  altFINS Enrichment Layer (agents/altfins_enrichment.py):
    Background polling (no EventBus, direct cache):
      pattern_getCryptoPatternData      — every 4h, +12 pts for ≥67% success BUY patterns
      screener_getAltfinsScreenerData   — every 15m, +20 pts for Oversold-in-Uptrend
      signal_feed_data (crossovers)     — every 15m, +6 to +12 pts per crossover signal
    Pre-execution (called by RiskAgent per-trade):
      technicalAnalysis_getTechnicalAnalysisData — TA confirmation, halve size on disagree
      news_getCryptoNewsMessages                — veto on negative news (>40% in last 4h)
```

**Tier mapping**
- **Tier 1 (Strategic)** — orchestrator (`main.py` / `live.py`), assembles bundles, applies regime params
- **Tier 2 (Tactical)** — MarketData, Technical, Sentiment, OnChain, AIAnalyst
- **Tier 3 (Execution)** — Risk, Execution, Monitor, Learning

---

## 2. Absolute Rules

These are non-negotiable. Violating any of them is a bug.

### 2.1 RiskAgent floors are inviolable
```python
# agents/risk_agent.py
MIN_SIGNAL_SCORE_FLOOR = 62
MIN_AI_CONFIDENCE_FLOOR = 0.62
```
The threshold checks use `max(FLOOR, instance_value)`. RegimeEngine **may** assign lower instance values for sizing logic, but the floor always wins at the gate. RegimeEngine **may** also raise the threshold above the floor (e.g. 75 in euphoria) and that is respected.

If you ever need to bypass the floor, you are doing something wrong. Do not add a "test mode" flag, do not add a "dev override". Edit the floor constant if the project's risk tolerance has actually changed.

### 2.2 main.py and live.py must use identical pipelines
Same `RiskAgent` class, same `ExecutionAgent`, same `MonitorAgent`, same `EventBus` subscription pattern. Differences are restricted to:
- Database (`data/trades.db` vs `data/live_trades.db`)
- Watchlist size (50 coins vs 3 coins)
- Capital ($100K vs $300)
- `--dry-run` flag

Do **not** create `live_rules.py`, `paper_rules.py`, or any per-engine config file. That pattern was deleted on `2026-04-08` because it caused live.py to bypass RiskAgent entirely. If a fix applies to one engine, it applies to both — put the logic inside an agent, not inside the orchestrator.

### 2.3 No LLM makes execution decisions
LLMs (Qwen3, Llama, DeepSeek) generate **signals** — direction, score, confidence, rationale. Every signal must pass through `RiskAgent._on_proposal` before any order is placed. The gate is deterministic Python: floors, position counts, correlation, R:R, regime compatibility, daily/weekly loss limits. No LLM is in that path.

This rule includes Claude. **Claude Code does engineering only**: refactoring, debugging, building tools, writing docs. Claude does not pick coins, does not set thresholds, does not approve trades.

### 2.4 RiskGate is mandatory and pre-order
The order of operations is invariant:
```
SignalBundle → AIAnalyst → TradeProposal → RiskAgent → (APPROVED) → ExecutionAgent → Alpaca
```
ExecutionAgent only subscribes to `RiskAssessmentEvent` with `decision == APPROVED`. There is no path from `TradeProposal` directly to order placement. If you find such a path, it is a bug.

---

## 3. Key Commits

| Commit | Date | Description |
|---|---|---|
| `5b8d237` | 2026-04-09 | **MCP server for Perplexity** — 7 read-only tools (trade summary, open positions, recent signals, whale events, system health, risk audit, morning audit) over stdio via fastmcp 3.2.2 |
| `9b32354` | 2026-04-09 | First-pass CLAUDE.md (superseded by this file) |
| `01054f7` | 2026-04-09 | **RiskAgent absolute floors** — `MIN_SIGNAL_SCORE_FLOOR=62`, `MIN_AI_CONFIDENCE_FLOOR=0.62`. `_check_signal_threshold` and `_check_ai_confidence` use `max(FLOOR, instance_value)`. Closes the bug where RegimeEngine could drop the threshold to 40 during capitulation |
| `994f943` | 2026-04-08 | **live.py unified pipeline refactor** — deleted `config/live_rules.py`, removed all inline threshold checks from `live.py`, wired live engine into the same `EventBus → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent` chain as `main.py`. Whale trigger is now direction-aware (bearish=12hr block, bullish=scan+boost) |
| `5674bc1` | 2026-04-08 | **WhaleTrigger per-asset Arkham scanning** — adds 15-min scan loop for top 10 watchlist assets, publishes `WhaleEvent` at HIGH priority for >$1M entity-labeled moves |
| `b807241` | 2026-04-08 | Nightly DeepSeek R1 14B analysis job at 2am via launchd — reads last 30 trades, outputs JSON weight recommendations to `logs/deepseek_nightly.json` |
| `c86f392` | 2026-04-08 | ChartPatternAgent — IHS / H&S / Double Bottom via `scipy.signal.argrelextrema`, runs every 4h, publishes `PatternEvent` at HIGH priority |
| `51e9c02` | 2026-04-08 | **Learning Agent guard rails** — `MIN_TRADES_BEFORE_UPDATE=20`, 25% validation holdout, `MAX_WEIGHT_DELTA=0.15`. Weight updates rejected if validation Sharpe doesn't improve >5%. Prevents the LearningAgent from chasing random noise |
| `1580f59` | 2026-04-08 | **Real ATR calculation** — MonitorAgent now computes ATR(14) from recent price history (`closes[-14:]`) instead of the hardcoded `entry * 0.03` that was making TP1 unreachable. Fallback is 1.2% of entry |
| `3cfbc00` | 2026-04-07 | **Architecture overhaul** — Priority EventBus (CRITICAL→HIGH→NORMAL→LOW), 3-step AI pipeline (Llama pre-filter → Qwen3 → Llama sanity), sentiment >30min / onchain >2h staleness checks, whale event filtering |
| `4715c03` | 2026-04-07 | Speed overhaul: 5-min scan, every trade outcome logged with full signal context + auto-generated lessons |
| `35ff4bf` | 2026-04-07 | MarketDataAgent pulls all sources (Coinbase + CMC + Arkham + altFINS + F&G); AI prompt now sees `MarketChange` and `Regime` |

| `e14d0df` | 2026-04-13 | **User guide** — `docs/USER_GUIDE.md` for both v2 and lite |
| `65e370a` | 2026-04-13 | **Crossover signal scoring + trailing stop improvements** — 5 crossover types (SMA_50_200 +12, EMA_12_50 +8, EMA_100_200 +10, MACD_SIGNAL +6, RSI_14_CROSS_30 +8), trailing stop changes A-F, capitulation threshold override 62→75 |
| `7f19e78` | 2026-04-13 | **Accumulated fixes** — sizing tests, whale rolling 12h net-flow, altFINS direct trigger in market_data_agent, backtest scripts (historical + comparison) |
| `e0ed08a` | 2026-04-13 | **altFINS enrichment layer** — patterns (4h), oversold-in-uptrend (15m), TA confirmation (per-trade), news gate (per-trade), composite score altfins_bonus, sub-$1K sizing fix |

For the full list, see `CHANGELOG.md`.

---

## 4. Current Thresholds

### 4.1 RiskAgent (`agents/risk_agent.py`)
| Constant | Value | Notes |
|---|---|---|
| `MIN_SIGNAL_SCORE_FLOOR` | **62** | Absolute floor. Cannot be overridden. |
| `MIN_AI_CONFIDENCE_FLOOR` | **0.62** | Absolute floor. Cannot be overridden. |
| `CAPITULATION_SCORE_OVERRIDE` | **75** | Raised threshold when F&G < 20. Added 2026-04-13. |
| `MIN_SIGNAL_SCORE` | 62 | Class default; instance value gets overwritten by regime |
| `MIN_AI_CONFIDENCE` | 0.62 | Class default; instance value gets overwritten by regime |
| `MAX_OPEN_POSITIONS` | 5 | Hard cap on concurrent positions |
| `MAX_POSITION_PCT` | 0.01 | 1% per trade — Quarter-Kelly default |
| `HIGH_CONVICTION_PCT` | 0.015 | 1.5% when score ≥ 85 |
| `SMALL_ACCOUNT_THRESHOLD` | 1000.0 | Below this → flat 10% sizing path |
| `SMALL_ACCOUNT_POSITION_PCT` | 0.10 | 10% flat for sub-$1K accounts ($300→$30) |
| `MIN_ORDER_USD` | 10.0 | Coinbase minimum. Floor for tiny accounts. |
| `ALTFINS_DISAGREE_SIZE_MULT` | 0.50 | Halve position when altFINS TA disagrees |
| `MAX_SAME_GROUP` | 3 | Max correlated positions per sector |
| `MIN_RISK_REWARD` | 2.0 | Weighted TP ladder R:R minimum |
| `DAILY_LOSS_LIMIT` | 0.05 | 5% portfolio drawdown halts trading |
| `WEEKLY_LOSS_LIMIT` | 0.10 | 10% weekly drawdown halts trading |

**RiskAgent now runs 10 checks** (was 8): the original 8 deterministic checks plus two altFINS pre-execution gates:
- Check 9: `_check_altfins_news()` — vetoes if >40% of articles in last 4h are negative
- Check 10: `_apply_altfins_ta_adjustment()` — halves position size if altFINS TA disagrees with our direction

### 4.2 RegimeAdaptiveEngine (`agents/regime_engine.py`)
Regime is selected by Fear & Greed index. Each regime sets `score_threshold`, `ai_confidence_min`, `position_size_mult`, `max_positions`, `strategy`, `bias`, `stop_atr_mult`. **Note:** the floor in 4.1 clamps `score_threshold` and `ai_confidence_min` to 62 / 0.62 at the RiskAgent gate.

| Regime | F&G | score_thr | ai_conf | pos_size_mult | max_pos | strategy | bias |
|---|---|---|---|---|---|---|---|
| `capitulation` | <10 | 40 | 0.35 | 0.5x | 15 | accumulate | long_only |
| `extreme_fear` | 10-25 | 45 | 0.40 | 0.5x | 15 | accumulate | long_bias |
| `fear` | 25-45 | 50 | 0.42 | 0.75x | 5 | mean_reversion | long_bias |
| `neutral` | 45-55 | 55 | 0.45 | 1.0x | 5 | momentum | neutral |
| `greed` | 55-70 | 60 | 0.50 | 0.75x | 4 | momentum | neutral |
| `extreme_greed` | 70-85 | 70 | 0.55 | 0.5x | 3 | defensive | short_bias |
| `euphoria` | >85 | 80 | 0.65 | 1.0x | 5 | defensive | short_bias |

Volatility overlay: `avg_atr_pct > 6%` → `stop_atr_mult=3.5`; `< 1.5%` → `stop_atr_mult=1.5`; else `2.5`.

**Currently active:** `capitulation` (F&G=14), `score_threshold=40` (clamped to 62 at gate), `position_size_mult=0.5x`, `max_positions=15`.

### 4.3 WhaleTrigger (`agents/whale_trigger.py`)
| Setting | Value |
|---|---|
| `GLOBAL_POLL_INTERVAL` | 60 seconds |
| `ASSET_POLL_INTERVAL` | 900 seconds (15 min) |
| `MIN_GLOBAL_USD` | $2,000,000 |
| `MIN_ASSET_USD` | $500,000 |
| `MIN_EVENT_USD` | $1,000,000 |
| Bearish block duration | 12 hours |
| Cooldown between fires | 120 seconds |
| Bridge tag filter | bridge, wrapped, relay, cross-chain, wormhole, layerzero |

Top 10 per-asset watchlist: BTC, ETH, SOL, XRP, ADA, AVAX, LINK, DOT, DOGE, UNI.

### 4.4 Position Sizing — Half-Kelly with Conviction Scaling
```python
win_prob   = 0.40 + (raw_score / 100) * 0.35
b          = weighted_TP_reward / risk        # weighted across TP1/TP2/TP3
full_kelly = win_prob - (1-win_prob)/b
half_kelly = max(0, full_kelly / 2)

conviction = 1.00 if score >= 85
             0.75 if score >= 70
             0.50 otherwise

ai_mod  = 0.70 + (ai_confidence * 0.30)
final   = clip(half_kelly * conviction * ai_mod, 0.005, MAX_POSITION_PCT)
```

### 4.5 MonitorAgent Exit Layers (`agents/monitor_agent.py`)
1. **Hard stop** — entry − ATR × 2.5
2. **ATR trailing stop** — improved 2026-04-13, see below
3. **TP1** — +1.5R, close 33%, move stop to breakeven
4. **TP2** — +3R, close 33% of remaining
5. **TP3** — +5R, close all
6. **Time exit** — 72h max, 48h if flat (±0.5%)
7. **Signal degradation** — exit if rescore < 30 (checked every 6 cycles)

**Trailing stop improvements (2026-04-13):**
- **(A) Trail from highest CLOSE** — high watermark updated at scan cycle only, not from wick high
- **(B) ATR activation distance** — trailing doesn't start until profit ≥ 1.5×ATR(14). Fallback 5% fixed if ATR unavailable (was 1.2%)
- **(C) Hybrid ATR trailing** — start at 3×ATR distance, tighten to 2×ATR after profit reaches +1.5R
- **(D) Regime-calibrated alpha** — `low_vol=2.0`, `normal=2.5`, `high_vol=3.5` (based on avg ATR% across positions)
- **(E) No-widening stops** — once a stop is set, it can only move UP (for longs): `new_stop = max(old_stop, calculated_stop)`
- **(F) Capitulation threshold override** — score threshold raised from 62→75 when F&G < 20 (implemented in RiskAgent `_check_signal_threshold`)

---

## 5. Data Sources & Priority

| Priority | Source | Role | Cost |
|---|---|---|---|
| **1 — Highest** | **Arkham Intelligence** | Whale flows, smart money tracking, exchange in/out. **Leading indicator** — fires HIGH priority `WhaleEvent` that triggers immediate market scans on bullish, 12hr buy block on bearish | Free (API key active) |
| 2 | **CoinGecko (free)** | OHLCV warmup, trending tokens, market cap context | Free |
| 3 | **CoinMarketCap** | Real-time market cap change, volume spikes | Free tier (key active) |
| 4 | **Coinbase Advanced** | Live spot prices, sub-minute candles, order book | Free (key active) |
| 5 | **altFINS** | Enrichment layer: chart patterns, crossover signals, oversold-in-uptrend screener, TA confirmation, news sentiment. 14 MCP tools available. Credit budget: **65K/month, current usage ~9,120/month** | Free tier |
| 6 | **Alternative.me F&G** | Fear & Greed index | Free |
| 7 | **DeFiLlama** | TVL, protocol yields, chain TVL | Free |
| **Execution** | **Alpaca Paper API** | Paper account ($100K) — main.py | Free |
| **Execution** | **Coinbase Advanced** | Future live execution path ($300+) — not yet wired | Free |
| Pending | Nansen | On-chain labels, smart money cohorts | $49/mo (no key yet) |
| Pending | Perplexity Sonar | Narrative detection, macro context | API key needed |

**Rule:** if Arkham and a price-based indicator disagree, **Arkham wins**. Whale flows lead price by minutes-to-hours. The price chart is a lagging confirmation.

---

## 6. Model Roles

| Model | Where it runs | Role | Decision authority |
|---|---|---|---|
| **Llama 3.2 3B** | Local Ollama | **Step 1** — pre-filter (`setup_quality: strong/weak/none`) AND **Step 3** — sanity check (`agrees: true/false`) on Qwen3's output | Signal generation only |
| **Qwen3 14B** | Local Ollama | **Step 2** — primary signal generation. Outputs `direction`, `score`, `ai_confidence`, `rationale` | Signal generation only |
| **DeepSeek R1 14B** | Local Ollama | Nightly alpha mining only. Cron job at 2am reads last 30 trades, outputs JSON weight recommendations to `logs/deepseek_nightly.json`. **Not in the live signal path.** | Offline analysis only |
| **Claude Code** (you) | Anthropic cloud | Engineering: refactoring, debugging, building tools, writing docs, running audits | **Engineering only — never trading decisions** |
| **Perplexity** (CTO/evaluator) | External | Reads MCP server tools to monitor system state, evaluate Claude's work, ask diagnostic questions | Read-only oversight |

**The chain of authority for any trade:**
```
Llama (filter) → Qwen3 (signal) → Llama (sanity) → RiskAgent (deterministic gate) → Alpaca
```
No LLM is in the gate. The gate is plain Python with hard floors.

---

## 7. Known Issues

1. **Alpaca 401 on open positions query (auth issue)** — intermittent 401 response from `/v2/positions` even with valid keys. Suspected: clock skew or token rotation timing. Workaround: retry once. Real fix: investigate Alpaca's auth header format or move to OAuth.

2. **MonitorAgent TP exits not yet validated under new ATR** — the real ATR(14) calculation landed on Apr 8 (`1580f59`), but no closed trade has hit TP1/TP2/TP3 since. All recent exits are still `time_72h` or `flat_48h`. Need 5-10 trades through the new TP path before declaring it working.

3. **DeepSeek not wired into live signal generation** — only runs in the nightly alpha-mining job. Original spec called for DeepSeek as a third model in the consensus chain. Decision deferred until Qwen3+Llama latency is acceptable (currently ~70s end-to-end). Adding DeepSeek (45-60s) would push a single signal cycle past 2 minutes.

4. **~~$300 account position sizing~~ RESOLVED 2026-04-13** — sub-$1K accounts now use flat 10% of equity (`SMALL_ACCOUNT_POSITION_PCT=0.10`). $300 account → $30 order. $50 account → $10 (MIN_ORDER_USD floor). Accounts ≥$1K keep Half-Kelly. Tests: `test_small_account_300_dollars_sizes_to_30`, `test_small_account_50_dollars_floors_at_min_order`, `test_large_account_1k_uses_kelly_path`.

5. **Stale main.py processes survive launchctl restart** — observed twice this week. After `launchctl stop com.signalforge.v2`, child processes from the previous run remain. Manual `kill` required. Root cause unknown — possibly Python multi-process from a library, or signal handler issue in `main.py`.

6. **Whale trigger "+20% confidence boost" is informational only** — `_on_whale_signal` logs the boost but doesn't actually plumb it into the AIAnalyst prompt or scoring. Needs cross-agent state passing.

7. **`_whale_events` rolling window does not persist across restarts** — replaced the single-event `_bearish_block_until` with a rolling 12h net-flow model on 2026-04-10, but the `_whale_events` list still lives in memory only. If you restart `live.py` during an active block, it clears. Should be persisted to `live_repo`.

---

## 8. MCP Server

**Location:** `~/signal-forge-v2/mcp_server.py`
**Library:** `fastmcp 3.2.2`
**Transport:** stdio
**Start command:**
```bash
cd ~/signal-forge-v2 && source venv/bin/activate && python mcp_server.py
```

For MCP client configuration (Perplexity, Claude Desktop, etc.):
```json
{
  "mcpServers": {
    "signal-forge": {
      "command": "/Users/sav/signal-forge-v2/venv/bin/python",
      "args": ["/Users/sav/signal-forge-v2/mcp_server.py"]
    }
  }
}
```

### Tools exposed (all read-only)

| Tool | Purpose |
|---|---|
| `get_trade_summary()` | Last 24h: trades opened/closed, win rate, total P&L from both DBs, RiskAgent approvals/vetoes |
| `get_open_positions()` | All currently open positions from Alpaca with unrealized P&L and hold time |
| `get_recent_signals(limit=20)` | Last N rows from `signals_log` with score, confidence, direction, decision, regime |
| `get_whale_events(hours=24)` | Whale trigger events in window with direction, USD, entity, chain |
| `get_system_health()` | Engine PIDs, last event bus activity, current F&G+regime, errors in last hour |
| `get_risk_audit()` | RiskAgent thresholds (floors + class defaults), 24h veto rate, top veto reasons, regime state |
| `run_morning_audit()` | Consolidated dashboard — calls all of the above |

### Roles
- **Perplexity** — CTO / evaluator. Reads MCP tools to monitor system state, audit Claude's work, ask diagnostic questions. Has no write access. Cannot place trades, change thresholds, or restart engines.
- **Claude Code** — Engineer. Edits code, writes tests, runs migrations, fixes bugs, deploys. Reports up to Perplexity. Does not make trading decisions.

This separation matters: the evaluator and the implementer are different agents so neither can rubber-stamp the other.

---

## 9. Process Hygiene

Only one paper engine and one live engine should run at a time. After any restart:
```bash
ps aux | grep -E "live\.py|main\.py" | grep -v grep
```
If you see more than 2 lines, kill the duplicates. Multiple instances writing to the same SQLite DB cause race conditions and inflated event counts.

When editing existing code, read **only** the specific function or class you need to change. Do not re-read whole files unless investigating cross-cutting behavior.

---

## 10. altFINS Enrichment Layer

**File:** `agents/altfins_enrichment.py`
**Class:** `AltFINSEnrichment`
**Started by:** both `main.py` and `live.py` in their `run()` methods.

### 10.1 Background Polling (no EventBus, direct MCP calls)

| Feature | MCP Tool | Poll interval | Score bonus | Credit est/mo |
|---|---|---|---|---|
| Chart patterns | `pattern_getCryptoPatternData` | 4h | +12 pts (≥67% success, BUY) | ~180 |
| Oversold in Uptrend | `screener_getAltfinsScreenerData` | 15min | +20 pts (RSI<30 + SMA200 UP + mcap>$100M) | ~2,880 |
| SMA 50/200 Golden Cross | `signal_feed_data` | 15min | +12 pts | ~2,880 (shared) |
| EMA 12/50 crossover | `signal_feed_data` | 15min | +8 pts | (shared) |
| EMA 100/200 crossover | `signal_feed_data` | 15min | +10 pts | (shared) |
| MACD signal crossover | `signal_feed_data` | 15min | +6 pts | (shared) |
| RSI 14 exits oversold | `signal_feed_data` | 15min | +8 pts | (shared) |

All bonuses are **additive** to the composite score via `scoring.py`'s `altfins_bonus` parameter. Max capped at 35 pts total (prevents a single enrichment source from dominating).

### 10.2 Pre-Execution Checks (per-trade, called by RiskAgent)

| Feature | MCP Tool | Cache TTL | Action | Credit est/mo |
|---|---|---|---|---|
| TA confirmation | `technicalAnalysis_getTechnicalAnalysisData` | 5 min | Halve size on disagree; log on agree | ~150 |
| News sentiment | `news_getCryptoNewsMessages` | 5 min | Veto if >40% of last-4h articles negative | ~150 |

### 10.3 Credit Budget

**Target:** 65,000 credits/month
**Current estimated usage:** ~9,120 credits/month (14% of budget)
**Headroom for expansion:** ~56K credits available

### 10.4 altFINS MCP Tools Available (14 total)

```
screener_getAltfinsScreenerData      — primary screening engine
screener_getAltfinsScreenerDataTypes — available field IDs
signal_feed_data                     — trading signals (bullish/bearish)
pattern_getCryptoPatternData         — chart pattern signals
technicalAnalysis_getTechnicalAnalysisData — curated TA summary
analytics_getAnalyticsTypes          — available analytics types
analytics_getAllLatestHistoryData     — latest indicator values
analytics_getHistoryData             — historical indicator time series
ohlc_getLatestData                   — latest OHLCV
ohlc_getHistoryData                  — historical OHLCV
news_getCryptoNewsMessages           — crypto news articles
news_getCryptoNewsSources            — available news sources
getCryptoCalendarEvents              — upcoming events (AMAs, listings)
getUserPortfolio                     — user portfolio (not used)
```

---

## 11. Backtest Framework

### 11.1 Historical Backtest (`scripts/historical_backtest.py`)
- 90-day replay of our scoring pipeline on real Coinbase OHLCV
- Runs at thresholds 55, 62, 68
- 531 trades at thr=55, 52.5% win, Sharpe 19.43 (from 2026-04-10 run)
- Uses Coinbase Exchange public API (Binance is HTTP 451 from US)
- Writes to `data/backtest_trades.db`

### 11.2 altFINS Comparison (`scripts/altfins_comparison.py`)
- 3-variant comparison: our-signals vs altfins-only vs combined
- **Status:** Variants B and C produce 0 trades because altFINS free tier returns no historical signals via `signal_feed_data`. The API is real-time only.
- **Path forward:** `altfins_shadow.py` is accumulating data 24/7 via launchd. Re-run the comparison in 2-4 weeks when we have enough signal history.

### 11.3 Backtest Report (`scripts/backtest_report.py`)
```bash
PYTHONPATH=. python scripts/backtest_report.py
PYTHONPATH=. python scripts/backtest_report.py --db data/altfins_comparison.db
PYTHONPATH=. python scripts/backtest_report.py --days 7 --regime capitulation
```

---

## 12. Role Split

| Agent | Owns | Does NOT do |
|---|---|---|
| **Claude Code** | All local code in `~/signal-forge-v2/` and `~/signalforge-lite/`. Edits agents, writes tests, runs backtests, manages git, deploys via launchd, builds dashboards. | Pick coins, set thresholds, approve trades, access .env/secrets |
| **Perplexity Computer** | Research, intelligence feeds, strategy docs (written to `~/signal-forge/docs/`). Evaluates Claude's work via MCP server. Designs altFINS integration strategies. | Edit code, restart engines, commit to git, place orders |

**Strategy docs location:** `~/signal-forge/docs/` — files authored by Perplexity Computer. Claude reads these as specs but does not write to that directory.

---

## 13. Sibling Project: signalforge-lite

**Repo:** `nycsav/signalforge-lite` — `~/signalforge-lite/`
**Purpose:** lean, backtest-first trading system using altFINS as primary intelligence. No LLM, no EventBus, no multi-agent orchestration. Tests the hypothesis: "Are altFINS signals good enough on their own?"

Completely independent from signal-forge-v2 — no shared code, no shared imports, separate git repo, separate venv. Can be deleted without affecting production.

---

## 14. Launchd Daemons

| Plist | Label | Engine | KeepAlive |
|---|---|---|---|
| `com.signalforge.v2.plist` | `com.signalforge.v2` | `main.py` (paper) | ✅ |
| `com.signalforge.live.plist` | `com.signalforge.live` | `live.py` (live $300) | ✅ |
| `com.signalforge.altfins-shadow.plist` | `com.signalforge.altfins-shadow` | `altfins_shadow.py` | ✅ |
| `com.signalforge.ollama.plist` | `com.signalforge.ollama` | `ollama serve` | ✅ |
| `com.signalforge.nightly-deepseek.plist` | `com.signalforge.nightly-deepseek` | DeepSeek cron (2am) | cron |

All plists live in `~/Library/LaunchAgents/`. Load/unload with `launchctl load/unload <path>`.
