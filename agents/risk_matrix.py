"""Signal Forge — Correlation & Volatility Risk Matrix

Extracted from ai-fund and ai-hedge-fund patterns:
- Cross-asset correlation matrix to prevent portfolio clustering
- Volatility-adjusted position limits
- Portfolio risk scoring

Used by the Risk Judge to override analyst consensus when portfolio risk is too high.
"""

import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Correlation groups for crypto (hardcoded, updated periodically)
# Within-group correlation is typically 0.7-0.9
CORRELATION_GROUPS = {
    "blue_chip": ["BTC", "ETH"],
    "layer1": ["SOL", "AVAX", "NEAR", "APT", "SUI", "ADA", "DOT", "ATOM"],
    "layer2": ["ARB", "OP"],
    "defi": ["UNI", "LINK"],
    "meme": ["DOGE"],
    "storage": ["FIL"],
    "legacy": ["LTC", "XRP"],
    "ai_oracle": ["INJ"],
}

# Reverse lookup: symbol → group
SYMBOL_GROUP = {}
for group, symbols in CORRELATION_GROUPS.items():
    for sym in symbols:
        SYMBOL_GROUP[sym] = group


@dataclass
class PositionRisk:
    symbol: str
    group: str
    volatility: float           # Annualized vol
    vol_percentile: float       # 0-1, where current vol ranks
    base_limit_pct: float       # Base position limit (% of portfolio)
    vol_adjusted_limit_pct: float  # After volatility adjustment
    corr_multiplier: float      # Correlation discount
    final_limit_pct: float      # Final position limit
    max_usd: float              # Max USD for this position


def compute_volatility(closes: list, period: int = 60) -> float:
    """Annualized volatility from recent closes."""
    if len(closes) < 2:
        return 0.0
    recent = closes[-min(period, len(closes)):]
    rets = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
    if not rets:
        return 0.0
    mean_r = sum(rets) / len(rets)
    variance = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    daily_vol = math.sqrt(variance)
    return daily_vol * math.sqrt(365)


def vol_adjusted_limit(annualized_vol: float, base_pct: float = 0.02) -> float:
    """Scale position limit based on volatility regime (from ai-fund pattern).

    Low vol → larger positions (up to 2.5x base)
    High vol → smaller positions (down to 0.5x base)
    """
    if annualized_vol < 0.30:       # < 30% annual vol = very calm
        return base_pct * 1.25
    elif annualized_vol < 0.50:     # Normal crypto vol
        return base_pct * 1.0
    elif annualized_vol < 0.80:     # Elevated
        return base_pct * 0.75
    elif annualized_vol < 1.20:     # High vol
        return base_pct * 0.50
    else:                           # Extreme vol
        return base_pct * 0.25


def correlation_multiplier(symbol: str, open_positions: list) -> float:
    """Discount position size based on correlation with existing positions.

    Pattern from ai-fund:
    - High correlation (same group) → 0.70x
    - Medium correlation → 0.85x
    - Low/no correlation → 1.0x
    - Diversifying (different groups) → 1.05x
    """
    if not open_positions:
        return 1.0

    new_group = SYMBOL_GROUP.get(symbol, "unknown")

    # Count positions in same group
    same_group_count = 0
    groups_represented = set()

    for pos in open_positions:
        pos_sym = pos.get("symbol", "").replace("/USD", "").replace("USD", "")
        pos_group = SYMBOL_GROUP.get(pos_sym, "unknown")
        groups_represented.add(pos_group)
        if pos_group == new_group:
            same_group_count += 1

    # Apply multiplier
    if same_group_count >= 2:
        return 0.50  # Already heavily concentrated
    elif same_group_count == 1:
        return 0.70  # One position in same group
    elif new_group not in groups_represented:
        return 1.10  # Diversifying into new sector
    else:
        return 1.0


def compute_position_risk(
    symbol: str,
    closes: list,
    portfolio_value: float,
    open_positions: list,
    base_pct: float = 0.02,
) -> PositionRisk:
    """Full risk assessment for a potential new position."""
    group = SYMBOL_GROUP.get(symbol, "unknown")
    vol = compute_volatility(closes)
    vol_limit = vol_adjusted_limit(vol, base_pct)
    corr_mult = correlation_multiplier(symbol, open_positions)
    final_pct = vol_limit * corr_mult
    max_usd = portfolio_value * final_pct

    # Vol percentile (rough: compare to typical crypto vol range 0.3-1.5)
    vol_pct = min(max((vol - 0.3) / (1.5 - 0.3), 0), 1)

    logger.debug(
        f"Risk matrix {symbol}: vol={vol:.0%} group={group} "
        f"base={base_pct:.1%} vol_adj={vol_limit:.1%} "
        f"corr={corr_mult:.2f} final={final_pct:.1%} max=${max_usd:,.0f}"
    )

    return PositionRisk(
        symbol=symbol,
        group=group,
        volatility=vol,
        vol_percentile=vol_pct,
        base_limit_pct=base_pct,
        vol_adjusted_limit_pct=vol_limit,
        corr_multiplier=corr_mult,
        final_limit_pct=final_pct,
        max_usd=max_usd,
    )


def portfolio_risk_score(open_positions: list) -> dict:
    """Score current portfolio risk based on concentration and exposure.

    Returns dict with:
    - concentration_score: 0 (diversified) to 1 (concentrated)
    - sector_exposure: {group: count}
    - largest_sector_pct: % of positions in largest sector
    - risk_level: "low", "medium", "high"
    """
    if not open_positions:
        return {
            "concentration_score": 0,
            "sector_exposure": {},
            "largest_sector_pct": 0,
            "risk_level": "low",
        }

    sector_counts = {}
    for pos in open_positions:
        sym = pos.get("symbol", "").replace("/USD", "").replace("USD", "")
        group = SYMBOL_GROUP.get(sym, "unknown")
        sector_counts[group] = sector_counts.get(group, 0) + 1

    total = len(open_positions)
    largest = max(sector_counts.values()) if sector_counts else 0
    largest_pct = largest / total if total > 0 else 0

    # HHI-style concentration (0 = perfectly diversified, 1 = all same sector)
    hhi = sum((c / total) ** 2 for c in sector_counts.values()) if total > 0 else 0

    risk_level = "low"
    if hhi > 0.5 or largest_pct > 0.6:
        risk_level = "high"
    elif hhi > 0.3 or largest_pct > 0.4:
        risk_level = "medium"

    return {
        "concentration_score": round(hhi, 2),
        "sector_exposure": sector_counts,
        "largest_sector_pct": round(largest_pct, 2),
        "risk_level": risk_level,
    }
