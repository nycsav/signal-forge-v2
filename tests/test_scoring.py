"""Tests for the Signal Scoring engine."""

from agents.scoring import SignalScorer
from agents.events import TechnicalEvent, SentimentEvent, OnChainEvent, Direction
from datetime import datetime


def make_tech(**kwargs) -> TechnicalEvent:
    defaults = dict(
        timestamp=datetime.now(), symbol="BTC-USD", rsi_14=50, rsi_trend="neutral",
        macd_signal=0, macd_histogram=0, bb_position=0.5, bb_squeeze=False,
        ichimoku_signal="in_cloud", ema_alignment=False, volume_ratio=1.0,
        atr_14_pct=0.02, support_levels=[], resistance_levels=[],
        timeframe_consensus={},
    )
    defaults.update(kwargs)
    return TechnicalEvent(**defaults)


def test_neutral_tech_score():
    scorer = SignalScorer()
    tech = make_tech()
    score = scorer.score_technical(tech)
    assert 40 <= score <= 60, f"Neutral tech should be ~50, got {score}"


def test_oversold_scores_high():
    scorer = SignalScorer()
    tech = make_tech(rsi_14=22, bb_position=0.05, macd_histogram=0.001, ema_alignment=True)
    score = scorer.score_technical(tech)
    assert score >= 65, f"Oversold + aligned should score high, got {score}"


def test_overbought_scores_low():
    scorer = SignalScorer()
    tech = make_tech(rsi_14=85, bb_position=0.95, macd_histogram=-0.001)
    score = scorer.score_technical(tech)
    assert score <= 40, f"Overbought should score low, got {score}"


def test_sentiment_extreme_fear_is_bullish():
    scorer = SignalScorer()
    sent = SentimentEvent(timestamp=datetime.now(), symbol="BTC-USD", fear_greed=10)
    score = scorer.score_sentiment(sent)
    assert score >= 55, f"Extreme fear should be contrarian bullish, got {score}"


def test_sentiment_extreme_greed_is_bearish():
    scorer = SignalScorer()
    sent = SentimentEvent(timestamp=datetime.now(), symbol="BTC-USD", fear_greed=90)
    score = scorer.score_sentiment(sent)
    assert score <= 45, f"Extreme greed should be bearish, got {score}"


def test_composite_respects_weights():
    scorer = SignalScorer()
    composite, breakdown = scorer.composite_score(80, 60, 50, 70)
    assert 60 <= composite <= 80, f"Composite should be 60-80 with these inputs, got {composite}"
    assert "technical" in breakdown
    assert "ai_analyst" in breakdown


def test_score_to_direction():
    scorer = SignalScorer()
    assert scorer.score_to_direction(70) == Direction.LONG
    assert scorer.score_to_direction(50) == Direction.FLAT
    assert scorer.score_to_direction(30) == Direction.SHORT


def test_onchain_accumulation():
    scorer = SignalScorer()
    onchain = OnChainEvent(
        timestamp=datetime.now(), symbol="BTC-USD",
        whale_net_flow=5.0, exchange_net_flow_24h_btc=-200, smart_money_signal=0.5,
    )
    score = scorer.score_onchain(onchain)
    assert score >= 55, f"Accumulation signals should be bullish, got {score}"


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
