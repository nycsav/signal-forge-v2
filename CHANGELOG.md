# Changelog

All notable changes to Signal Forge v2. Reverse chronological. See `CLAUDE.md` for the system reference.

---

## Week of 2026-04-03 → 2026-04-09

### 2026-04-09

- **`5b8d237`** `Add local MCP server for Perplexity / MCP clients`
  Built `mcp_server.py` on `fastmcp 3.2.2`. Exposes 7 read-only tools over stdio: `get_trade_summary`, `get_open_positions`, `get_recent_signals`, `get_whale_events`, `get_system_health`, `get_risk_audit`, `run_morning_audit`. Perplexity acts as the read-only CTO/evaluator; Claude Code is the engineer.

- **`9b32354`** `Add CLAUDE.md — pipeline architecture, floor rules, week changelog`
  First-pass system reference. Superseded by the comprehensive rewrite later the same day.

- **`01054f7`** `RiskAgent: enforce absolute floors that RegimeEngine cannot override`
  Added `MIN_SIGNAL_SCORE_FLOOR = 62` and `MIN_AI_CONFIDENCE_FLOOR = 0.62` class constants. `_check_signal_threshold` and `_check_ai_confidence` now use `max(FLOOR, instance_value)`. Closes the bug where `main.py` and `live.py` were directly mutating `self.risk.MIN_SIGNAL_SCORE` from regime params, allowing the gate to drop to 40 during capitulation. Verified with 4 in-process unit tests.

### 2026-04-08

- **`994f943`** `Refactor live.py to use identical agent pipeline as main.py`
  Deleted `config/live_rules.py` (had separate score=55, confidence=0.50 thresholds). Removed all inline threshold checks from `live.py`. Wired live engine into the same `EventBus → AIAnalyst → RiskAgent → ExecutionAgent → MonitorAgent` pipeline as `main.py`. Whale trigger now direction-aware: bearish → 12hr buy block, bullish → confidence boost + immediate scan. Updated `live_dashboard.py` and `agents/dual_tracker.py` to import thresholds from `RiskAgent` class instead of deleted `live_rules`.

- **`5674bc1`** `WhaleTrigger: per-asset Arkham scanning + HIGH priority WhaleEvents`
  Added 15-min per-asset scan loop for top 10 watchlist (BTC, ETH, SOL, XRP, ADA, AVAX, LINK, DOT, DOGE, UNI). Publishes WhaleEvent at HIGH priority for >$1M entity-labeled moves.

- **`b807241`** `Nightly DeepSeek R1 analysis job + ChartPatternAgent`
  Added 2am cron via launchd (`com.signalforge.nightly-deepseek.plist`) that reads last 30 trades from `trade_outcomes`, sends to DeepSeek R1 14B, parses JSON weight recommendations into `logs/deepseek_nightly.json`. DeepSeek is *only* in the offline analysis path — never in the live signal chain.

- **`c86f392`** `ChartPatternAgent: IHS, H&S, Double Bottom detection via scipy`
  New agent runs every 4 hours, uses `scipy.signal.argrelextrema` to detect Inverse Head & Shoulders, Head & Shoulders, and Double Bottom patterns. Confidence = symmetry(40%) + depth(40%) + recency(20%). Publishes `PatternEvent` at HIGH priority when confidence ≥ 70%.

- **`51e9c02`** `Learning Agent guard rails: validation holdout + delta clamping`
  Added `MIN_TRADES_BEFORE_UPDATE = 20`, `VALIDATION_HOLDOUT_RATIO = 0.25`, `MAX_WEIGHT_DELTA = 0.15`. LearningAgent now refuses to update scoring weights unless validation Sharpe improves >5% on held-out trades. Prevents weight thrashing on noisy feedback.

- **`1580f59`** `Fix ATR: real calculation from price history, fallback 1.2% (was hardcoded 3%)`
  MonitorAgent now computes true ATR(14) from `closes[-14:]` instead of `entry * 0.03`. Fallback 1.2% when fewer than 15 candles available. Original 3% hardcode made TP1 unreachable in current low-vol market (TP1 was at +11.3%, TP1 should be ~+4.5%).

### 2026-04-07

- **`3cfbc00`** `Architecture overhaul — priority bus + 3-step AI + staleness checks`
  Major refactor implementing user-specified architecture patterns:
  - Priority EventBus with `IntEnum`: CRITICAL → HIGH → NORMAL → LOW queues
  - 3-step AI pipeline: Llama 3.2 pre-filter (15s) → Qwen3 14B full analysis (70s) → Llama 3.2 sanity check (15s)
  - Staleness checks: sentiment >30min or onchain >2h marks bundle stale and caps `max_allowed_confidence` at 0.65
  - Whale event direction filtering and bridge/relay noise removal

- **`1afb20a`** `Both engines live with whale trigger + trade rationale logging`
- **`56590c1`** `Whale Trigger — Arkham activity triggers immediate market scans`
- **`4715c03`** `Speed overhaul + trade logger — 3x faster, every trade recorded`
  Scan interval dropped to 5min. Every trade outcome recorded to `trade_outcomes` with full signal context (F&G, regime, RSI, confidence, consensus, Fib, Arkham) plus auto-generated lessons ("CHASED THE RALLY", "Non-consensus trade lost").
- **`38e3b58`** `7 trades executed into rally + 5-min scan interval`
- **`35ff4bf`** `MarketDataAgent pulls ALL sources — AI now sees the full market picture`
  MarketDataAgent now pulls Coinbase + CoinMarketCap + Arkham + altFINS + Fear & Greed in a single cycle. AI prompt now includes `MarketChange` and `Regime` fields.
