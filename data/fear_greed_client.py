"""Fear & Greed Index — free API, no auth."""

import httpx
import time
from loguru import logger

_cache = {"value": None, "fetched_at": 0}
CACHE_TTL = 3600  # 1 hour


async def get_fear_greed() -> dict:
    now = time.time()
    if _cache["value"] and now - _cache["fetched_at"] < CACHE_TTL:
        return _cache["value"]

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get("https://api.alternative.me/fng/", params={"limit": 1})
            if r.status_code == 200:
                d = r.json()["data"][0]
                result = {
                    "value": int(d["value"]),
                    "classification": d["value_classification"],
                    "timestamp": d["timestamp"],
                }
                _cache["value"] = result
                _cache["fetched_at"] = now
                return result
        except Exception as e:
            logger.error(f"Fear & Greed fetch failed: {e}")

    return _cache.get("value") or {"value": 50, "classification": "Unknown", "timestamp": "0"}
