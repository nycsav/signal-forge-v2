"""Signal Forge v2 — Live Trading Rules

Strict rules for real money. These override paper settings.
$300 starting capital demands different approach than $100K paper.
"""

# ── Capital ──
STARTING_CAPITAL = 300.00
MODE = "live"

# ── Coins: only trade the most liquid ──
WATCHLIST = ["BTC-USD", "ETH-USD", "SOL-USD"]

# ── Position Sizing ──
MAX_POSITION_PCT = 0.10         # 10% per trade ($30 on $300)
MAX_OPEN_POSITIONS = 2          # Never more than 2 positions
MIN_ORDER_USD = 10.00           # Below this, fees kill you

# ── Entry: only high conviction ──
MIN_SIGNAL_SCORE = 65           # Paper uses 40 (adaptive). Live: 65 minimum.
MIN_AI_CONFIDENCE = 0.60        # Paper uses 0.45. Live: 0.60 minimum.
REQUIRE_CONSENSUS = True        # Both Qwen3 AND DeepSeek must agree on direction
REQUIRE_FIB_CONFLUENCE = True   # Must be near a Fib level with multi-TF support

# ── Stops: tight, non-negotiable ──
STOP_LOSS_PCT = 0.03            # 3% stop (paper uses 7.5%). Max loss $0.90 on $30 trade.
TRAILING_ACTIVATION_PCT = 0.02  # Activate trailing after +2% (paper: +4.5%)
TRAILING_DISTANCE_PCT = 0.015   # Trail at 1.5% behind peak

# ── Take Profit: take money off the table ──
TP1_PCT = 0.03                  # Close 50% at +3% ($0.45 profit per $30)
TP2_PCT = 0.05                  # Close remaining at +5% ($0.75 profit per $30)
TP1_SCALE = 0.50
TP2_SCALE = 0.50

# ── Risk Limits ──
DAILY_LOSS_LIMIT_USD = 15.00    # 5% of $300 — halt all live trading
DAILY_LOSS_LIMIT_PCT = 0.05
WEEKLY_LOSS_LIMIT_USD = 30.00   # 10% — halt + require manual review
MAX_TRADES_PER_DAY = 3          # Prevent over-trading
MIN_HOLD_MINUTES = 15           # No scalping — hold at least 15 min

# ── Order Execution ──
ORDER_TYPE = "limit"            # NEVER market orders on small capital
LIMIT_OFFSET_PCT = 0.001       # Place limit 0.1% below ask (buy) or above bid (sell)
SLIPPAGE_LIMIT_BPS = 10        # Reject if slippage > 10 basis points

# ── Exchange ──
EXCHANGE = "alpaca"             # 0% crypto commission
# If switching to Coinbase: maker fee 0.6%, taker 0.8% — adjust targets accordingly

# ── Compounding ──
COMPOUND = True                 # Reinvest all profits
WITHDRAW_THRESHOLD = 1000.00   # Don't withdraw until $1,000
