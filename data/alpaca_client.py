"""Alpaca paper trading client — positions, orders, account."""

import httpx
from loguru import logger


class AlpacaClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://paper-api.alpaca.markets"):
        self.base_url = base_url
        self.headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        }

    async def get_account(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"{self.base_url}/v2/account", headers=self.headers)
                if r.status_code == 200:
                    a = r.json()
                    return {
                        "portfolio_value": float(a.get("portfolio_value", 0)),
                        "cash": float(a.get("cash", 0)),
                        "buying_power": float(a.get("buying_power", 0)),
                        "equity": float(a.get("equity", 0)),
                        "status": a.get("status", "unknown"),
                    }
            except Exception as e:
                logger.error(f"Alpaca account error: {e}")
        return {}

    async def get_positions(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(f"{self.base_url}/v2/positions", headers=self.headers)
                if r.status_code == 200:
                    return [
                        {
                            "symbol": p.get("symbol", ""),
                            "qty": float(p.get("qty", 0)),
                            "avg_entry": float(p.get("avg_entry_price", 0)),
                            "current_price": float(p.get("current_price", 0)),
                            "market_value": float(p.get("market_value", 0)),
                            "unrealized_pl": float(p.get("unrealized_pl", 0)),
                            "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                            "side": p.get("side", "long"),
                        }
                        for p in r.json()
                    ]
            except Exception as e:
                logger.error(f"Alpaca positions error: {e}")
        return []

    async def get_orders(self, limit: int = 50) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                r = await client.get(
                    f"{self.base_url}/v2/orders",
                    headers=self.headers,
                    params={"status": "all", "limit": limit, "direction": "desc"},
                )
                if r.status_code == 200:
                    return [
                        {
                            "id": o.get("id", ""),
                            "symbol": o.get("symbol", ""),
                            "side": o.get("side", ""),
                            "qty": o.get("qty") or o.get("filled_qty", "0"),
                            "filled_qty": o.get("filled_qty", "0"),
                            "filled_avg_price": o.get("filled_avg_price"),
                            "status": o.get("status", ""),
                            "submitted_at": o.get("submitted_at", ""),
                            "filled_at": o.get("filled_at"),
                            "type": o.get("type", ""),
                        }
                        for o in r.json()
                    ]
            except Exception as e:
                logger.error(f"Alpaca orders error: {e}")
        return []
