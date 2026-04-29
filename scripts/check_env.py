"""
Environment Check for Signal Forge V2
Run before testing any agent to verify deps are installed.

Usage:
    python scripts/check_env.py
    -- or --
    make check-env
"""
import sys
import importlib

PASS = "\033[92m\u2713\033[0m"
FAIL = "\033[91m\u2717\033[0m"
WARN = "\033[93m!\033[0m"

print("\nSignal Forge V2 — Environment Check")
print("=" * 45)

errors = 0

# Python version
if sys.version_info >= (3, 10):
    print(f"  {PASS} Python {sys.version_info.major}.{sys.version_info.minor}")
else:
    print(f"  {FAIL} Python {sys.version_info.major}.{sys.version_info.minor} (need 3.10+)")
    errors += 1

# Required packages
required = [
    ("requests",    "requests"),
    ("sqlite3",     "sqlite3"),
    ("pathlib",     "pathlib"),
    ("json",        "json"),
    ("pandas",      "pandas"),
    ("numpy",       "numpy"),
    ("scipy",       "scipy"),
    ("sklearn",     "scikit-learn"),
]

for module, display in required:
    try:
        importlib.import_module(module)
        print(f"  {PASS} {display}")
    except ImportError:
        print(f"  {FAIL} {display} NOT INSTALLED")
        if display not in ["pandas", "numpy", "scipy", "scikit-learn"]:
            errors += 1

# CoinGecko connectivity
print("\n  Checking CoinGecko API connectivity...")
try:
    import requests
    r = requests.get("https://api.coingecko.com/api/v3/ping", timeout=8)
    if r.status_code == 200:
        print(f"  {PASS} CoinGecko API reachable")
    else:
        print(f"  {WARN} CoinGecko returned status {r.status_code}")
except Exception as e:
    print(f"  {FAIL} CoinGecko unreachable: {e}")
    errors += 1

# DB directory
from pathlib import Path
db_dir = Path("db")
if db_dir.exists():
    print(f"  {PASS} db/ directory exists")
else:
    db_dir.mkdir()
    print(f"  {WARN} db/ created (was missing)")

# Logs directory
logs_dir = Path("logs")
if logs_dir.exists():
    print(f"  {PASS} logs/ directory exists")
else:
    logs_dir.mkdir()
    print(f"  {WARN} logs/ created (was missing)")

print("\n" + "=" * 45)
if errors == 0:
    print(f"  {PASS} All checks passed. Ready to run agents.\n")
    print("  Next step:  make test-category\n")
else:
    print(f"  {FAIL} {errors} issue(s) found. Fix above before running agents.\n")
    print("  Run: pip install -r requirements.txt\n")
    sys.exit(1)
