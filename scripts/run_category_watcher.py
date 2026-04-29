"""
CoinGecko Category Watcher — 24/7 Mac Mini Service
Scans every 30 minutes, logs all signals to file

Setup (run once to register with macOS launchd):
    launchctl load ~/Library/LaunchAgents/com.signalforge.category-watcher.plist

Manual start:
    python scripts/run_category_watcher.py

Check logs:
    tail -f ~/signal-forge-v2/logs/category_watcher.log

Check signals DB:
    sqlite3 ~/signal-forge-v2/db/category_signals.db \\
      "SELECT symbol, category, coin_change_24h, phase, timestamp \\
       FROM category_signals ORDER BY timestamp DESC LIMIT 20;"
"""
import time
import logging
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.coingecko_category_agent import scan

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "category_watcher.log"),
        logging.StreamHandler()
    ]
)

SCAN_INTERVAL_SEC = 1800  # 30 minutes

if __name__ == "__main__":
    logging.info("=" * 50)
    logging.info("Signal Forge V2 — Category Watcher started")
    logging.info(f"Scan interval: {SCAN_INTERVAL_SEC // 60} minutes")
    logging.info(f"DB: {Path(__file__).parent.parent / 'db' / 'category_signals.db'}")
    logging.info("=" * 50)

    scan_count = 0
    while True:
        scan_count += 1
        logging.info(f"--- Scan #{scan_count} ---")
        try:
            signals = scan()
            logging.info(f"Scan #{scan_count}: {len(signals)} signals fired")
            for s in signals:
                logging.info(
                    f"  SIGNAL {s['symbol']} | {s['category']} | "
                    f"+{s['coin_change_24h']:.1f}% | Phase {s['phase']} | "
                    f"{s['confidence']:.0%} | MCap ${s['market_cap']:,.0f}"
                )
        except KeyboardInterrupt:
            logging.info("Watcher stopped (KeyboardInterrupt)")
            sys.exit(0)
        except Exception as e:
            logging.error(f"Scan #{scan_count} failed: {e}")

        logging.info(f"Sleeping {SCAN_INTERVAL_SEC // 60}min until next scan...")
        time.sleep(SCAN_INTERVAL_SEC)
