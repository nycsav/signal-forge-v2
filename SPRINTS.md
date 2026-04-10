# Signal Forge v2 — Sprint Tracker

Updated: 2026-04-09

---

## Sprint 1 — THIS WEEK (Apr 9-13)

Goal: Make live trading mechanically possible and start collecting backtest data.

| # | Task | File | Status |
|---|---|---|---|
| 1.1 | Fix $300 position sizing bug | `agents/risk_agent.py` | 🟡 Ready for Claude Code (see `scripts/fix_sizing.md`) |
| 1.2 | Wire altFINS `signal_feed_data` as direct scan trigger | `agents/market_data_agent.py` | 🔴 Not started |
| 1.3 | Run `backtest_report.py` after 20 closed paper trades | `backtest_report.py` | 🔴 Waiting on trades |
| 1.4 | Add `pattern_getCryptoPatternData` to `altfins_shadow.py` | `altfins_shadow.py` | 🔴 Not started |
| 1.5 | Fix `_bearish_block_until` persistence across restarts | `db/repository.py` + `live.py` | 🔴 Not started |
| 1.6 | Validate whale confidence boost is actually plumbed in | `agents/whale_trigger.py` | 🔴 Known Issue #6 |

---

## Sprint 2 — NEXT WEEK (Apr 14-20)

Goal: Expand watchlist dynamically, replace scipy patterns, connect Perplexity dashboard.

| # | Task | File | Status |
|---|---|---|---|
| 2.1 | Tiered watchlist: Tier 1 (8) + Tier 2 (top 50 by volume) + Tier 3 (signal-triggered) | `agents/market_data_agent.py` | 🔴 Not started |
| 2.2 | Replace `ChartPatternAgent` scipy with `pattern_getCryptoPatternData` | `agents/chart_pattern_agent.py` | 🔴 Not started |
| 2.3 | Add altFINS trend score + RSI as AIAnalyst prompt context field | `agents/ai_analyst.py` | 🔴 Not started |
| 2.4 | Alt/BTC pairs: add ETH/BTC, SOL/BTC to watchlist | `config/` | 🔴 Not started |
| 2.5 | Perplexity desktop dashboard — confirm MCP tools return live data | `mcp_server.py` | 🔴 Not started |
| 2.6 | Add `get_altfins_snapshot()` tool to MCP server | `mcp_server.py` | 🔴 Not started |
| 2.7 | New token detection: CoinGecko trending endpoint → auto-add to Tier 3 | `agents/market_data_agent.py` | 🔴 Not started |

---

## Sprint 3 — WEEK 3 (Apr 21-27)

Goal: Go live on Coinbase with real capital. 5-50 trades/day target.

| # | Task | File | Status |
|---|---|---|---|
| 3.1 | Wire Coinbase Advanced execution in `live.py` | `agents/execution_agent.py` | 🔴 Not started |
| 3.2 | Raise live capital to $1K+ | External | 🔴 Pending Sprint 1 results |
| 3.3 | Lower signal score floor from 62 → 55 on paper first, validate with backtest | `agents/risk_agent.py` | 🔴 Pending backtest results |
| 3.4 | Rolling 12h whale net-flow model (replace single-event override) | `live.py` | 🔴 Pending (see prior discussion) |
| 3.5 | DeepSeek R1 into live signal consensus chain | `agents/ai_analyst.py` | 🔴 Pending latency validation |
| 3.6 | `backtest_report.py` — regime parameter optimisation pass | `backtest_report.py` | 🔴 Pending 50+ closed trades |
| 3.7 | Fix Alpaca 401 intermittent auth issue | `agents/execution_agent.py` | 🔴 Known Issue #1 |

---

## Daily Checklist (run every morning)

```bash
# 1. Check system is running
ps aux | grep -E "live\.py|main\.py" | grep -v grep

# 2. Check altFINS shadow is running  
ps aux | grep altfins_shadow | grep -v grep

# 3. Run backtest report
python backtest_report.py

# 4. Check quota
source .env && curl -H "X-Api-Key: $ALTFINS_API_KEY" \
  https://altfins.com/api/v2/public/all-available-permits

# 5. Check paper P&L via MCP
# (ask Perplexity: "run morning audit")
```

---

## Credit Budget (altFINS Hobbyist — 100k/month)

| Usage | Calls/month | % of quota |
|---|---|---|
| Shadow logger (10 min, 3 calls/cycle) | ~13,000 | 13% |
| Pattern data (1x/hour per 8 symbols) | ~5,800 | 6% |
| Signal triggers (on-demand) | ~2,000 | 2% |
| Manual / discovery queries | ~1,000 | 1% |
| **Total projected** | **~22,000** | **22%** |
| **Remaining buffer** | **~78,000** | **78%** |
