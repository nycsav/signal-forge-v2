"""
altFINS Tool Discovery
======================
One-shot diagnostic — run this ONCE to find the exact tool names
and see the raw response format from altFINS MCP.

This tells us exactly how to fix altfins_shadow.py.

Run with:
    python altfins_discover.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

load_dotenv()

ALTFINS_API_KEY = os.getenv("ALTFINS_API_KEY", "")
ALTFINS_MCP_URL = "https://mcp.altfins.com/mcp"


async def main():
    if not ALTFINS_API_KEY:
        print("ERROR: ALTFINS_API_KEY not set in .env")
        return

    headers = {"X-Api-Key": ALTFINS_API_KEY}

    async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── Step 1: List ALL available tools ─────────────────────────────
            print("\n" + "=" * 60)
            print("AVAILABLE TOOLS ON altFINS MCP SERVER")
            print("=" * 60)

            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"\nTOOL: {tool.name}")
                print(f"  {tool.description[:120] if tool.description else 'no description'}")

            print(f"\nTotal tools: {len(tools.tools)}")

            # ── Step 2: Try screener and print RAW response ────────────────
            print("\n" + "=" * 60)
            print("RAW SCREENER RESPONSE (BTC only)")
            print("=" * 60)

            # Find the screener tool name from the list above
            screener_tool = next(
                (t.name for t in tools.tools if "screener" in t.name.lower()),
                None
            )

            if screener_tool:
                print(f"Using tool: {screener_tool}")
                try:
                    result = await session.call_tool(
                        screener_tool,
                        arguments={
                            "coins": ["BTC"],
                            "displayTypes": [
                                "SHORT_TERM_TREND",
                                "RSI14",
                                "PRICE",
                                "MARKET_CAP",
                            ],
                            "size": 1,
                        },
                    )
                    print("\nRAW CONTENT ITEMS:")
                    for i, item in enumerate(result.content):
                        print(f"\n--- Item {i} (type={type(item).__name__}) ---")
                        if hasattr(item, 'text'):
                            print(item.text[:2000])  # first 2000 chars
                            # Try to pretty-print if JSON
                            try:
                                parsed = json.loads(item.text)
                                print("\nPARSED JSON STRUCTURE:")
                                print(json.dumps(parsed, indent=2)[:2000])
                            except Exception:
                                print("(not valid JSON)")
                except Exception as e:
                    print(f"Screener call failed: {e}")
            else:
                print("No screener tool found! Check tool list above.")

            # ── Step 3: Find signals tool name ────────────────────────────
            print("\n" + "=" * 60)
            print("SIGNALS TOOL NAME")
            print("=" * 60)

            signals_tool = next(
                (t.name for t in tools.tools if "signal" in t.name.lower()),
                None
            )
            if signals_tool:
                print(f"Signals tool: {signals_tool}")
            else:
                print("No signals tool found — check full tool list above")

            print("\nDone. Copy the tool names above into altfins_shadow.py")


if __name__ == "__main__":
    asyncio.run(main())
