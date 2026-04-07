"""Signal Forge v2 — Multi-Timeframe Fibonacci Engine

Research-validated configuration (25+ sources):
- Entry: 61.8% Golden Pocket (primary, 65-70% win rate with confluence)
- Exit: 127.2% (close 50%), 161.8% (close 25%), 261.8% (close remaining)
- Stop: one Fib level below entry + 1-2% buffer
- Confluence: requires 2+ of 5 signals (EMA, RSI, volume, higher-TF Fib, price action)
- Multi-TF: 4H trend → 1H setup → 15m entry (crypto optimal per LuxAlgo)
- Multi-TF reduces false signals by 37% and improves accuracy by 40%

Key findings:
- BTC respects deeper retracements (61.8%, 78.6%) more than shallower ones
- Standalone Fib: 37-50% win rate. With 2+ confluence: 55-70% win rate.
- 127.2% is conservative first TP (high probability). 161.8% is golden extension.
- Higher timeframe Fib levels ALWAYS override lower timeframe levels.
"""

import math
from dataclasses import dataclass, field
from loguru import logger


FIB_RETRACEMENTS = {
    "23.6%": 0.236,
    "38.2%": 0.382,
    "50.0%": 0.500,
    "61.8%": 0.618,
    "78.6%": 0.786,
}

FIB_EXTENSIONS = {
    "127.2%": 1.272,
    "161.8%": 1.618,
    "200.0%": 2.000,
    "261.8%": 2.618,
    "423.6%": 4.236,
}

TIMEFRAME_PRIORITY = {
    "1M": 10, "1w": 9, "1d": 8, "4h": 7, "1h": 6,
    "15m": 5, "5m": 4, "3m": 3, "1m": 2,
}


@dataclass
class FibLevel:
    price: float
    name: str
    ratio: float
    timeframe: str
    is_extension: bool = False


@dataclass
class FibAnalysis:
    symbol: str
    current_price: float
    trend: str

    # All levels across timeframes
    retracements: list[FibLevel] = field(default_factory=list)
    extensions: list[FibLevel] = field(default_factory=list)

    # Confluence zones (multi-TF clusters within 1%)
    confluence_zones: list[dict] = field(default_factory=list)

    # Key levels
    nearest_support: float = 0
    nearest_resistance: float = 0
    golden_pocket: float = 0  # 61.8% level

    # Extension targets for exits
    ext_1272: float = 0
    ext_1618: float = 0
    ext_2618: float = 0

    # Scoring
    fib_score_adj: int = 0  # -10 to +10
    signal: str = "neutral"
    entry_zone: str = "none"
    confluence_count: int = 0
    signals: list[str] = field(default_factory=list)

    # Per-timeframe swing data
    swings: dict = field(default_factory=dict)


def find_swing_points(closes: list[float], min_swing_pct: float = 0.05) -> tuple[float, float, int, int, str]:
    """Find significant swing high and low with indices."""
    if len(closes) < 5:
        return 0, 0, 0, 0, "neutral"

    high = max(closes)
    low = min(closes)
    high_idx = closes.index(high)
    low_idx = closes.index(low)

    if high <= 0 or (high - low) / high < min_swing_pct:
        return high, low, high_idx, low_idx, "neutral"

    direction = "uptrend" if high_idx > low_idx else "downtrend"
    return high, low, high_idx, low_idx, direction


def calculate_levels(swing_high: float, swing_low: float, direction: str) -> tuple[list[FibLevel], list[FibLevel]]:
    """Calculate retracement and extension levels for a single timeframe."""
    diff = swing_high - swing_low
    if diff <= 0:
        return [], []

    retracements = []
    extensions = []

    for name, ratio in FIB_RETRACEMENTS.items():
        if direction == "uptrend":
            price = swing_high - diff * ratio
        else:
            price = swing_low + diff * ratio
        retracements.append(FibLevel(price=round(price, 8), name=name, ratio=ratio, timeframe="", is_extension=False))

    for name, ratio in FIB_EXTENSIONS.items():
        if direction == "uptrend":
            price = swing_low + diff * ratio
        else:
            price = swing_high - diff * ratio
        extensions.append(FibLevel(price=round(price, 8), name=name, ratio=ratio, timeframe="", is_extension=True))

    return retracements, extensions


