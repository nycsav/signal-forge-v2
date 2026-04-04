"""altFINS API v2 client — signals, screener, indicators."""

import httpx
from loguru import logger

ALTFINS_BASE = "https://altfins.com/api/v2/public"


async def get_signals(api_key: str, direction: str = "BULLISH", size: int = 50) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                f"{ALTFINS_BASE}/signals-feed/search-requests",
                headers={"X-API-KEY": api_key},
                json={"direction": direction, "size": size},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("content", data) if isinstance(data, dict) else data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"altFINS signals fetch failed: {e}")
    return []


async def get_all_signals(api_key: str) -> list[dict]:
    """Fetch both bullish and bearish signals."""
    import asyncio
    bull, bear = await asyncio.gather(
        get_signals(api_key, "BULLISH"),
        get_signals(api_key, "BEARISH"),
    )
    return bull + bear


async def get_credits(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{ALTFINS_BASE}/available-permits",
                headers={"X-API-KEY": api_key},
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}
