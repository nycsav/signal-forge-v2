"""Signal Forge v2 — DeFiLlama Client

Free, no auth, no rate limits. DeFi TVL, yields, stablecoin flows, protocol revenue.
"""

from loguru import logger
import httpx

LLAMA_BASE = "https://api.llama.fi"


async def get_protocol_tvl(protocol: str) -> dict:
    """Get TVL for a specific protocol (e.g., 'aave', 'uniswap')."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{LLAMA_BASE}/protocol/{protocol}")
            if r.status_code == 200:
                d = r.json()
                tvl_history = d.get("tvl", [])
                current_tvl = tvl_history[-1].get("totalLiquidityUSD", 0) if tvl_history else 0
                return {
                    "name": d.get("name", protocol),
                    "tvl": current_tvl,
                    "chain_tvls": {k: v for k, v in d.get("currentChainTvls", {}).items()},
                    "change_1d": d.get("change_1d", 0),
                    "change_7d": d.get("change_7d", 0),
                    "category": d.get("category", ""),
                }
        except Exception as e:
            logger.debug(f"DeFiLlama protocol {protocol} failed: {e}")
    return {}


async def get_top_protocols(limit: int = 20) -> list[dict]:
    """Get top DeFi protocols by TVL."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{LLAMA_BASE}/protocols")
            if r.status_code == 200:
                protocols = r.json()[:limit]
                return [
                    {
                        "name": p.get("name", ""),
                        "symbol": p.get("symbol", ""),
                        "tvl": p.get("tvl", 0),
                        "change_1d": p.get("change_1d", 0),
                        "change_7d": p.get("change_7d", 0),
                        "category": p.get("category", ""),
                        "chains": p.get("chains", [])[:3],
                    }
                    for p in protocols
                ]
        except Exception as e:
            logger.debug(f"DeFiLlama top protocols failed: {e}")
    return []


async def get_stablecoin_flows() -> dict:
    """Get stablecoin market overview — inflows/outflows signal market direction."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{LLAMA_BASE}/stablecoins")
            if r.status_code == 200:
                stables = r.json().get("peggedAssets", [])[:10]
                total_mcap = sum(s.get("circulating", {}).get("peggedUSD", 0) for s in stables)
                return {
                    "total_stablecoin_mcap": total_mcap,
                    "top_stablecoins": [
                        {"name": s.get("name", ""), "symbol": s.get("symbol", ""),
                         "mcap": s.get("circulating", {}).get("peggedUSD", 0)}
                        for s in stables[:5]
                    ],
                }
        except Exception as e:
            logger.debug(f"DeFiLlama stablecoins failed: {e}")
    return {}


async def get_yields(min_tvl: float = 1_000_000) -> list[dict]:
    """Get top yield opportunities across DeFi."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{LLAMA_BASE}/pools")
            if r.status_code == 200:
                pools = [p for p in r.json().get("data", []) if p.get("tvlUsd", 0) >= min_tvl]
                pools.sort(key=lambda p: p.get("apy", 0), reverse=True)
                return [
                    {
                        "pool": p.get("pool", ""),
                        "project": p.get("project", ""),
                        "chain": p.get("chain", ""),
                        "symbol": p.get("symbol", ""),
                        "tvl": p.get("tvlUsd", 0),
                        "apy": p.get("apy", 0),
                        "apy_base": p.get("apyBase", 0),
                        "apy_reward": p.get("apyReward", 0),
                    }
                    for p in pools[:20]
                ]
        except Exception as e:
            logger.debug(f"DeFiLlama yields failed: {e}")
    return []


async def get_chain_tvl() -> list[dict]:
    """Get TVL by blockchain — shows where capital is flowing."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{LLAMA_BASE}/v2/chains")
            if r.status_code == 200:
                chains = r.json()[:15]
                return [
                    {"name": c.get("name", ""), "tvl": c.get("tvl", 0),
                     "tokenSymbol": c.get("tokenSymbol", "")}
                    for c in chains
                ]
        except Exception as e:
            logger.debug(f"DeFiLlama chain TVL failed: {e}")
    return []
