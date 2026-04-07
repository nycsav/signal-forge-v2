"""Signal Forge v2 — Live Trading Rules (Aggressive with Guardrails)

$300 starting capital. Aggressive in extreme fear, disciplined otherwise.
The edge IS the fear — F&G < 15 is when the money is made historically.
"""

# ── Capital ──
STARTING_CAPITAL = 300.00
MODE = "live"

# ── Coins: only trade the most liquid ──
WATCHLIST = ["BTC-USD", "ETH-USD", "SOL-USD"]

# ── Position Sizing: aggressive but bounded ──
MAX_POSITION_PCT = 0.15         # 15% per trade ($45 on $300) — bigger bets, fewer trades
MAX_OPEN_POSITIONS = 3          # Up to 3 positions (45% deployed max)
MIN_ORDER_USD = 10.00           # Below this, fees kill you

# ── Entry: lower bar in extreme fear, signal quality still matters ──
MIN_SIGNAL_SCORE = 55           # Lower bar — the edge IS the fear
MIN_AI_CONFIDENCE = 0.50        # At least one model confident
REQUIRE_CONSENSUS = False       # Consensus PREFERRED (+15% size boost) but not required
REQUIRE_FIB_CONFLUENCE = False  # Fib confluence PREFERRED (+15% size boost) but not gating

# ── Conviction Bonuses: reward quality signals with bigger size ──
CONSENSUS_SIZE_BONUS = 0.15     # +15% position size when both AI models agree
FIB_CONFLUENCE_BONUS = 0.15     # +15% when price is at multi-TF Fib level
ARKHAM_BULLISH_BONUS = 0.10     # +10% when whale data confirms direction
MAX_SIZE_WITH_BONUSES = 0.25    # Hard cap: never more than 25% on any single trade

# ── Stops: tight but not suffocating ──
STOP_LOSS_PCT = 0.04            # 4% stop. Max loss $1.80 on $45 trade.
TRAILING_ACTIVATION_PCT = 0.025 # Activate trailing after +2.5%
TRAILING_DISTANCE_PCT = 0.02    # Trail at 2% behind peak

# ── Take Profit: let winners run ──
TP1_PCT = 0.04                  # Close 50% at +4% ($0.90 profit per $45)
TP2_PCT = 0.08                  # Close remaining at +8% ($1.80 profit per $45)
TP1_SCALE = 0.50
TP2_SCALE = 0.50
# Asymmetric R:R: risk $1.80 (4% stop) to make $0.90-$1.80 (4-8% TP) = 1:1 to 2:1

# ── Risk Limits: non-negotiable ──
DAILY_LOSS_LIMIT_USD = 15.00    # 5% of $300 — halt all live trading
DAILY_LOSS_LIMIT_PCT = 0.05
WEEKLY_LOSS_LIMIT_USD = 30.00   # 10% — halt + require manual review
MAX_TRADES_PER_DAY = 5          # Allow more trades in volatile markets
MIN_HOLD_MINUTES = 5            # Allow faster exits if needed

# ── Order Execution ──
ORDER_TYPE = "limit"            # NEVER market orders on small capital
LIMIT_OFFSET_PCT = 0.001        # Place limit 0.1% below ask (buy) or above bid (sell)
SLIPPAGE_LIMIT_BPS = 10         # Reject if slippage > 10 basis points

# ── Exchange ──
EXCHANGE = "alpaca"             # 0% crypto commission

# ── Compounding ──
COMPOUND = True                 # Reinvest all profits
WITHDRAW_THRESHOLD = 1000.00    # Don't withdraw until $1,000

# ── Aggressive Regime Rules ──
# When F&G < 15 (extreme fear / capitulation):
#   - Score threshold drops to 50
#   - Accept single-model signals (no consensus needed)
#   - Max 3 positions instead of 2
#   - This is when we deploy capital hardest
# When F&G > 70:
#   - Score threshold rises to 70
#   - Require consensus
#   - Max 1 position
#   - Take profits aggressively
FEAR_AGGRESSIVE_THRESHOLD = 15
GREED_DEFENSIVE_THRESHOLD = 70
