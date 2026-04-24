"""
Signal Forge V2 — Fear & Greed Overlay

Fetches the Crypto Fear & Greed Index from alternative.me.
Returns a boost multiplier for the composite score:
  - Extreme Fear (< 25):  fg_boost = 1.10 (contrarian buy signal)
  - Extreme Greed (> 75):  fg_boost = 0.90 (caution, reduce exposure)
  - Otherwise:              fg_boost = 1.00 (neutral)

No API key required. Free endpoint, rate limit ~30 req/min.
"""

import httpx
from loguru import logger


def get_fear_greed() -> dict:
    """
    Fetch current Fear & Greed index.

    Returns:
        {
            "value": int (0-100),
            "label": str ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"),
            "fg_boost": float (0.90, 1.00, or 1.10),
        }
    """
    try:
        resp = httpx.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Fear & Greed API returned {resp.status_code}")
            return {"value": 50, "label": "Neutral", "fg_boost": 1.0}

        data = resp.json()
        entry = data.get("data", [{}])[0]
        value = int(entry.get("value", 50))
        label = entry.get("value_classification", "Neutral")

        if value < 25:
            fg_boost = 1.10
        elif value > 75:
            fg_boost = 0.90
        else:
            fg_boost = 1.0

        logger.info(f"Fear & Greed: {value} ({label}) → boost={fg_boost}")

        return {
            "value": value,
            "label": label,
            "fg_boost": fg_boost,
        }

    except Exception as e:
        logger.warning(f"Fear & Greed fetch failed: {e}")
        return {"value": 50, "label": "Unknown", "fg_boost": 1.0}


if __name__ == "__main__":
    result = get_fear_greed()
    print(f"Fear & Greed: {result['value']} ({result['label']}) → boost={result['fg_boost']}")
