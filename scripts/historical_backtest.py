#!/usr/bin/env python3
"""Signal Forge v2 — Fast Historical Replay Backtest

Replays 90 days of hourly OHLCV through the signal pipeline and simulates
trades using the same 7-layer exit logic the live MonitorAgent uses.

Data sources (no new API keys):
  - Binance public klines           (1h OHLCV, no auth)
  - CoinGecko free tier             (market_chart fallback, key in .env)
  - Alternative.me Fear & Greed     (daily, no auth)

Run at 3 signal-score thresholds in one invocation so you can compare
which threshold produces the best risk-adjusted return.

Usage:
    python scripts/historical_backtest.py
    python scripts/historical_backtest.py --days 90 --thresholds 55,62,68
    python scripts/historical_backtest.py --symbols BTC,ETH,SOL
    python scripts/historical_backtest.py --db data/backtest_trades.db
"""

import argparse
import math
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

# Project imports (for authoritative scoring)
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.scoring import SignalScorer  # noqa: E402
from agents.events import TechnicalEvent, SentimentEvent  # noqa: E402


# ── Defaults ──────────────────────────────────────────────────────

DEFAULT_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "MATIC-USD", "ADA-USD",
]
DEFAULT_DAYS = 90
DEFAULT_THRESHOLDS = [55, 62, 68]
DEFAULT_DB = PROJECT_ROOT / "data" / "backtest_trades.db"

# Exit parameters — mirror MonitorAgent
ATR_STOP_MULT = 2.5
TP1_ATR_MULT = 1.5
TP2_ATR_MULT = 3.0
TP3_ATR_MULT = 5.0
TP1_CLOSE_PCT = 0.40
TP2_CLOSE_PCT = 0.30
TP3_CLOSE_PCT = 0.30
MAX_HOLD_HOURS = 72

# Position sizing
NOTIONAL_USD = 1000.0  # per-trade notional; backtest only uses this for pnl_usd reporting


# ── Data fetching ─────────────────────────────────────────────────

def symbol_to_binance(symbol: str) -> str:
    """BTC-USD → BTCUSDT"""
    base = symbol.replace("-USD", "").replace("/USD", "").upper()
    return f"{base}USDT"


