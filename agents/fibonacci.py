"""Signal Forge v2 — Fibonacci Analysis

Calculates Fibonacci retracement and extension levels for:
- Pullback entry zones (38.2%, 50%, 61.8%)
- Support/resistance levels
- Exit targets (extensions: 127.2%, 161.8%, 261.8%)
- Trend strength (which Fib level holds = strength indicator)

Used by Monitor Agent for dynamic exit levels and Technical Agent for scoring.
"""

from dataclasses import dataclass
from loguru import logger


@dataclass
class FibLevels:
    """Fibonacci levels calculated from a swing high/low range."""
    symbol: str
    swing_high: float
    swing_low: float
    direction: str  # "uptrend" or "downtrend"

    # Retracement levels (support in uptrend, resistance in downtrend)
    fib_236: float  # 23.6% — shallow pullback
    fib_382: float  # 38.2% — healthy pullback
    fib_500: float  # 50.0% — midpoint
    fib_618: float  # 61.8% — golden ratio (strongest)
    fib_786: float  # 78.6% — deep pullback (trend may be reversing)

    # Extension levels (profit targets beyond the swing)
    ext_1272: float  # 127.2% extension
    ext_1618: float  # 161.8% golden extension
    ext_2618: float  # 261.8% extreme extension

    # Current price context
    current_price: float
    nearest_support: float
    nearest_resistance: float
    current_zone: str  # "below_618", "382_500", "above_236", etc.


def calculate_fib_levels(
    symbol: str,
    closes: list[float],
    current_price: float,
    lookback: int = 50,
) -> FibLevels | None:
    """Calculate Fibonacci retracement levels from recent swing high/low.

    Uses the highest and lowest close in the lookback period.
    """
    if len(closes) < lookback:
        if len(closes) < 10:
            return None
        lookback = len(closes)

    recent = closes[-lookback:]
    swing_high = max(recent)
    swing_low = min(recent)
    high_idx = recent.index(swing_high)
    low_idx = recent.index(swing_low)

    if swing_high <= swing_low:
        return None

    diff = swing_high - swing_low

    # Determine trend direction: if high came after low = uptrend
    direction = "uptrend" if high_idx > low_idx else "downtrend"

    if direction == "uptrend":
        # Retracements measured DOWN from the high
        fib_236 = swing_high - diff * 0.236
        fib_382 = swing_high - diff * 0.382
        fib_500 = swing_high - diff * 0.500
        fib_618 = swing_high - diff * 0.618
        fib_786 = swing_high - diff * 0.786

        # Extensions measured UP from the high
        ext_1272 = swing_low + diff * 1.272
        ext_1618 = swing_low + diff * 1.618
        ext_2618 = swing_low + diff * 2.618
    else:
        # Downtrend: retracements measured UP from the low
        fib_236 = swing_low + diff * 0.236
        fib_382 = swing_low + diff * 0.382
        fib_500 = swing_low + diff * 0.500
        fib_618 = swing_low + diff * 0.618
        fib_786 = swing_low + diff * 0.786

        # Extensions measured DOWN from the low
        ext_1272 = swing_high - diff * 1.272
        ext_1618 = swing_high - diff * 1.618
        ext_2618 = swing_high - diff * 2.618

    # Determine current zone
    levels = sorted([
        ("below_swing_low", swing_low),
        ("786_zone", fib_786),
        ("618_zone", fib_618),
        ("500_zone", fib_500),
        ("382_zone", fib_382),
        ("236_zone", fib_236),
        ("above_swing_high", swing_high),
    ], key=lambda x: x[1])

    current_zone = "above_swing_high"
    for i, (zone_name, level) in enumerate(levels):
        if current_price <= level:
            current_zone = zone_name
            break

    # Find nearest support and resistance
    all_levels = sorted([fib_236, fib_382, fib_500, fib_618, fib_786, swing_low, swing_high])
    supports = [l for l in all_levels if l < current_price]
    resistances = [l for l in all_levels if l > current_price]
    nearest_support = supports[-1] if supports else swing_low
    nearest_resistance = resistances[0] if resistances else swing_high

    return FibLevels(
        symbol=symbol,
        swing_high=swing_high,
        swing_low=swing_low,
        direction=direction,
        fib_236=round(fib_236, 6),
        fib_382=round(fib_382, 6),
        fib_500=round(fib_500, 6),
        fib_618=round(fib_618, 6),
        fib_786=round(fib_786, 6),
        ext_1272=round(ext_1272, 6),
        ext_1618=round(ext_1618, 6),
        ext_2618=round(ext_2618, 6),
        current_price=current_price,
        nearest_support=round(nearest_support, 6),
        nearest_resistance=round(nearest_resistance, 6),
        current_zone=current_zone,
    )


