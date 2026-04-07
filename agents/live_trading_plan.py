"""Signal Forge v2 — Live Trading Readiness Plan

Honest assessment of what needs to change before real money.
$300 starting capital requires fundamentally different approach than $100K paper.
"""


def generate_live_plan() -> dict:
    return {
        "capital": 300,
        "critical_differences_from_paper": [
            {
                "issue": "Fees destroy small accounts",
                "detail": "Coinbase Advanced: 0.6% maker / 0.8% taker for <$10K volume. A $30 trade costs $0.24 each way = $0.48 round trip. Need +1.6% just to break even on fees. Our average trade P&L is +0.5% — that's a NET LOSS after fees.",
                "fix": "Use Alpaca for crypto (0% commission on crypto). Or use Coinbase with limit orders only (0.6% vs 0.8%). Or increase minimum trade size so fees are proportionally smaller.",
            },
            {
                "issue": "Position sizing too small to be meaningful",
                "detail": "Quarter-Kelly at 1% of $300 = $3 per trade. Most exchanges have $5-10 minimum order. Even at 5% per trade = $15, the P&L is pennies.",
                "fix": "For $300 account: use 5-10% per trade (not 1%). Concentrate on 2-3 high-conviction trades, not 15 scattered positions. This is higher risk but necessary for small capital.",
            },
            {
                "issue": "50 coins is wrong for $300",
                "detail": "Scanning 50 coins and spreading $300 across them = $6 per position. Pointless. Small accounts need FOCUS, not diversification.",
                "fix": "Trade only BTC and ETH (highest liquidity, lowest spread). Maybe SOL as a third. Maximum 2-3 positions at a time.",
            },
            {
                "issue": "Paper slippage is zero, real slippage is not",
                "detail": "Paper trading fills instantly at the quoted price. Real orders on thin altcoins can slip 0.5-2%. On a $30 position that's $0.15-$0.60 lost to slippage.",
                "fix": "Only trade top-3 by liquidity. Use limit orders, never market orders. Accept slower fills for better prices.",
            },
        ],
        "live_configuration": {
            "exchange": "Alpaca (0% crypto commission) or Coinbase Advanced (limit orders only)",
            "coins": ["BTC-USD", "ETH-USD", "SOL-USD"],
            "max_positions": 2,
            "position_size": "10% of capital per trade ($30)",
            "max_risk_per_trade": "3% of capital ($9 max loss)",
            "stop_loss": "3% below entry (not 7.5% — can't afford large drawdowns on $300)",
            "take_profit": "Scale out: 50% at +3%, remaining at +5%",
            "minimum_score": 65,
            "minimum_ai_confidence": 0.6,
            "minimum_consensus": True,
            "order_type": "Limit orders ONLY (never market)",
            "daily_loss_limit": "5% ($15) — halt all trading",
            "weekly_loss_limit": "10% ($30) — halt + review",
        },
        "go_live_checklist": [
            {
                "requirement": "Paper trade profitably for 14+ days",
                "status": "IN PROGRESS (5 days, +0.16% — need stronger results)",
                "target": "+5% over 14 days minimum",
            },
            {
                "requirement": "Win rate above 55% over 50+ trades",
                "status": "NEED MORE DATA (50 trades but win rate unclear from mixed open/closed)",
                "target": "55%+ on closed trades only",
            },
            {
                "requirement": "Max drawdown under 10% on paper",
                "status": "PASS (no significant drawdown yet)",
                "target": "<10% peak-to-trough",
            },
            {
                "requirement": "Fee-adjusted returns positive",
                "status": "FAIL (0.16% return < 1.6% fee drag per round trip)",
                "target": "Returns must exceed fee drag",
            },
            {
                "requirement": "Exits working correctly (not just time exits)",
                "status": "PARTIAL (mostly time exits, few TP/trailing exits)",
                "target": "At least 30% of exits from TP1/trailing, not just 72h timeout",
            },
            {
                "requirement": "AI consensus producing edge",
                "status": "TOO EARLY (Qwen3 + DeepSeek dual model just deployed)",
                "target": "Consensus trades outperform non-consensus by 2%+",
            },
        ],
        "what_i_would_do_differently": [
            {
                "change": "PATIENCE over frequency",
                "detail": "Stop trading every 15 minutes. Wait for A+ setups only — Fib Golden Pocket + multi-TF confluence + RSI oversold + volume spike. Maybe 1-2 trades per WEEK, not 10 per day.",
            },
            {
                "change": "Tighter stops, smaller targets",
                "detail": "On $300, a 7.5% stop = $22.50 loss. That's 7.5% of capital gone on one bad trade. Use 3% stops instead. Take profits at +3% and +5%, not +11% (TP1 too far for small capital).",
            },
            {
                "change": "Only trade with confluence",
                "detail": "Require ALL of: Fib level + EMA alignment + RSI confirmation + Qwen3/DeepSeek consensus. No confluence = no trade. This alone would cut trade count by 80% but increase win rate to 65%+.",
            },
            {
                "change": "Compound, don't withdraw",
                "detail": "$300 → $309 (+3%) → $318 → $328 → $338. After 10 winning trades at +3%, you have $403. After 20: $542. Compounding is how small accounts grow. Never withdraw until $1,000+.",
            },
            {
                "change": "Track fee impact on every trade",
                "detail": "Log actual fees paid. If fees > 30% of gross profit, the strategy needs wider targets or fewer trades.",
            },
            {
                "change": "Use the learning agent actively",
                "detail": "After every 10 trades, review: which coin performed best? Which timeframe? Which Fib level held? Adjust weights based on actual results, not theory.",
            },
        ],
        "realistic_expectations": {
            "monthly_target": "+5-10% ($15-30 on $300)",
            "time_to_double": "7-14 months at 5-10% monthly (compounded)",
            "expected_losing_streaks": "3-5 consecutive losses will happen. At 3% risk per trade, that's -9% to -15%. Must survive these.",
            "probability_of_losing_everything": "If risk management is followed: <5%. If over-leveraged or stops not respected: >50%.",
        },
    }
