"""Signal Forge v2 — Canonical Event Types

All inter-agent communication uses these typed Pydantic models.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class MarketRegime(str, Enum):
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGING = "ranging"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"


class RiskDecision(str, Enum):
    APPROVED = "approved"
    VETOED = "vetoed"
    MODIFIED = "modified"


# ── TIER 2 EVENTS ──────────────────────────────────────────────

class MarketStateEvent(BaseModel):
    timestamp: datetime
    symbol: str
    price: float
    volume_24h: float = 0
    price_change_24h_pct: float = 0
    fear_greed_index: int = 50
    regime: MarketRegime = MarketRegime.RANGING
    altfins_signal_score: float = 0
    atr_14: float = 0
    bid_ask_spread: float = 0
    raw_candles: dict = Field(default_factory=dict)


class SentimentEvent(BaseModel):
    timestamp: datetime
    symbol: str
    sonar_summary: str = ""
    sentiment_score: float = 0
    key_narratives: list[str] = Field(default_factory=list)
    social_volume_change_pct: float = 0
    fear_greed: int = 50
    sources: list[str] = Field(default_factory=list)


class OnChainEvent(BaseModel):
    timestamp: datetime
    symbol: str
    whale_net_flow: float = 0
    exchange_net_flow_24h_btc: float = 0
    large_tx_count_1h: int = 0
    smart_money_signal: float = 0
    exchange_balance_7d_change_pct: float = 0
    nansen_label_count: int = 0


class TechnicalEvent(BaseModel):
    timestamp: datetime
    symbol: str
    rsi_14: float = 50
    rsi_trend: str = "neutral"
    macd_signal: float = 0
    macd_histogram: float = 0
    bb_position: float = 0.5
    bb_squeeze: bool = False
    ichimoku_signal: str = "in_cloud"
    ema_alignment: bool = False
    volume_ratio: float = 1.0
    atr_14_pct: float = 0
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    timeframe_consensus: dict[str, str] = Field(default_factory=dict)


# ── TIER 2 → AI ANALYST ───────────────────────────────────────

class SignalBundle(BaseModel):
    timestamp: datetime
    symbol: str
    market_state: MarketStateEvent
    sentiment: Optional[SentimentEvent] = None
    on_chain: Optional[OnChainEvent] = None
    technical: TechnicalEvent
    sentiment_stale: bool = False
    onchain_stale: bool = False
    sentiment_age_mins: float = 0.0
    onchain_age_hrs: float = 0.0
    max_allowed_confidence: float = 1.0  # RiskAgent reads this


# ── AI ANALYST → RISK AGENT ───────────────────────────────────

class TradeProposal(BaseModel):
    timestamp: datetime
    proposal_id: str
    symbol: str
    direction: Direction
    raw_score: float
    ai_confidence: float
    ai_rationale: str = ""
    suggested_entry: float = 0
    suggested_stop: float = 0
    suggested_tp1: float = 0
    suggested_tp2: float = 0
    suggested_tp3: float = 0
    score_breakdown: dict[str, float] = Field(default_factory=dict)


# ── RISK AGENT EVENTS ─────────────────────────────────────────

class RiskAssessmentEvent(BaseModel):
    timestamp: datetime
    proposal_id: str
    decision: RiskDecision
    veto_reason: Optional[str] = None
    approved_size_usd: Optional[float] = None
    approved_size_pct_portfolio: Optional[float] = None
    kelly_fraction: Optional[float] = None
    risk_score: float = 0
    correlation_warning: bool = False
    open_positions_count: int = 0


# ── EXECUTION EVENTS ──────────────────────────────────────────

class OrderPlacedEvent(BaseModel):
    timestamp: datetime
    proposal_id: str
    order_id: str
    symbol: str
    direction: Direction
    size_usd: float
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    broker: Literal["alpaca", "coinbase"] = "alpaca"


class OrderFilledEvent(BaseModel):
    timestamp: datetime
    order_id: str
    filled_price: float
    slippage_bps: float = 0


class TradeClosedEvent(BaseModel):
    timestamp: datetime
    order_id: str
    close_price: float
    close_reason: str
    pnl_usd: float
    pnl_pct: float
    hold_time_hours: float
    max_favorable_excursion: float = 0
    max_adverse_excursion: float = 0


# ── LEARNING AGENT EVENTS ─────────────────────────────────────

class WeightUpdateEvent(BaseModel):
    timestamp: datetime
    old_weights: dict[str, float]
    new_weights: dict[str, float]
    training_window_trades: int = 0
    sharpe_improvement: float = 0