- **`d0358db`** `CoinMarketCap API activated — market rally detected in real-time`
- **`9d45ceb`** `Dual Account Tracker — test signals on $100K paper + $300 live simultaneously`
- **`6373e1c`** `Trending Token Day Trader — same-day trade signals from CoinGecko + GeckoTerminal`
- **`8afc932`** `New token launch scanner with scam filtering`
- **`e616afa`** `Live engine dry-run test — aggressive strategy in action`
- **`9d622a5`** `Aggressive-with-guardrails live strategy`
- **`cfb3e10`** `Arkham intelligence live — smart money tracking operational`
- **`5187c34`** `Arkham Intelligence API key activated + client field fix`
- **`70bd8dd`** `Live trading engine + separate dashboard (port 8889)`
- **`be53647`** `Cleanup: fix all errors, organize, 16/16 endpoints, 11/11 tests, 35/35 modules`

### 2026-04-06

- **`19b15e4`** `Multi-timeframe Fibonacci engine v2 — research-validated`
  5 retracement levels (23.6%–78.6%) + 5 extension levels (127.2%–423.6%) across all timeframes. Confluence detection finds zones where 3+ TF levels cluster within 1%. Golden Pocket (61.8%) gets +8 score boost.
- **`18f5a93`** `Fibonacci retracement + extension levels for exit strategy`
- **`67c1b53`** `Update reporter: remove stale recommendations, reflect current state`
- **`1351fc0`** `AI Consensus dashboard panel + tracking endpoint`
- **`c0c2e2e`** `Dual-model consensus: Qwen3 14B + DeepSeek R1 14B`
- **`86242bf`** `Fix Qwen3 14B — fully operational, zero Llama fallbacks`
  Root cause: `num_predict=200` was being consumed by Qwen3's thinking tokens before any output JSON. Bumped to `num_predict=2000` and switched from `/api/chat` to `/api/generate` (chat endpoint had a regression).
- **`977494d`** `Integrate Arkham Intelligence — free on-chain smart money tracking`
- **`c3a78f0`** `Integrate full data stack: Binance + CoinGecko + DeFiLlama + Nansen`
- **`5ed1188`** `Switch to Qwen3 14B primary + Llama 3.2 fallback, Quarter-Kelly sizing`
- **`026ba2e`** `Update probability model with research-validated data`
- **`bad4b78`** `Probability Improvement Model — research-validated upgrade roadmap`
- **`89699d6`** `Fix all 5 MEDIUM issues — trades flowing, exits working, warmup fixed`
- **`65df6ca`** `Daily Journal tab — tracks all changes, insights, actions by date`
- **`1483bec`** `Fix all 3 HIGH priority issues — exits firing, trades closing`
  Hold time now read from real Alpaca `filled_at` timestamps (was resetting to 0h on every engine restart).
- **`d1fa5df`** `System Auditor + Dashboard Audit Panel`
- **`cb8b590`** `Fix Monitor Agent — rebuilt without DB dependency, port conflict removed`
  MonitorAgent rebuilt to read positions from Alpaca directly, hold state in memory. SQLite `busy_timeout=15s`, `WAL` mode. Dashboard moved off port 8000 to prevent conflict with engine.
- **`7a17297`** `Add per-project token usage tracker`
- **`91d8465`** `Activity Reporter + Token Usage + Dashboard Report Panel`

### 2026-04-05

- **`8037212`** `Expand watchlist to top 50 coins by market cap`
- **`bb45e02`** `Fix Monitor Agent DB errors + raise accumulation position limit`
  RiskAgent now queries live Alpaca position count via `/v2/positions` (was reading stale DB causing 20-vs-14 mismatches). Cached 30s.

### 2026-04-04

- **`b0977a1`** `Full pipeline live — first paper trade executed by AI agents`
- **`1301806`** `Regime Adaptive Engine + System Journal`
  RegimeAdaptiveEngine selects 7 regimes by Fear & Greed: capitulation, extreme_fear, fear, neutral, greed, extreme_greed, euphoria. Sets `score_threshold`, `ai_confidence_min`, `position_size_mult`, `max_positions`, `strategy`, `bias`, `stop_atr_mult`. Volatility overlay adjusts stop multiplier (1.5–3.5).
- **`3e357a1`** `Add backtest engine + API endpoint`
- **`fe7c9e3`** `Phase 5: Perplexity Sonar Integration + Full System Complete`
- **`5aad1a3`** `Phase 4: Learning Agent + Feedback Loop`

### 2026-04-03

- **`11f6a09`** `Phase 3: Execution Agent + Monitor Agent + ATR Exit Strategy`
- **`c1ca0d8`** `Phase 2: Multi-Agent Scoring Engine — 6 agents + orchestrator`
- **`8d3d3da`** `Phase 1: Infrastructure — Dashboard + Data Pipeline + Event Bus`

---

## Notes on conventions

- Every commit tagged here is reachable in `git log`. Use `git show <hash>` for the full diff.
- Hashes ending in shorter prefixes are short SHAs — pass them to `git show` directly.
- Co-authorship: every commit this week was paired with `Claude Opus 4.6 (1M context)`.
- Bug-fix commits include a one-line root cause in the body where known.
- "Fix" commits should reference the original symptom and the actual cause, not just "fixed it".
