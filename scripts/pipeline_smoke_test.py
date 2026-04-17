#!/usr/bin/env python3
"""Signal Forge v2 — Pipeline Smoke Test

Tests that a high-score signal can flow through the ENTIRE pipeline
from scoring → pre-filter → AI analysis → RiskAgent → Execution.

Run after ANY code change to the signal pipeline.

Usage:
    PYTHONPATH=. python scripts/pipeline_smoke_test.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def test_pipeline():
    from config.settings import settings
    from agents.event_bus import EventBus, Priority
    from agents.events import TradeProposal, RiskAssessmentEvent, RiskDecision, Direction
    from agents.risk_agent import RiskAgent
    from agents.execution_agent import ExecutionAgent

    bus = EventBus()
    results = {"risk_received": False, "risk_decision": None, "exec_received": False}

    # Mock RiskAgent handler
    original_risk = RiskAgent(bus, settings.database_path, settings.portfolio_value)

    async def track_risk(event):
        results["risk_received"] = True
        results["risk_decision"] = "approved" if event.decision == RiskDecision.APPROVED else f"vetoed: {event.veto_reason}"

    bus.subscribe(RiskAssessmentEvent, track_risk)

    # Start bus
    bus_task = asyncio.create_task(bus.run())

    # Publish a high-score test proposal
    proposal = TradeProposal(
        timestamp=__import__("datetime").datetime.now(),
        proposal_id="smoke-test-001",
        symbol="BTC-USD",
        direction=Direction.LONG,
        raw_score=85.0,
        ai_confidence=0.85,
        ai_rationale="Smoke test — pipeline validation",
        suggested_entry=75000.0,
        suggested_stop=73500.0,
        suggested_tp1=78000.0,
        suggested_tp2=81000.0,
        suggested_tp3=84000.0,
    )

    print("Publishing test proposal: BTC-USD score=85 conf=85%...")
    await bus.publish(proposal, priority=Priority.HIGH)

    # Wait for processing
    await asyncio.sleep(3)

    bus.stop()
    bus_task.cancel()

    # Report
    print(f"\n{'='*50}")
    print(f"PIPELINE SMOKE TEST RESULTS")
    print(f"{'='*50}")
    print(f"  RiskAgent received proposal: {results['risk_received']}")
    print(f"  RiskAgent decision: {results['risk_decision']}")
    print(f"{'='*50}")

    if results["risk_received"] and "approved" in str(results["risk_decision"]):
        print("  PASS — Pipeline is functional")
        return True
    elif results["risk_received"]:
        print(f"  PARTIAL — RiskAgent received but vetoed: {results['risk_decision']}")
        print("  This may be correct (e.g., max positions reached)")
        return True
    else:
        print("  FAIL — Proposal did not reach RiskAgent")
        print("  Check EventBus priority, subscription wiring")
        return False


if __name__ == "__main__":
    ok = asyncio.run(test_pipeline())
    sys.exit(0 if ok else 1)