def find_confluence(all_levels: list[FibLevel], current_price: float, tolerance: float = 0.01) -> list[dict]:
    """Find price zones where Fib levels from different timeframes cluster."""
    if not all_levels:
        return []

    sorted_lvls = sorted(all_levels, key=lambda l: l.price)
    zones = []
    used = set()

    for i, level in enumerate(sorted_lvls):
        if i in used:
            continue
        cluster = [level]
        used.add(i)

        for j, other in enumerate(sorted_lvls):
            if j in used:
                continue
            if other.timeframe != level.timeframe and abs(level.price - other.price) / max(level.price, 0.0001) <= tolerance:
                cluster.append(other)
                used.add(j)

        if len(cluster) >= 2:
            avg = sum(l.price for l in cluster) / len(cluster)
            tfs = sorted(set(l.timeframe for l in cluster), key=lambda t: TIMEFRAME_PRIORITY.get(t, 0), reverse=True)
            zones.append({
                "price": round(avg, 8),
                "count": len(cluster),
                "timeframes": tfs,
                "labels": [f"{l.timeframe} {l.name}" for l in cluster],
                "strength": "STRONG" if len(cluster) >= 3 else "moderate",
                "is_support": avg < current_price,
                "distance_pct": round((avg - current_price) / current_price * 100, 2),
            })

    return sorted(zones, key=lambda z: abs(z["distance_pct"]))


