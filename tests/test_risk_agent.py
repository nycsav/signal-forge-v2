"""Tests for the Risk Agent veto rules."""

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from agents.events import TradeProposal, RiskAssessmentEvent, RiskDecision, Direction
from agents.event_bus import EventBus
from agents.risk_agent import RiskAgent


def make_proposal(**kwargs) -> TradeProposal:
    defaults = dict(
        timestamp=datetime.now(), proposal_id="test-001", symbol="BTC-USD",
        direction=Direction.LONG, raw_score=70, ai_confidence=0.7,
        ai_rationale="Test", suggested_entry=67000, suggested_stop=62000,
        suggested_tp1=74500, suggested_tp2=82000, suggested_tp3=92000,
        score_breakdown={},
    )
    defaults.update(kwargs)
    return TradeProposal(**defaults)


def _setup_test_db(path):
    Path(path).parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path)
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()


def test_veto_low_score():
    """Score below 55 should be vetoed."""
    db_path = "/tmp/test_risk_v2_1.db"
    _setup_test_db(db_path)
    bus = EventBus()
    risk = RiskAgent(bus, db_path, 100000)
    # Override Alpaca check to return 0 positions
    risk._cached_position_count = 0
    risk._cached_positions = []
    risk._cache_time = 9999999999

    results = []
    bus.subscribe(RiskAssessmentEvent, lambda e: results.append(e))

    asyncio.run(_run(bus, risk, make_proposal(raw_score=40)))
    assert len(results) == 1
    assert results[0].decision == RiskDecision.VETOED
    os.unlink(db_path)


def test_approve_good_proposal():
    """Score 70+ with good R:R should be approved."""
    db_path = "/tmp/test_risk_v2_2.db"
    _setup_test_db(db_path)
    bus = EventBus()
    risk = RiskAgent(bus, db_path, 100000)
    risk._cached_position_count = 0
    risk._cached_positions = []
    risk._cache_time = 9999999999

    results = []
    bus.subscribe(RiskAssessmentEvent, lambda e: results.append(e))

    asyncio.run(_run(bus, risk, make_proposal(raw_score=72, ai_confidence=0.75)))
    assert len(results) == 1
    assert results[0].decision == RiskDecision.APPROVED
    assert results[0].approved_size_usd > 0
    os.unlink(db_path)


def test_veto_low_confidence():
    """AI confidence below 0.45 should be vetoed."""
    db_path = "/tmp/test_risk_v2_3.db"
    _setup_test_db(db_path)
    bus = EventBus()
    risk = RiskAgent(bus, db_path, 100000)
    risk._cached_position_count = 0
    risk._cached_positions = []
    risk._cache_time = 9999999999

    results = []
    bus.subscribe(RiskAssessmentEvent, lambda e: results.append(e))

    asyncio.run(_run(bus, risk, make_proposal(raw_score=70, ai_confidence=0.3)))
    assert len(results) == 1
    assert results[0].decision == RiskDecision.VETOED
    assert "confidence" in (results[0].veto_reason or "").lower()
    os.unlink(db_path)


async def _run(bus, risk, proposal):
    bus_task = asyncio.create_task(bus.run())
    await risk._on_proposal(proposal)
    await asyncio.sleep(0.2)
    bus.stop()


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
