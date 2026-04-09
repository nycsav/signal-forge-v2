# Signal Forge v2 — System Reference

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
                       bullish → boost+scan, bearish → 12hr buy block
    ChartPatternAgent — every 4h, IHS / H&S / Double Bottom (scipy)
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

For the full list, see `CHANGELOG.md`.

---

## 4. Current Thresholds

### 4.1 RiskAgent (`agents/risk_agent.py`)
| Constant | Value | Notes |
|---|---|---|
| `MIN_SIGNAL_SCORE_FLOOR` | **62** | Absolute floor. Cannot be overridden. |
| `MIN_AI_CONFIDENCE_FLOOR` | **0.62** | Absolute floor. Cannot be overridden. |
| `MIN_SIGNAL_SCORE` | 62 | Class default; instance value gets overwritten by regime |
| `MIN_AI_CONFIDENCE` | 0.62 | Class default; instance value gets overwritten by regime |
| `MAX_OPEN_POSITIONS` | 5 | Hard cap on concurrent positions |
| `MAX_POSITION_PCT` | 0.01 | 1% per trade — Quarter-Kelly default |
| `HIGH_CONVICTION_PCT` | 0.015 | 1.5% when score ≥ 85 |
| `MAX_SAME_GROUP` | 3 | Max correlated positions per sector |
| `MIN_RISK_REWARD` | 2.0 | Weighted TP ladder R:R minimum |
| `DAILY_LOSS_LIMIT` | 0.05 | 5% portfolio drawdown halts trading |
| `WEEKLY_LOSS_LIMIT` | 0.10 | 10% weekly drawdown halts trading |

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
2. **ATR trailing stop** — activated at +ATR × 1.5, trails from highest close
3. **TP1** — +1.5R, close 33%, move stop to breakeven
4. **TP2** — +3R, close 33% of remaining
5. **TP3** — +5R, close all
6. **Time exit** — 72h max, 48h if flat (±0.5%)
7. **Signal degradation** — exit if rescore < 30 (checked every 6 cycles)

---

## 5. Data Sources & Priority

| Priority | Source | Role | Cost |
|---|---|---|---|
| **1 — Highest** | **Arkham Intelligence** | Whale flows, smart money tracking, exchange in/out. **Leading indicator** — fires HIGH priority `WhaleEvent` that triggers immediate market scans on bullish, 12hr buy block on bearish | Free (API key active) |
| 2 | **CoinGecko (free)** | OHLCV warmup, trending tokens, market cap context | Free |
| 3 | **CoinMarketCap** | Real-time market cap change, volume spikes | Free tier (key active) |
| 4 | **Coinbase Advanced** | Live spot prices, sub-minute candles, order book | Free (key active) |
| 5 | **altFINS** | Aggregated technical signal score | Free tier |
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

4. **$300 account position sizing needs recalibration** — `MAX_POSITION_PCT=0.01` gives a $3 position on $300, below `MIN_ORDER_USD=$10`. Live engine currently can't open trades on the $300 account at all. Need a separate sizing path for sub-$1K accounts or raise position % to ~10% for tiny accounts. Decision pending.

5. **Stale main.py processes survive launchctl restart** — observed twice this week. After `launchctl stop com.signalforge.v2`, child processes from the previous run remain. Manual `kill` required. Root cause unknown — possibly Python multi-process from a library, or signal handler issue in `main.py`.

6. **Whale trigger "+20% confidence boost" is informational only** — `_on_whale_signal` logs the boost but doesn't actually plumb it into the AIAnalyst prompt or scoring. Needs cross-agent state passing.

7. **`_bearish_block_until` does not persist across restarts** — lives on the engine instance only. If you restart `live.py` during a 12hr block, it clears. Should be persisted to `live_repo`.

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