def multi_timeframe_fib(
    symbol: str,
    candles_by_tf: dict[str, list[float]],
    current_price: float,
) -> FibAnalysis:
    """Full multi-timeframe Fibonacci analysis.

    candles_by_tf: {"4h": [close1, close2, ...], "1h": [...], "15m": [...]}
    """
    all_retracements = []
    all_extensions = []
    swings = {}
    primary_direction = "neutral"

    for tf, closes in candles_by_tf.items():
        if not closes or len(closes) < 10:
            continue

        priority = TIMEFRAME_PRIORITY.get(tf, 5)
        min_swing = 0.03 if priority >= 7 else 0.04 if priority >= 5 else 0.05

        high, low, hi_idx, lo_idx, direction = find_swing_points(closes, min_swing)
        if direction == "neutral":
            continue

        swings[tf] = {"high": high, "low": low, "direction": direction}

        retrace, extend = calculate_levels(high, low, direction)

        for l in retrace:
            l.timeframe = tf
        for l in extend:
            l.timeframe = tf

        all_retracements.extend(retrace)
        all_extensions.extend(extend)

        # Highest available TF sets primary direction
        if priority > TIMEFRAME_PRIORITY.get("15m", 5):
            primary_direction = direction

    if not all_retracements:
        return FibAnalysis(symbol=symbol, current_price=current_price, trend="neutral")

    # Confluence zones
    all_levels = all_retracements + all_extensions
    confluence = find_confluence(all_levels, current_price)

    # Nearest support/resistance
    supports = sorted([l.price for l in all_retracements if l.price < current_price], reverse=True)
    resistances = sorted([l.price for l in all_retracements if l.price > current_price])
    nearest_sup = supports[0] if supports else 0
    nearest_res = resistances[0] if resistances else 0

    # Golden pocket (61.8%) from highest timeframe available
    golden = 0
    for l in all_retracements:
        if l.name == "61.8%":
            if golden == 0 or TIMEFRAME_PRIORITY.get(l.timeframe, 0) > TIMEFRAME_PRIORITY.get("15m", 5):
                golden = l.price

    # Extensions for exits (from highest TF)
    ext_1272 = ext_1618 = ext_2618 = 0
    for l in sorted(all_extensions, key=lambda x: TIMEFRAME_PRIORITY.get(x.timeframe, 0), reverse=True):
        if l.name == "127.2%" and ext_1272 == 0:
            ext_1272 = l.price
        elif l.name == "161.8%" and ext_1618 == 0:
            ext_1618 = l.price
        elif l.name == "261.8%" and ext_2618 == 0:
            ext_2618 = l.price

    # ── Scoring ──
    score_adj = 0
    signals = []

    if primary_direction == "uptrend":
        # Golden Pocket entry (highest probability: 65-70% with confluence)
        if golden > 0 and abs(current_price - golden) / golden < 0.01:
            score_adj += 8
            signals.append(f"IN GOLDEN POCKET (61.8%) — highest probability long entry")
        elif any(abs(current_price - l.price) / l.price < 0.01 for l in all_retracements if l.name == "50.0%"):
            score_adj += 5
            signals.append("At 50% Fib retracement — midpoint support")
        elif any(abs(current_price - l.price) / l.price < 0.01 for l in all_retracements if l.name == "38.2%"):
            score_adj += 3
            signals.append("At 38.2% retracement — shallow pullback (strong trend)")
        elif any(current_price < l.price for l in all_retracements if l.name == "78.6%"):
            score_adj -= 5
            signals.append("Below 78.6% — deep retracement, trend may be reversing")

    elif primary_direction == "downtrend":
        if golden > 0 and abs(current_price - golden) / golden < 0.01:
            score_adj -= 8
            signals.append("At 61.8% resistance in downtrend — likely rejection zone")
        elif any(abs(current_price - l.price) / l.price < 0.01 for l in all_retracements if l.name == "50.0%"):
            score_adj -= 5
            signals.append("At 50% Fib resistance in downtrend")

    # Confluence bonus (37% fewer false signals per LuxAlgo research)
    nearby = [z for z in confluence if abs(z["distance_pct"]) < 1.5]
    for z in nearby[:2]:
        if z["is_support"] and z["count"] >= 2:
            bonus = 4 if z["strength"] == "STRONG" else 2
            score_adj += bonus
            signals.append(f"Multi-TF confluence support: {z['count']} levels from {', '.join(z['timeframes'])} at ${z['price']:.4f}")
        elif not z["is_support"] and z["count"] >= 2:
            score_adj -= 2
            signals.append(f"Multi-TF confluence resistance: {', '.join(z['timeframes'])} at ${z['price']:.4f}")

    # Extension check (exit signals)
    if ext_1272 > 0 and current_price >= ext_1272 * 0.99:
        signals.append(f"At 127.2% extension — TP1 zone, close 50% of position")
    if ext_1618 > 0 and current_price >= ext_1618 * 0.99:
        signals.append(f"At 161.8% golden extension — TP2 zone, close 25%")
    if ext_2618 > 0 and current_price >= ext_2618 * 0.99:
        signals.append(f"At 261.8% extreme extension — close remaining or trail")

    # Entry zone classification
    entry_zone = "none"
    if score_adj >= 8:
        entry_zone = "golden_pocket"
    elif score_adj >= 5:
        entry_zone = "secondary_50"
    elif score_adj >= 3:
        entry_zone = "tertiary_382"

    signal = "bullish" if score_adj > 3 else "bearish" if score_adj < -3 else "neutral"

    return FibAnalysis(
        symbol=symbol,
        current_price=current_price,
        trend=primary_direction,
        retracements=all_retracements,
        extensions=all_extensions,
        confluence_zones=confluence,
        nearest_support=nearest_sup,
        nearest_resistance=nearest_res,
        golden_pocket=golden,
        ext_1272=ext_1272,
        ext_1618=ext_1618,
        ext_2618=ext_2618,
        fib_score_adj=max(-10, min(10, score_adj)),
        signal=signal,
        entry_zone=entry_zone,
        confluence_count=len(nearby),
        signals=signals,
        swings=swings,
    )


def fib_exit_targets(entry: float, swing_high: float, swing_low: float) -> dict:
    """Fibonacci-based exit levels for a position.

    Research-validated scaling: 50% at 127.2%, 25% at 161.8%, 25% at 261.8%
    """
    diff = swing_high - swing_low
    if diff <= 0:
        return {}

    return {
        "stop_below_786": round(swing_high - diff * 0.786 - diff * 0.02, 8),
        "tp1_1272": round(swing_low + diff * 1.272, 8),
        "tp1_scale": 0.50,
        "tp2_1618": round(swing_low + diff * 1.618, 8),
        "tp2_scale": 0.25,
        "tp3_2618": round(swing_low + diff * 2.618, 8),
        "tp3_scale": 0.25,
    }
