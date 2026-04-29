# Signal Forge V2 — Makefile
# Usage: make <target>

.PHONY: help test-category test-email check-env watcher-start watcher-stop watcher-logs signals-db

help:
	@echo ""
	@echo "Signal Forge V2 — Available Commands"
	@echo "======================================"
	@echo "  make check-env        Check Python deps and environment"
	@echo "  make test-category    Run CoinGecko category agent (one-shot)"
	@echo "  make test-email       Run Gmail email signal agent (one-shot)"
	@echo "  make watcher-start    Start 24/7 category watcher (background)"
	@echo "  make watcher-stop     Stop the category watcher"
	@echo "  make watcher-logs     Tail category watcher logs live"
	@echo "  make signals-db       Show last 20 category signals from DB"
	@echo ""

check-env:
	@python scripts/check_env.py

test-category:
	@echo "Running CoinGecko Category Agent..."
	@python scripts/test_category_scan.py

test-email:
	@echo "Running Gmail Email Signal Agent..."
	@python scripts/test_email_scan.py

watcher-start:
	@echo "Starting category watcher in background..."
	@mkdir -p logs
	@nohup python scripts/run_category_watcher.py > logs/category_watcher.log 2>&1 &
	@echo "Watcher PID: $$!"
	@echo "Logs: logs/category_watcher.log"

watcher-stop:
	@pkill -f run_category_watcher.py && echo "Watcher stopped." || echo "No watcher running."

watcher-logs:
	@tail -f logs/category_watcher.log

signals-db:
	@sqlite3 db/category_signals.db \
		"SELECT symbol, category, coin_change_24h, phase, confidence, timestamp \
		 FROM category_signals ORDER BY timestamp DESC LIMIT 20;"