def score_fib_position(fib: FibLevels) -> dict:
    """Score the current price position relative to Fibonacci levels.

    Returns signals for entry, exit, and trend strength.
    """
    if not fib:
        return {"score": 0, "signal": "neutral", "reasoning": "No Fib data"}

    price = fib.current_price
    signals = []
    score_adj = 0  # Adjustment to composite score (-10 to +10)

    if fib.direction == "uptrend":
        # In uptrend: pullbacks to 38.2%-61.8% are buying opportunities
        if fib.fib_500 <= price <= fib.fib_382:
            score_adj = +5
            signals.append("Price at 38.2-50% Fib retracement — healthy pullback, good long entry")
        elif fib.fib_618 <= price <= fib.fib_500:
            score_adj = +8
            signals.append("Price at 50-61.8% Fib retracement — golden ratio support, strong long entry")
        elif price <= fib.fib_786:
            score_adj = -3
            signals.append("Price below 78.6% Fib — deep pullback, trend may be reversing")
        elif price >= fib.swing_high:
            score_adj = -2
            signals.append("Price above swing high — extended, watch for pullback")
        elif fib.fib_236 <= price <= fib.swing_high:
            score_adj = +2
            signals.append("Price in 23.6% zone — strong uptrend, minor pullback")

        # Extension targets for exits
        if price >= fib.ext_1272:
            signals.append(f"Above 127.2% extension — consider taking profits")
        if price >= fib.ext_1618:
            signals.append(f"Above 161.8% golden extension — strong TP zone")

    else:  # downtrend
        # In downtrend: bounces to 38.2%-61.8% are selling/shorting zones
        if fib.fib_382 <= price <= fib.fib_500:
            score_adj = -5
            signals.append("Price at 38.2-50% Fib bounce — selling zone in downtrend")
        elif fib.fib_500 <= price <= fib.fib_618:
            score_adj = -8
            signals.append("Price at 50-61.8% Fib bounce — strong resistance, likely rejection")
        elif price >= fib.fib_786:
            score_adj = +3
            signals.append("Price above 78.6% — breakout attempt, trend may be reversing up")
        elif price <= fib.swing_low:
            score_adj = -2
            signals.append("Price below swing low — downtrend continuing")

    # Support/resistance distance
    support_dist = (price - fib.nearest_support) / price * 100 if price > 0 else 0
    resistance_dist = (fib.nearest_resistance - price) / price * 100 if price > 0 else 0

    return {
        "score_adjustment": score_adj,
        "signal": "bullish" if score_adj > 3 else "bearish" if score_adj < -3 else "neutral",
        "zone": fib.current_zone,
        "direction": fib.direction,
        "nearest_support": fib.nearest_support,
        "nearest_resistance": fib.nearest_resistance,
        "support_distance_pct": round(support_dist, 2),
        "resistance_distance_pct": round(resistance_dist, 2),
        "signals": signals,
        "fib_levels": {
            "23.6%": fib.fib_236,
            "38.2%": fib.fib_382,
            "50.0%": fib.fib_500,
            "61.8%": fib.fib_618,
            "78.6%": fib.fib_786,
            "127.2% ext": fib.ext_1272,
            "161.8% ext": fib.ext_1618,
            "261.8% ext": fib.ext_2618,
        },
    }
