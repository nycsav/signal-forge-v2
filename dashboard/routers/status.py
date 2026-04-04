"""Dashboard API — status, positions, prices, signals, orders, performance."""

from fastapi import APIRouter
from datetime import datetime
from pathlib import Path
from config.settings import settings
from data.alpaca_client import AlpacaClient
from data import coinbase_client, fear_greed_client, altfins_client
from db.repository import Repository

router = APIRouter()
repo = Repository(settings.database_path)
alpaca = AlpacaClient(
    api_key=settings.alpaca_api_key,
    api_secret=settings.alpaca_secret_key or settings.alpaca_api_secret,
    base_url=settings.alpaca_base_url,
)


@router.get("/status")
async def status():
    account = await alpaca.get_account()
    fg = await fear_greed_client.get_fear_greed()
    positions = await alpaca.get_positions()
    total_upl = sum(p.get("unrealized_pl", 0) for p in positions)

    # Ollama health
    import httpx
    ollama_status = "offline"
    ollama_models = []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.ollama_host}/api/tags")
            if r.status_code == 200:
                ollama_status = "online"
                ollama_models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass

    return {
        "status": "online",
        "mode": settings.mode,
        "alpaca": {**account, "paper": "paper" in settings.alpaca_base_url},
        "fear_greed": fg,
        "ollama": {"status": ollama_status, "models": ollama_models},
        "positions_count": len(positions),
        "unrealized_pl": total_upl,
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/positions")
async def positions():
    pos = await alpaca.get_positions()
    return {"positions": pos, "count": len(pos)}


@router.get("/prices")
async def prices():
    p = await coinbase_client.get_all_prices(settings.watchlist)
    return {"prices": p, "timestamp": datetime.now().isoformat()}


@router.get("/signals")
async def signals(limit: int = 20):
    return {"signals": repo.get_recent_signals(limit)}


@router.get("/orders")
async def orders():
    o = await alpaca.get_orders()
    return {"orders": o}


@router.get("/performance")
async def performance():
    from agents.learning_agent import LearningAgent
    from agents.event_bus import EventBus
    learner = LearningAgent(EventBus(), settings.database_path)
    return {
        "30d": repo.get_performance_stats(30),
        "7d": repo.get_performance_stats(7),
        "report_7d": learner.generate_performance_report(7),
        "report_30d": learner.generate_performance_report(30),
        "weights": learner.get_weights(),
    }


@router.get("/events")
async def events(limit: int = 50):
    return {"events": repo.get_recent_events(limit)}


@router.get("/trades")
async def trades(limit: int = 50):
    return {"trades": repo.get_recent_trades(limit)}


@router.get("/regime")
async def regime():
    """Current adaptive regime parameters and reasoning journal."""
    from agents.regime_engine import RegimeAdaptiveEngine
    from agents.events import MarketRegime
    from data import fear_greed_client
    engine = RegimeAdaptiveEngine(settings.database_path)

    # Get current conditions
    fg = await fear_greed_client.get_fear_greed()
    fg_val = fg.get("value", 50)

    # Get recent performance
    perf = repo.get_performance_stats(7)
    win_rate = perf.get("win_rate", 50) / 100
    positions = await alpaca.get_positions()

    # Update engine
    engine.update(
        fear_greed=fg_val,
        market_regime=MarketRegime.BEAR_TREND if fg_val < 30 else MarketRegime.RANGING,
        recent_win_rate=win_rate,
        open_positions=len(positions),
    )

    data = engine.get_dashboard_data()

    # Add journal of recent events
    events = repo.get_recent_events(20)
    regime_events = [e for e in events if e.get("agent_name") == "regime_engine"]
    data["journal"] = regime_events

    return data


@router.get("/journal")
async def journal(limit: int = 50):
    """Full system journal — all agent events with timestamps."""
    events = repo.get_recent_events(limit)
    # Group by agent
    by_agent = {}
    for e in events:
        agent = e.get("agent_name", "unknown")
        if agent not in by_agent:
            by_agent[agent] = []
        by_agent[agent].append(e)
    return {
        "total_events": len(events),
        "events": events,
        "by_agent": {k: len(v) for k, v in by_agent.items()},
    }


@router.get("/backtest/{symbol}")
async def run_backtest_api(symbol: str, days: int = 30):
    """Run a backtest for a symbol. Returns performance metrics."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from scripts.backtest import fetch_ohlc, run_backtest
    candles = fetch_ohlc(symbol, days)
    if not candles:
        return {"error": f"No data for {symbol}"}
    return run_backtest(candles)
