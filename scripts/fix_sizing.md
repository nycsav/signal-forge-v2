# Claude Code Task: Fix $300 Account Position Sizing

## Problem

`MAX_POSITION_PCT = 0.01` (1%) on a $300 account = $3 per trade.
Coinbase minimum order = $10.
Result: live.py cannot open any trades.

## Fix Required

In `agents/risk_agent.py`, add a dynamic minimum position sizing path:

```python
# In RiskAgent.__init__ or wherever position size is computed:
MIN_ORDER_USD = 10.0   # exchange minimum

def _compute_position_size(self, portfolio_value: float, score: float, ai_confidence: float) -> float:
    # existing Half-Kelly logic here...
    size_usd = portfolio_value * final_pct
    
    # Sub-$1K accounts: enforce minimum viable order
    if portfolio_value < 1000:
        min_pct = MIN_ORDER_USD / portfolio_value  # e.g. 10/300 = 3.3%
        # Cap at 10% max for tiny accounts
        sub1k_pct = min(0.10, max(min_pct, final_pct))
        size_usd = portfolio_value * sub1k_pct
    
    return max(size_usd, MIN_ORDER_USD)
```

## Rules
- Apply to BOTH main.py and live.py pipelines (same RiskAgent class)
- Do NOT change MAX_POSITION_PCT for accounts >= $1K
- Log when sub-$1K path is used: `logger.info(f"Sub-$1K sizing: ${size_usd:.2f} on ${portfolio_value:.0f} account")`
- Add a unit test in tests/ that asserts $300 account produces >= $10 order
- Syntax-check risk_agent.py after edit
- Do NOT restart engines

## Acceptance criteria
- `python -c "from agents.risk_agent import RiskAgent; r = RiskAgent(); print(r._compute_position_size(300, 75, 0.70))"` returns >= 10.0
- Unit test passes
- No change to floors: MIN_SIGNAL_SCORE_FLOOR=62, MIN_AI_CONFIDENCE_FLOOR=0.62
