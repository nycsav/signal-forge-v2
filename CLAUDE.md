# Signal Forge v2 — Project Conventions

## Agent Pipeline Architecture

Both `main.py` (paper, $100K) and `live.py` (live, $300) use the **identical** pipeline:

```
EventBus → MarketDataAgent → TechnicalAgent → SignalBundle
       → AIAnalystAgent (3-step: Llama pre-filter → Qwen3 → Llama sanity)
       → TradeProposal → RiskAgent (8 checks)
       → RiskAssessmentEvent(APPROVED) → ExecutionAgent → Alpaca
       → OrderFilledEvent → MonitorAgent (7-layer exit)
       → TradeClosedEvent → LearningAgent
```

Side channels:
- `WhaleTrigger` polls Arkham every 60s (global) and 15min (per-asset). Bullish → boost+scan. Bearish → 12hr buy block. Wired into the same EventBus at `Priority.HIGH`.
- `RegimeAdaptiveEngine` updates strategy params (threshold, sizing, stops, max positions) per market state.
- `ChartPatternAgent` runs every 4h, scans for IHS / H&S / Double Bottom via `scipy.signal.argrelextrema`.

## Absolute Floor Thresholds (RiskAgent)

These are hard floors enforced inside `RiskAgent._check_signal_threshold` and `_check_ai_confidence`. **They cannot be lowered by RegimeEngine, config, or any caller.**

| Floor | Value | Defined in |
|---|---|---|
| `MIN_SIGNAL_SCORE_FLOOR` | **62** | `agents/risk_agent.py` |
| `MIN_AI_CONFIDENCE_FLOOR` | **0.62** | `agents/risk_agent.py` |

The check pattern:
```python
threshold = max(self.MIN_SIGNAL_SCORE_FLOOR, self.MIN_SIGNAL_SCORE)
if p.raw_score < threshold:
    return False, ...
```

RegimeEngine can write to `self.risk.MIN_SIGNAL_SCORE` (e.g. `40` during capitulation), but the `max()` clamps it back up. RegimeEngine **can** raise the threshold above the floor (e.g. `75` during euphoria) — that's still respected.

Other risk parameters that RegimeEngine **does** still control freely (no floor):
- `MAX_OPEN_POSITIONS` — sizing/exposure
- `MAX_POSITION_PCT` — sizing
- `stop_atr_mult` — exit aggression

## Pipeline Identity Rule

`main.py` and `live.py` **must** use the same RiskAgent class, the same agent imports, and the same threshold logic. Differences are restricted to:
- Database (`trades.db` vs `live_trades.db`)
- Watchlist size (50 coins vs 3 coins for live)
- Capital ($100K vs $300)
- `--dry-run` flag

If a fix is applied to `main.py`, it must also apply to `live.py` (or be implemented inside an agent that both engines use). Do not duplicate threshold or sizing logic in either file. Do not create separate `live_rules.py` / `paper_rules.py` files — that pattern was deleted on `2026-04-08` for exactly this reason.

When adding a new agent, wire it via the EventBus subscription model in both `__init__` methods and follow the same `bus.subscribe(EventType, handler)` pattern.

## Process Hygiene

Only one paper engine (PID from `launchctl`) and one live engine (`python live.py --dry-run`) should run at a time. After any restart, verify with:
```bash
ps aux | grep -E "live\.py|main\.py" | grep -v grep
```
Multiple instances writing to the same SQLite DB will cause race conditions and inflated event counts. Stale processes from prior `launchctl stop` calls have been observed — kill them explicitly if they survive a restart.

## File Reading Convention

When editing existing code, read **only** the specific function or class you need to change. Do not re-read the whole file unless investigating cross-cutting behavior.

## Changelog — Week of 2026-04-03 to 2026-04-09

| Date | Commit | Fix |
|---|---|---|
| 2026-04-09 | `01054f7` | RiskAgent absolute floors (`MIN_SIGNAL_SCORE_FLOOR=62`, `MIN_AI_CONFIDENCE_FLOOR=0.62`) — RegimeEngine can no longer drop thresholds below the floor |
| 2026-04-08 | `994f943` | Refactor `live.py` to use identical pipeline as `main.py`. Deleted `config/live_rules.py`. All thresholds now from `RiskAgent`. Whale trigger made direction-aware (bearish=12hr block, bullish=boost+scan) |
| 2026-04-08 | `5674bc1` | WhaleTrigger per-asset Arkham scanning + HIGH priority WhaleEvents on >$1M entity-labeled moves |
| 2026-04-08 | `b807241` | Nightly DeepSeek R1 14B analysis job at 2am via launchd, reads last 30 trades, outputs JSON weight recommendations |
| 2026-04-08 | `c86f392` | ChartPatternAgent: detects IHS, H&S, Double Bottom via `scipy.signal.argrelextrema`, runs every 4h |
| 2026-04-08 | `51e9c02` | LearningAgent guard rails: `MIN_TRADES_BEFORE_UPDATE=20`, 25% validation holdout, `MAX_WEIGHT_DELTA=0.15`, rejects updates if validation Sharpe doesn't improve >5% |
| 2026-04-07 | `1580f59` | MonitorAgent ATR fix: real ATR(14) calculation from price history, fallback to 1.2% (was hardcoded 3%, made TP1 unreachable) |
| 2026-04-07 | `3cfbc00` | Architecture overhaul — `Priority` event bus (CRITICAL→HIGH→NORMAL→LOW), 3-step AI pipeline, sentiment >30min / onchain >2h staleness checks |
| 2026-04-07 | `4715c03` | Speed overhaul: 5-min scan interval, every trade outcome recorded via `trade_logger` with full signal context + auto-generated lessons |
| 2026-04-07 | `35ff4bf` | MarketDataAgent pulls all sources (Coinbase + CMC + Arkham + altFINS + F&G), AI prompt now sees `MarketChange` and `Regime` |
| 2026-04-06 | `9d45ceb` | DualTracker: same signal → two trades at different sizes ($100K paper + $300 live) |
| 2026-04-06 | `5187c34` | Arkham Intelligence API key wired into WhaleTrigger client |
| 2026-04-05 | `89699d6` | Fix 5 MEDIUM issues: trades flowing, exits working, warmup fixed |
| 2026-04-05 | `cb8b590` | MonitorAgent rebuilt: reads from Alpaca directly, in-memory state, SQLite `busy_timeout=15s`, `WAL` mode |
| 2026-04-05 | `bb45e02` | RiskAgent now queries live Alpaca position count (was reading stale DB; cached 30s) |
| 2026-04-05 | `1483bec` | Hold time read from real Alpaca `filled_at` timestamps (was resetting to 0h on restart) |
| 2026-04-04 | `c1ca0d8` | Phase 2: 6 tactical agents + orchestrator with typed Pydantic event bus |
| 2026-04-03 | `8d3d3da` | Phase 1: dashboard + data pipeline + event bus |

### Known Open Issues
- Live engine `_on_whale_signal` triggers `market_data._scan_all()` directly on bullish whales, but the 20% confidence boost it logs is not yet plumbed into `AIAnalystAgent` — the boost is informational only.
- Bearish whale block sets `_bearish_block_until` on the engine instance only; it does not persist across restarts.
- Multiple `main.py` zombie processes have been observed surviving `launchctl stop` calls. Manual cleanup required.
