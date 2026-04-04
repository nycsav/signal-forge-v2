"""Dashboard API — status, positions, prices, signals, orders, performance."""

from fastapi import APIRouter
from datetime import datetime
from config.settings import settings
from data.alpaca_client import AlpacaClient
from data import coinbase_client, fear_greed_client, altfins_client
from db.repository import Repository

router = APIRouter()
repo = Repository(settings.database_path)
alpaca = AlpacaClient(
    api_key=settings.alpaca_api_key or settings.alpaca_api_secret,  # handle both .env key names
    api_secret=settings.alpaca_api_secret or settings.alpaca_secret_key,
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
    return {
        "30d": repo.get_performance_stats(30),
        "7d": repo.get_performance_stats(7),
    }


@router.get("/events")
async def events(limit: int = 50):
    return {"events": repo.get_recent_events(limit)}


@router.get("/trades")
async def trades(limit: int = 50):
    return {"trades": repo.get_recent_trades(limit)}
