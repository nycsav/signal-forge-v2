-- Signal Forge v2 — Database Schema
-- 7 tables as specified in the architecture

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    order_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,  -- 'long' or 'short'
    entry_price REAL,
    exit_price REAL,
    stop_price REAL,
    tp1_price REAL,
    tp2_price REAL,
    tp3_price REAL,
    size_usd REAL,
    quantity REAL,
    pnl_usd REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,
    signal_score REAL,
    ai_confidence REAL,
    ai_rationale TEXT,
    risk_score REAL,
    close_reason TEXT,
    hold_time_hours REAL,
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    status TEXT DEFAULT 'open',  -- open, closed, cancelled
    broker TEXT DEFAULT 'alpaca',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    raw_score REAL,
    ai_confidence REAL,
    direction TEXT,
    ai_rationale TEXT,
    score_breakdown TEXT,  -- JSON
    decision TEXT,  -- 'proposed', 'approved', 'vetoed', 'skipped'
    veto_reason TEXT,
    fear_greed INTEGER,
    market_regime TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS position_state (
    symbol TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL,
    stop_price REAL NOT NULL,
    tp1_price REAL,
    tp2_price REAL,
    tp3_price REAL,
    quantity REAL NOT NULL,
    size_usd REAL,
    hwm REAL DEFAULT 0,
    trailing_active INTEGER DEFAULT 0,
    tp1_hit INTEGER DEFAULT 0,
    tp2_hit INTEGER DEFAULT 0,
    signal_score REAL,
    opened_at TEXT NOT NULL,
    last_checked TEXT
);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT,
    payload TEXT,  -- JSON
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scoring_weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    weights TEXT NOT NULL,  -- JSON: {"technical": 0.35, "sentiment": 0.15, ...}
    training_window_trades INTEGER,
    sharpe_improvement REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trade_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    symbol TEXT NOT NULL,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    pnl_pct REAL,
    signal_score REAL,
    ai_confidence REAL,
    ai_rationale TEXT,
    fear_greed INTEGER,
    outcome TEXT,  -- 'win', 'loss', 'breakeven'
    opened_at TEXT,
    closed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    volume_24h REAL,
    fear_greed INTEGER,
    market_regime TEXT,
    atr_14 REAL,
    rsi_14 REAL,
    data_json TEXT,  -- full snapshot
    created_at TEXT DEFAULT (datetime('now'))
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals_log(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_agent ON agent_events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON market_snapshots(symbol, timestamp);