def fetch_binance_klines(symbol: str, days: int) -> list[list]:
    """Fetch `days` of 1h klines from Binance public REST with startTime pagination.

    Returns raw kline arrays. Empty list on failure.
    """
    bn_symbol = symbol_to_binance(symbol)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    all_rows: list[list] = []
    cursor = start_ms
    url = "https://api.binance.com/api/v3/klines"

    with httpx.Client(timeout=20.0) as client:
        while cursor < end_ms:
            try:
                r = client.get(url, params={
                    "symbol": bn_symbol,
                    "interval": "1h",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                })
            except Exception as e:
                print(f"  ! {symbol}: binance error {e}", file=sys.stderr)
                return []

            if r.status_code != 200:
                if r.status_code == 400:
                    # Pair doesn't exist on Binance
                    print(f"  ! {symbol} ({bn_symbol}) not on Binance", file=sys.stderr)
                    return []
                print(f"  ! {symbol}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
                return []

            rows = r.json()
            if not rows:
                break

            all_rows.extend(rows)

            # Advance cursor past the last candle's close_time
            last_open_ms = int(rows[-1][0])
            next_cursor = last_open_ms + 3_600_000  # +1h
            if next_cursor <= cursor:
                break
            cursor = next_cursor

            if len(rows) < 1000:
                break  # No more data
            time.sleep(0.05)  # Cheap rate limit courtesy

    return all_rows


def fetch_coinbase_candles(symbol: str, days: int) -> list[list]:
    """Fetch `days` of 1h candles from Coinbase Exchange public API.

    Returns rows in Binance-compatible format:
        [open_time_ms, open, high, low, close, volume, ...]
    Empty list on failure. Used as fallback when Binance is region-blocked.
    """
    product = symbol if "-USD" in symbol else f"{symbol.upper()}-USD"
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    granularity = 3600  # 1h
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=days)
    # Coinbase caps at 300 candles per call → 300 * 1h = 12.5 days. Page backwards.
    page_seconds = 300 * granularity
    all_rows: list[list] = []

    with httpx.Client(timeout=20.0) as client:
        cursor_end = end
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(seconds=page_seconds))
            try:
                r = client.get(url, params={
                    "granularity": granularity,
                    "start": cursor_start.isoformat(),
                    "end": cursor_end.isoformat(),
                })
            except Exception as e:
                print(f"  ! {symbol}: coinbase error {e}", file=sys.stderr)
                return []
            if r.status_code != 200:
                print(f"  ! {symbol}: coinbase HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
                return []
            page = r.json()
            if not page:
                break
            # Coinbase returns [time_s, low, high, open, close, volume] sorted DESC
            for row in page:
                t_s, lo, hi, op, cl, vol = row
                all_rows.append([
                    int(t_s) * 1000,  # open_time_ms
                    float(op), float(hi), float(lo), float(cl), float(vol),
                ])
            cursor_end = cursor_start
            time.sleep(0.15)  # public rate limit ~10 req/s

    # Dedupe & sort ascending by open_time_ms
    seen = set()
    out = []
    for r in sorted(all_rows, key=lambda x: x[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    return out


def fetch_ohlcv(symbol: str, days: int) -> tuple[list[list], str]:
    """Try Binance first, fall back to Coinbase. Returns (rows, source_name)."""
    rows = fetch_binance_klines(symbol, days)
    if len(rows) >= 60:
        return rows, "binance"
    rows = fetch_coinbase_candles(symbol, days)
    return rows, "coinbase"


def fetch_fear_greed_daily(days: int = 365) -> dict[str, int]:
    """Fetch daily Fear & Greed index. Returns {YYYY-MM-DD: int_value}."""
    try:
        r = httpx.get(
            "https://api.alternative.me/fng/",
            params={"limit": days, "format": "json"},
            timeout=15.0,
        )
        if r.status_code != 200:
            print(f"  ! F&G HTTP {r.status_code}", file=sys.stderr)
            return {}
        data = r.json().get("data", [])
        result = {}
        for entry in data:
            ts = int(entry["timestamp"])
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            result[date] = int(entry["value"])
        return result
    except Exception as e:
        print(f"  ! F&G error {e}", file=sys.stderr)
        return {}


# ── Indicator computation (numpy, vectorized) ────────────────────

def rsi_14(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI. Returns array same length as closes, NaN for warmup."""
    deltas = np.diff(closes, prepend=closes[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.full_like(closes, np.nan, dtype=float)
    avg_loss = np.full_like(closes, np.nan, dtype=float)

    if len(closes) <= period:
        return np.full_like(closes, 50.0, dtype=float)

    avg_gain[period] = gains[1:period + 1].mean()
    avg_loss[period] = losses[1:period + 1].mean()

    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    rs = avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss)
    rsi = 100 - (100 / (1 + rs))
    return np.nan_to_num(rsi, nan=50.0)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return np.full_like(values, values[0] if len(values) else 0.0, dtype=float)
    alpha = 2.0 / (period + 1)
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    # Back-fill warmup with first valid
    first_valid = out[period - 1]
    out[:period - 1] = first_valid
    return out


def atr_14(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range (Wilder)."""
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    out = np.full_like(close, np.nan, dtype=float)
    if len(close) <= period:
        return np.full_like(close, (high[0] - low[0]) or 1e-6, dtype=float)
    out[period] = tr[1:period + 1].mean()
    for i in range(period + 1, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    out[:period] = out[period]
    return out


def macd_histogram(closes: np.ndarray) -> np.ndarray:
    """MACD histogram: EMA12 - EMA26 minus signal EMA9."""
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = ema(macd_line, 9)
    return macd_line - signal_line


def bollinger_position(closes: np.ndarray, period: int = 20, std_mult: float = 2.0) -> np.ndarray:
    """Position within Bollinger Bands: 0 = lower, 1 = upper."""
    out = np.full_like(closes, 0.5, dtype=float)
    for i in range(period, len(closes)):
        window = closes[i - period:i]
        mean = window.mean()
        std = window.std()
        if std == 0:
            continue
        upper = mean + std_mult * std
        lower = mean - std_mult * std
        pos = (closes[i] - lower) / (upper - lower) if upper > lower else 0.5
        out[i] = max(0.0, min(1.0, pos))
    return out


def volume_ratio(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    """Current volume / avg(last period)."""
    out = np.ones_like(volumes, dtype=float)
    for i in range(period, len(volumes)):
        avg = volumes[i - period:i].mean()
        out[i] = volumes[i] / avg if avg > 0 else 1.0
    return out


# ── Regime mapping (mirrors agents/regime_engine.py) ──────────────

def fg_to_regime(fear_greed: int) -> str:
    if fear_greed < 15:
        return "capitulation"
    elif fear_greed < 25:
        return "extreme_fear"
    elif fear_greed < 40:
        return "fear"
    elif fear_greed < 60:
        return "neutral"
    elif fear_greed < 75:
        return "greed"
    elif fear_greed < 90:
        return "extreme_greed"
    return "euphoria"


# ── DB setup ──────────────────────────────────────────────────────

BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    order_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
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
    status TEXT DEFAULT 'open',
    broker TEXT DEFAULT 'backtest',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    -- backtest extensions
    regime TEXT,
    score_threshold REAL,
    fear_greed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bt_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_bt_threshold ON trades(score_threshold);
CREATE INDEX IF NOT EXISTS idx_bt_regime ON trades(regime);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(BACKTEST_SCHEMA)
    conn.commit()
    return conn


def reset_threshold_rows(conn: sqlite3.Connection, threshold: float):
    """Delete any prior rows for this threshold so repeat runs don't accumulate."""
    conn.execute("DELETE FROM trades WHERE score_threshold = ?", (threshold,))
    conn.commit()


# ── Core replay ───────────────────────────────────────────────────

def build_indicators(klines: list[list]) -> dict:
    """Convert raw Binance klines into numpy arrays + precomputed indicators."""
    arr = np.array(klines, dtype=object)
    open_time = arr[:, 0].astype(np.int64)
    high = arr[:, 2].astype(float)
    low = arr[:, 3].astype(float)
    close = arr[:, 4].astype(float)
    volume = arr[:, 5].astype(float)

    return {
        "open_time_ms": open_time,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "rsi": rsi_14(close),
        "ema20": ema(close, 20),
        "ema50": ema(close, 50),
        "atr": atr_14(high, low, close),
        "macd_hist": macd_histogram(close),
        "bb_pos": bollinger_position(close),
        "vol_ratio": volume_ratio(volume),
    }


def score_candle(
    scorer: SignalScorer,
    symbol: str,
    i: int,
    ind: dict,
    fear_greed: int,
) -> tuple[float, float, float]:
    """Build TechnicalEvent + SentimentEvent and run the production scorer.

    Returns (composite_score, technical_score, sentiment_score).
    """
    ts = datetime.fromtimestamp(int(ind["open_time_ms"][i]) / 1000, tz=timezone.utc).replace(tzinfo=None)
    ema_aligned = ind["ema20"][i] > ind["ema50"][i]

    tech = TechnicalEvent(
        timestamp=ts,
        symbol=symbol,
        rsi_14=float(ind["rsi"][i]),
        macd_histogram=float(ind["macd_hist"][i]),
        bb_position=float(ind["bb_pos"][i]),
        ema_alignment=bool(ema_aligned),
        volume_ratio=float(ind["vol_ratio"][i]),
        atr_14_pct=float(ind["atr"][i] / ind["close"][i]) if ind["close"][i] > 0 else 0.0,
        ichimoku_signal="above_cloud" if ema_aligned else "in_cloud",
    )
    sent = SentimentEvent(
        timestamp=ts,
        symbol=symbol,
        fear_greed=int(fear_greed),
    )

    tech_score = scorer.score_technical(tech)
    sent_score = scorer.score_sentiment(sent)
    # No on-chain / AI analyst in historical replay — use neutral 50
    composite, _ = scorer.composite_score(tech_score, sent_score, 50.0, 50.0)
    return composite, tech_score, sent_score


def simulate_symbol(
    symbol: str,
    ind: dict,
    fg_daily: dict[str, int],
    scorer: SignalScorer,
    threshold: float,
) -> list[dict]:
    """Walk hourly candles, open/manage/close positions. Returns list of trade dicts."""
    n = len(ind["close"])
    trades: list[dict] = []
    position: dict | None = None

    warmup = 50  # Need EMA50 valid

    for i in range(warmup, n):
        ts_ms = int(ind["open_time_ms"][i])
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        date_key = ts.strftime("%Y-%m-%d")
        fg = fg_daily.get(date_key, 50)
        regime = fg_to_regime(fg)

        close_px = float(ind["close"][i])
        high_px = float(ind["high"][i])
        low_px = float(ind["low"][i])
        atr = float(ind["atr"][i])

        # ── Manage open position first ──
        if position is not None:
            entry = position["entry"]
            hold_hours = i - position["entry_idx"]

            # Update MFE/MAE (intrabar using high/low)
            if high_px > position["peak"]:
                position["peak"] = high_px
            if low_px < position["trough"]:
                position["trough"] = low_px

            # 1. Hard stop (intrabar — check low)
            if low_px <= position["stop"]:
                exit_px = position["stop"]
                trades.append(_close_trade(position, exit_px, "hard_stop", hold_hours, symbol, regime, fg, threshold))
                position = None
                continue

            # 2. TP3 — close all (intrabar — check high)
            if high_px >= position["tp3"] and position["qty_frac"] > 0:
                trades.append(_close_trade(position, position["tp3"], "tp3", hold_hours, symbol, regime, fg, threshold))
                position = None
                continue

            # 3. TP2 — close TP2_CLOSE_PCT of original
            if high_px >= position["tp2"] and not position["tp2_hit"]:
                position["tp2_hit"] = True
                position["qty_frac"] -= TP2_CLOSE_PCT
                # Record partial as a trade (pro-rated)
                trades.append(_close_partial(position, position["tp2"], TP2_CLOSE_PCT, "tp2", hold_hours, symbol, regime, fg, threshold))

            # 4. TP1 — close TP1_CLOSE_PCT of original, move stop to breakeven
            if high_px >= position["tp1"] and not position["tp1_hit"]:
                position["tp1_hit"] = True
                position["qty_frac"] -= TP1_CLOSE_PCT
                position["stop"] = entry  # breakeven
                trades.append(_close_partial(position, position["tp1"], TP1_CLOSE_PCT, "tp1", hold_hours, symbol, regime, fg, threshold))

            # 5. Time stop
            if hold_hours >= MAX_HOLD_HOURS and position["qty_frac"] > 0:
                trades.append(_close_trade(position, close_px, "time_72h", hold_hours, symbol, regime, fg, threshold))
                position = None
                continue

            # If we've closed everything via partials, clear
            if position and position["qty_frac"] <= 0.0001:
                position = None
                continue

        # ── Check entry signal (no position) ──
        if position is None:
            if atr <= 0:
                continue

            composite, tech_score, sent_score = score_candle(scorer, symbol, i, ind, fg)

            if composite >= threshold:
                entry_px = close_px
                stop = entry_px - atr * ATR_STOP_MULT
                tp1 = entry_px + atr * TP1_ATR_MULT
                tp2 = entry_px + atr * TP2_ATR_MULT
                tp3 = entry_px + atr * TP3_ATR_MULT

                position = {
                    "entry": entry_px,
                    "entry_idx": i,
                    "entry_ts": ts,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "qty_frac": 1.0,
                    "peak": high_px,
                    "trough": low_px,
                    "signal_score": composite,
                    "tech_score": tech_score,
                    "sent_score": sent_score,
                    "entry_regime": regime,
                    "entry_fg": fg,
                }

    # Close any position still open at end of series
    if position is not None:
        hold_hours = (n - 1) - position["entry_idx"]
        trades.append(_close_trade(position, float(ind["close"][-1]), "end_of_data", hold_hours, symbol, fg_to_regime(50), 50, threshold))

    return trades


def _close_trade(pos: dict, exit_px: float, reason: str, hold_hours: float,
                  symbol: str, regime: str, fg: int, threshold: float) -> dict:
    """Close the full remaining quantity."""
    qty_frac = pos["qty_frac"]
    entry = pos["entry"]
    pnl_pct = (exit_px - entry) / entry
    pnl_usd = pnl_pct * NOTIONAL_USD * qty_frac
    mfe = (pos["peak"] - entry) / entry
    mae = (pos["trough"] - entry) / entry
    return {
        "proposal_id": f"bt_{threshold:.0f}_{symbol}_{pos['entry_idx']}_{uuid.uuid4().hex[:6]}",
        "symbol": symbol,
        "direction": "long",
        "entry_price": entry,
        "exit_price": exit_px,
        "stop_price": pos["stop"],
        "tp1_price": pos["tp1"],
        "tp2_price": pos["tp2"],
        "tp3_price": pos["tp3"],
        "size_usd": NOTIONAL_USD * qty_frac,
        "quantity": (NOTIONAL_USD * qty_frac) / entry,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "signal_score": pos["signal_score"],
        "close_reason": reason,
        "hold_time_hours": hold_hours,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "opened_at": pos["entry_ts"].isoformat(),
        "closed_at": (pos["entry_ts"] + timedelta(hours=hold_hours)).isoformat(),
        "regime": pos["entry_regime"],
        "score_threshold": threshold,
        "fear_greed": pos["entry_fg"],
        "ai_rationale": f"tech={pos['tech_score']:.0f} sent={pos['sent_score']:.0f}",
    }


def _close_partial(pos: dict, exit_px: float, frac: float, reason: str, hold_hours: float,
                    symbol: str, regime: str, fg: int, threshold: float) -> dict:
    """Record a partial close (TP1/TP2) as its own row for reporting simplicity."""
    entry = pos["entry"]
    pnl_pct = (exit_px - entry) / entry
    pnl_usd = pnl_pct * NOTIONAL_USD * frac
    return {
        "proposal_id": f"bt_{threshold:.0f}_{symbol}_{pos['entry_idx']}_{reason}_{uuid.uuid4().hex[:6]}",
        "symbol": symbol,
        "direction": "long",
        "entry_price": entry,
        "exit_price": exit_px,
        "stop_price": pos["stop"],
        "tp1_price": pos["tp1"],
        "tp2_price": pos["tp2"],
        "tp3_price": pos["tp3"],
        "size_usd": NOTIONAL_USD * frac,
        "quantity": (NOTIONAL_USD * frac) / entry,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "signal_score": pos["signal_score"],
        "close_reason": reason,
        "hold_time_hours": hold_hours,
        "max_favorable_excursion": (pos["peak"] - entry) / entry,
        "max_adverse_excursion": (pos["trough"] - entry) / entry,
        "opened_at": pos["entry_ts"].isoformat(),
        "closed_at": (pos["entry_ts"] + timedelta(hours=hold_hours)).isoformat(),
        "regime": pos["entry_regime"],
        "score_threshold": threshold,
        "fear_greed": pos["entry_fg"],
        "ai_rationale": f"partial tech={pos['tech_score']:.0f} sent={pos['sent_score']:.0f}",
    }


def write_trades(conn: sqlite3.Connection, trades: list[dict]):
    if not trades:
        return
    cols = [
        "proposal_id", "symbol", "direction", "entry_price", "exit_price",
        "stop_price", "tp1_price", "tp2_price", "tp3_price", "size_usd",
        "quantity", "pnl_usd", "pnl_pct", "signal_score", "ai_rationale",
        "close_reason", "hold_time_hours", "max_favorable_excursion",
        "max_adverse_excursion", "status", "broker", "opened_at", "closed_at",
        "regime", "score_threshold", "fear_greed",
    ]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO trades ({','.join(cols)}) VALUES ({placeholders})"
    rows = [
        tuple(t.get(c, "closed" if c == "status" else "backtest" if c == "broker" else None) for c in cols)
        for t in trades
    ]
    conn.executemany(sql, rows)
    conn.commit()


# ── Summary stats ─────────────────────────────────────────────────

def summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "trades": 0}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]

    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 0
    std = math.sqrt(var)
    sharpe = (mean / (std + 1e-9)) * math.sqrt(365 * 24) if std > 0 else 0.0

    downside = [p for p in pnls if p < 0]
    dstd = math.sqrt(sum(p * p for p in downside) / len(downside)) if downside else 0
    sortino = (mean / (dstd + 1e-9)) * math.sqrt(365 * 24) if dstd > 0 else 0.0

    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else float("inf")

    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    regime_counts: dict[str, int] = {}
    for t in trades:
        regime_counts[t["regime"]] = regime_counts.get(t["regime"], 0) + 1
    top_regimes = sorted(regime_counts.items(), key=lambda kv: -kv[1])[:3]

    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t["close_reason"]] = exit_counts.get(t["close_reason"], 0) + 1

    return {
        "label": label,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl_usd": total_pnl_usd,
        "avg_win_pct": avg_win * 100,
        "avg_loss_pct": avg_loss * 100,
        "rr": rr,
        "sharpe": sharpe,
        "sortino": sortino,
        "profit_factor": pf,
        "top_regimes": top_regimes,
        "exit_breakdown": exit_counts,
    }


def print_summary(s: dict):
    if s["trades"] == 0:
        print(f"  {s['label']:>12} │ NO TRADES")
        return
    top3 = ", ".join(f"{r}({c})" for r, c in s["top_regimes"])
    print(
        f"  {s['label']:>12} │ "
        f"n={s['trades']:<4} "
        f"win={s['win_rate']:5.1f}% "
        f"pnl=${s['total_pnl_usd']:+8.2f} "
        f"sharpe={s['sharpe']:5.2f} "
        f"R:R={s['rr']:4.2f} "
        f"PF={s['profit_factor']:5.2f}"
    )
    print(f"               │   avg_win={s['avg_win_pct']:+5.2f}% avg_loss={s['avg_loss_pct']:+5.2f}%  top_regimes=[{top3}]")
    exits = " ".join(f"{k}={v}" for k, v in sorted(s["exit_breakdown"].items(), key=lambda kv: -kv[1]))
    print(f"               │   exits: {exits}")


# ── Main ──────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Signal Forge v2 — Historical Backtest")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Lookback days (default {DEFAULT_DAYS})")
    p.add_argument("--symbols", type=str, default=",".join(s.replace("-USD", "") for s in DEFAULT_SYMBOLS),
                   help="Comma-separated base symbols (e.g. BTC,ETH,SOL)")
    p.add_argument("--thresholds", type=str,
                   default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
                   help="Comma-separated signal-score thresholds to evaluate")
    p.add_argument("--db", type=str, default=str(DEFAULT_DB),
                   help=f"Output SQLite path (default {DEFAULT_DB})")
    args = p.parse_args()

    symbols = [f"{s.strip().upper()}-USD" for s in args.symbols.split(",") if s.strip()]
    thresholds = [float(t.strip()) for t in args.thresholds.split(",") if t.strip()]
    db_path = Path(args.db)

    t0 = time.time()
    print(f"Signal Forge v2 — Historical Backtest")
    print(f"  days={args.days}  symbols={len(symbols)}  thresholds={thresholds}  db={db_path}")
    print()

    # F&G once
    print("Fetching Fear & Greed history...")
    fg_daily = fetch_fear_greed_daily(days=max(args.days + 10, 120))
    print(f"  got {len(fg_daily)} days")

    # Fetch + indicators for every symbol (once)
    print(f"\nFetching 1h klines from Binance ({args.days}d × {len(symbols)} symbols)...")
    per_symbol_ind: dict[str, dict] = {}
    for sym in symbols:
        rows, source = fetch_ohlcv(sym, args.days)
        if len(rows) < 60:
            print(f"  · {sym}: only {len(rows)} candles ({source}), skipping")
            continue
        per_symbol_ind[sym] = build_indicators(rows)
        print(f"  · {sym}: {len(rows)} candles ({source})")

    if not per_symbol_ind:
        print("\nNo symbol data fetched. Aborting.")
        return

    # Init DB + scorer
    conn = init_db(db_path)
    scorer = SignalScorer()

    # Run each threshold
    all_results: dict[float, dict] = {}
    print(f"\nReplaying through signal pipeline at {len(thresholds)} thresholds...")
    for thr in thresholds:
        reset_threshold_rows(conn, thr)
        threshold_trades: list[dict] = []
        t_thr = time.time()
        for sym, ind in per_symbol_ind.items():
            sym_trades = simulate_symbol(sym, ind, fg_daily, scorer, thr)
            threshold_trades.extend(sym_trades)
        write_trades(conn, threshold_trades)
        summary = summarize(threshold_trades, label=f"thr={thr:.0f}")
        all_results[thr] = summary
        print(f"  · thr={thr:.0f} → {len(threshold_trades)} trades ({time.time() - t_thr:.1f}s)")

    conn.close()

    # Final comparison table
    print(f"\n{'=' * 78}")
    print(f"  SUMMARY BY THRESHOLD  ({time.time() - t0:.1f}s total)")
    print(f"{'=' * 78}")
    for thr in sorted(all_results.keys()):
        print_summary(all_results[thr])
        print()

    # Pick best by Sharpe
    viable = {t: s for t, s in all_results.items() if s.get("trades", 0) >= 10}
    if viable:
        best = max(viable.items(), key=lambda kv: kv[1]["sharpe"])
        print(f"  Best by Sharpe: threshold={best[0]:.0f}  sharpe={best[1]['sharpe']:.2f}  trades={best[1]['trades']}")
    else:
        print("  No threshold produced ≥10 trades — results not statistically meaningful.")
    print(f"\n  DB written: {db_path}")
    print(f"  Run `python scripts/backtest_report.py --db {db_path}` to re-print this report from the DB.")


if __name__ == "__main__":
    main()
