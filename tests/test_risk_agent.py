"""Tests for the Risk Agent veto rules."""

import asyncio
from datetime import datetime
from agents.events import TradeProposal, RiskAssessmentEvent, RiskDecision, Direction
from agents.event_bus import EventBus
from agents.risk_agent import RiskAgent


def make_proposal(**kwargs) -> TradeProposal:
    defaults = dict(
        timestamp=datetime.now(), proposal_id="test-001", symbol="BTC-USD",
        direction=Direction.LONG, raw_score=70, ai_confidence=0.7,
        ai_rationale="Test", suggested_entry=67000, suggested_stop=65000,
        suggested_tp1=70000, suggested_tp2=73000, suggested_tp3=78000,
        score_breakdown={},
    )
    defaults.update(kwargs)
    return TradeProposal(**defaults)


def test_veto_low_score():
    """Score below 55 should be vetoed."""
    bus = EventBus()
    risk = RiskAgent(bus, "/tmp/test_risk.db", 100000)
    # Init DB
    from scripts.init_db import init_db
    import sqlite3, os
    db_path = "/tmp/test_risk.db"
    from pathlib import Path
    Path(db_path).parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()

    risk = RiskAgent(bus, db_path, 100000)

    results = []
    async def capture(event):
        results.append(event)
    bus.subscribe(RiskAssessmentEvent, capture)

    proposal = make_proposal(raw_score=40)

    async def run():
        bus_task = asyncio.create_task(bus.run())
        await risk._on_proposal(proposal)
        await asyncio.sleep(0.2)
        bus.stop()

    asyncio.run(run())
    assert len(results) == 1
    assert results[0].decision == RiskDecision.VETOED
    assert "Score" in results[0].veto_reason
    os.unlink(db_path)


def test_approve_good_proposal():
    """Score 70+ with room for positions should be approved."""
    bus = EventBus()
    db_path = "/tmp/test_risk2.db"
    import sqlite3, os
    from pathlib import Path
    conn = sqlite3.connect(db_path)
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()

    risk = RiskAgent(bus, db_path, 100000)

    results = []
    async def capture(event):
        results.append(event)
    bus.subscribe(RiskAssessmentEvent, capture)

    # TP1 must be >= 2x the risk distance from entry for R:R >= 2.0
    proposal = make_proposal(raw_score=72, ai_confidence=0.75,
        suggested_entry=67000, suggested_stop=65000, suggested_tp1=71000)

    async def run():
        bus_task = asyncio.create_task(bus.run())
        await risk._on_proposal(proposal)
        await asyncio.sleep(0.2)
        bus.stop()

    asyncio.run(run())
    assert len(results) == 1
    assert results[0].decision == RiskDecision.APPROVED
    assert results[0].approved_size_usd > 0
    os.unlink(db_path)


def test_veto_bad_risk_reward():
    """R:R below 2.0 should be vetoed."""
    bus = EventBus()
    db_path = "/tmp/test_risk3.db"
    import sqlite3, os
    from pathlib import Path
    conn = sqlite3.connect(db_path)
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()

    risk = RiskAgent(bus, db_path, 100000)

    results = []
    async def capture(event):
        results.append(event)
    bus.subscribe(RiskAssessmentEvent, capture)

    # TP1 is barely above entry, stop is far below = bad R:R
    proposal = make_proposal(
        raw_score=70, suggested_entry=67000,
        suggested_stop=64000, suggested_tp1=67500,
    )

    async def run():
        bus_task = asyncio.create_task(bus.run())
        await risk._on_proposal(proposal)
        await asyncio.sleep(0.2)
        bus.stop()

    asyncio.run(run())
    assert len(results) == 1
    assert results[0].decision == RiskDecision.VETOED
    assert "Risk/reward" in (results[0].veto_reason or "")
    os.unlink(db_path)


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
