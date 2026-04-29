"""
CoinGecko Category Agent — Quick Test
Run once to verify agent is working before deploying watcher

Usage:
    cd ~/signal-forge-v2
    python scripts/test_category_scan.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.coingecko_category_agent import scan

print("CoinGecko Category Signal Test")
print("=" * 60)

signals = scan()

print(f"\nRESULTS: {len(signals)} signals fired")
print("=" * 60)

for s in signals:
    print(
        f"  {s['symbol']:8} | "
        f"{s['category'][:28]:28} | "
        f"+{s['coin_change_24h']:5.1f}% 24h | "
        f"+{s['coin_change_1h']:4.1f}% 1h | "
        f"MCap ${s['market_cap']:>12,.0f} | "
        f"Vol/MCap {s['vol_mcap_ratio']:.2f}x | "
        f"Phase {s['phase']} | "
        f"{s['confidence']:.0%} conf"
    )

if not signals:
    print("  No signals this scan. Market may be quiet.")
    print("  Tip: Lower CATEGORY_THRESHOLD to 5.0 in coingecko_category_agent.py")
