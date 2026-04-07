-- Signal Forge v2 — Live Trading Database
-- Separate from paper trades. Sacred accounting.

CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    quantity REAL,
    size_usd REAL,
    stop_price REAL,
    tp1_price REAL,
    tp2_price REAL,
    fee_usd REAL DEFAULT 0,
    pnl_usd REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,
    pnl_after_fees REAL DEFAULT 0,
    signal_score REAL,
    ai_confidence REAL,
    consensus INTEGER DEFAULT 0,
    fib_level TEXT,
    fib_confluence INTEGER DEFAULT 0,
    exit_reason TEXT,
    hold_minutes REAL,
    status TEXT DEFAULT 'open',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS live_daily_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    starting_balance REAL,
    ending_balance REAL,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    total_fees REAL DEFAULT 0,
    trades_opened INTEGER DEFAULT 0,
    trades_closed INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL,
    max_drawdown_pct REAL DEFAULT 0,
    halted INTEGER DEFAULT 0,
    halt_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS live_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    trade_id TEXT,
    data_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_live_trades_status ON live_trades(status);
CREATE INDEX IF NOT EXISTS idx_live_trades_symbol ON live_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_live_daily_date ON live_daily_pnl(date);
