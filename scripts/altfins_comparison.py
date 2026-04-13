#!/usr/bin/env python3
"""SignalForge v2 — altFINS vs Our Signals Comparison Backtest

Fetches 90 days of altFINS BULLISH signals via MCP, then replays the same
period through 3 variants on the same OHLCV + exit stack:

  A. our-signals-only     — entry when composite_score >= 55
  B. altfins-only         — entry when altFINS has a BULLISH signal for symbol+day
  C. combined             — entry when BOTH A and B agree

Outputs a side-by-side comparison table:
  total trades, win rate, Sharpe, avg R:R, max drawdown, top regimes

Usage:
    PYTHONPATH=. python scripts/altfins_comparison.py
    PYTHONPATH=. python scripts/altfins_comparison.py --days 90
"""

import asyncio
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Reuse heavy lifting from the existing backtest
from scripts.historical_backtest import (
    fetch_ohlcv, fetch_fear_greed_daily, build_indicators, fg_to_regime,
    init_db, reset_threshold_rows, write_trades, summarize, print_summary,
    ATR_STOP_MULT, TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
    TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT, MAX_HOLD_HOURS,
    NOTIONAL_USD, _close_trade, _close_partial,
)
from agents.scoring import SignalScorer
from agents.events import TechnicalEvent, SentimentEvent

# ── Config ────────────────────────────────────────────────────────

DEFAULT_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "ADA-USD",
]
DEFAULT_DAYS = 90
OUR_THRESHOLD = 55
DB_PATH = PROJECT_ROOT / "data" / "altfins_comparison.db"

# Variant tags stored in score_threshold column for DB + reporting
TAG_OUR = 1055        # our-signals-only at threshold 55
TAG_ALTFINS = 2055    # altfins-only
TAG_COMBINED = 3055   # combined (both agree)

ALTFINS_MCP_URL = "https://mcp.altfins.com/mcp"
ALTFINS_SIGNAL_TOOL = "signal_feed_data"

# ── altFINS signal fetching ──────────────────────────────────────

async def fetch_altfins_signals(api_key: str, coins: list[str], days: int) -> list[dict]:
    """Fetch BULLISH signals from altFINS MCP in weekly chunks over `days` days.

    Returns list of dicts with at minimum: symbol, timestamp/date, direction,
    signalKey/signalName.
    """
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError:
        print("  ! mcp library not available, skipping altFINS fetch", file=sys.stderr)
        return []

    headers = {"X-Api-Key": api_key}
    all_signals: list[dict] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # Page in 7-day chunks to respect rate limits / pagination
    chunk_days = 7
    cursor = start
    chunk_num = 0

    while cursor < now:
        chunk_end = min(cursor + timedelta(days=chunk_days), now)
        from_str = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        chunk_num += 1

        try:
            async with streamablehttp_client(ALTFINS_MCP_URL, headers=headers) as (
                read, write, _
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        ALTFINS_SIGNAL_TOOL,
                        arguments={
                            "coins": coins,
                            "direction": "BULLISH",
                            "fromDate": from_str,
                            "toDate": to_str,
                            "size": 200,
                        },
                    )
                    items = _parse_mcp_result(result)
                    all_signals.extend(items)
                    print(f"  chunk {chunk_num}: {from_str[:10]} → {to_str[:10]}: {len(items)} signals")
        except Exception as e:
            print(f"  ! chunk {chunk_num} ({from_str[:10]}→{to_str[:10]}) failed: {e}", file=sys.stderr)

        cursor = chunk_end
        await asyncio.sleep(2.0)  # rate limit courtesy

    # Dedupe by (symbol, signalKey, timestamp)
    seen = set()
    deduped = []
    for sig in all_signals:
        key = (
            sig.get("symbol", ""),
            sig.get("signalKey") or sig.get("signal_key") or "",
            sig.get("timestamp") or sig.get("date") or "",
        )
        if key not in seen:
            seen.add(key)
            deduped.append(sig)

    return deduped


def _parse_mcp_result(result) -> list[dict]:
    """Extract list of items from an MCP tool result."""
    items = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            if isinstance(data.get("content"), list):
                items.extend(data["content"])
            elif "symbol" in data or "signalKey" in data:
                items.append(data)
    return items


def build_altfins_signal_set(signals: list[dict]) -> set[tuple[str, str]]:
    """Convert altFINS signals into a set of (SYMBOL, YYYY-MM-DD) for fast lookup.

    Maps altFINS timestamps to date-level granularity. A BULLISH signal on
    2026-02-15 means we consider entry valid on any hourly candle that day.
    """
    entries: set[tuple[str, str]] = set()
    for sig in signals:
        sym = (sig.get("symbol") or "").upper()
        # Try multiple timestamp formats altFINS might use
        ts_raw = sig.get("timestamp") or sig.get("date") or sig.get("createdAt") or ""
        if not sym or not ts_raw:
            continue
        # Parse ISO-ish timestamps
        try:
            if "T" in ts_raw:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(ts_raw[:10], "%Y-%m-%d")
            date_key = dt.strftime("%Y-%m-%d")
            entries.add((sym, date_key))
        except (ValueError, TypeError):
            continue
    return entries


# ── Scoring helper (same as historical_backtest.score_candle) ────

def score_candle(scorer, symbol, i, ind, fear_greed):
    ts = datetime.fromtimestamp(int(ind["open_time_ms"][i]) / 1000, tz=timezone.utc).replace(tzinfo=None)
    ema_aligned = ind["ema20"][i] > ind["ema50"][i]
    tech = TechnicalEvent(
        timestamp=ts, symbol=symbol,
        rsi_14=float(ind["rsi"][i]),
        macd_histogram=float(ind["macd_hist"][i]),
        bb_position=float(ind["bb_pos"][i]),
        ema_alignment=bool(ema_aligned),
        volume_ratio=float(ind["vol_ratio"][i]),
        atr_14_pct=float(ind["atr"][i] / ind["close"][i]) if ind["close"][i] > 0 else 0.0,
        ichimoku_signal="above_cloud" if ema_aligned else "in_cloud",
    )
    sent = SentimentEvent(timestamp=ts, symbol=symbol, fear_greed=int(fear_greed))
    tech_score = scorer.score_technical(tech)
    sent_score = scorer.score_sentiment(sent)
    composite, _ = scorer.composite_score(tech_score, sent_score, 50.0, 50.0)
    return composite, tech_score, sent_score


# ── Core simulation (parameterized entry logic) ─────────────────

def simulate_variant(
    symbol: str,
    ind: dict,
    fg_daily: dict[str, int],
    scorer: SignalScorer,
    variant_tag: float,
    *,
    our_threshold: float = OUR_THRESHOLD,
    altfins_dates: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Walk hourly candles for one symbol under one variant.

    Entry logic depends on variant_tag:
      TAG_OUR      → our score >= threshold
      TAG_ALTFINS  → altFINS has bullish signal for symbol+date
      TAG_COMBINED → both must agree
    """
    n = len(ind["close"])
    trades: list[dict] = []
    position: dict | None = None
    warmup = 50
    base = symbol.replace("-USD", "").upper()

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

        # ── Manage open position ──
        if position is not None:
            entry = position["entry"]
            hold_hours = i - position["entry_idx"]
            if high_px > position["peak"]:
                position["peak"] = high_px
            if low_px < position["trough"]:
                position["trough"] = low_px

            if low_px <= position["stop"]:
                trades.append(_close_trade(position, position["stop"], "hard_stop", hold_hours, symbol, regime, fg, variant_tag))
                position = None
                continue
            if high_px >= position["tp3"] and position["qty_frac"] > 0:
                trades.append(_close_trade(position, position["tp3"], "tp3", hold_hours, symbol, regime, fg, variant_tag))
                position = None
                continue
            if high_px >= position["tp2"] and not position["tp2_hit"]:
                position["tp2_hit"] = True
                position["qty_frac"] -= TP2_CLOSE_PCT
                trades.append(_close_partial(position, position["tp2"], TP2_CLOSE_PCT, "tp2", hold_hours, symbol, regime, fg, variant_tag))
            if high_px >= position["tp1"] and not position["tp1_hit"]:
                position["tp1_hit"] = True
                position["qty_frac"] -= TP1_CLOSE_PCT
                position["stop"] = entry
                trades.append(_close_partial(position, position["tp1"], TP1_CLOSE_PCT, "tp1", hold_hours, symbol, regime, fg, variant_tag))
            if hold_hours >= MAX_HOLD_HOURS and position["qty_frac"] > 0:
                trades.append(_close_trade(position, close_px, "time_72h", hold_hours, symbol, regime, fg, variant_tag))
                position = None
                continue
            if position and position["qty_frac"] <= 0.0001:
                position = None
                continue

        # ── Entry check ──
        if position is None:
            if atr <= 0:
                continue

            # Compute entry conditions
            composite, tech_sc, sent_sc = score_candle(scorer, symbol, i, ind, fg)
            our_signal = composite >= our_threshold
            altfins_signal = (base, date_key) in altfins_dates if altfins_dates else False

            enter = False
            if variant_tag == TAG_OUR:
                enter = our_signal
            elif variant_tag == TAG_ALTFINS:
                enter = altfins_signal
            elif variant_tag == TAG_COMBINED:
                enter = our_signal and altfins_signal

            if not enter:
                continue

            entry_px = close_px
            position = {
                "entry": entry_px,
                "entry_idx": i,
                "entry_ts": ts,
                "stop": entry_px - atr * ATR_STOP_MULT,
                "tp1": entry_px + atr * TP1_ATR_MULT,
                "tp2": entry_px + atr * TP2_ATR_MULT,
                "tp3": entry_px + atr * TP3_ATR_MULT,
                "tp1_hit": False,
                "tp2_hit": False,
                "qty_frac": 1.0,
                "peak": high_px,
                "trough": low_px,
                "signal_score": composite,
                "tech_score": tech_sc,
                "sent_score": sent_sc,
                "entry_regime": regime,
                "entry_fg": fg,
            }

    # Close any remaining position
    if position is not None:
        hold_hours = (n - 1) - position["entry_idx"]
        trades.append(_close_trade(position, float(ind["close"][-1]), "end_of_data", hold_hours, symbol, fg_to_regime(50), 50, variant_tag))

    return trades


# ── Enhanced summary with max drawdown ───────────────────────────

def comparison_summary(trades: list[dict], label: str) -> dict:
    """Extended summary for comparison table (adds max_dd)."""
    s = summarize(trades, label)
    if s.get("trades", 0) == 0:
        s["max_drawdown_pct"] = 0
        return s
    # Running PnL drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t["pnl_usd"]
        peak = max(peak, equity)
        dd = equity - peak
        if dd < max_dd:
            max_dd = dd
    s["max_drawdown_pct"] = (max_dd / NOTIONAL_USD) * 100 if NOTIONAL_USD else 0
    return s


def print_comparison(summaries: dict[str, dict]):
    """Print side-by-side comparison table."""
    labels = ["our-signals", "altfins-only", "combined"]
    print()
    print("=" * 90)
    print("  altFINS vs Our Signals — COMPARISON REPORT")
    print("=" * 90)
    print(f"  {'Variant':<16} {'Trades':>6}  {'Win%':>6}  {'Sharpe':>7}  {'R:R':>5}  {'PF':>6}  {'MaxDD%':>7}  {'PnL$':>10}")
    print("-" * 90)
    for label in labels:
        s = summaries.get(label, {})
        if s.get("trades", 0) == 0:
            print(f"  {label:<16} {'—':>6}  {'—':>6}  {'—':>7}  {'—':>5}  {'—':>6}  {'—':>7}  {'—':>10}")
            continue
        print(
            f"  {label:<16} "
            f"{s['trades']:>6}  "
            f"{s.get('win_rate', 0):>5.1f}%  "
            f"{s.get('sharpe', 0):>7.2f}  "
            f"{s.get('rr', 0):>5.2f}  "
            f"{s.get('profit_factor', 0):>6.2f}  "
            f"{s.get('max_drawdown_pct', 0):>6.2f}%  "
            f"${s.get('total_pnl_usd', 0):>+9.2f}"
        )
    print("=" * 90)

    # Decision helper
    viable = {k: v for k, v in summaries.items() if v.get("trades", 0) >= 10}
    if not viable:
        print("\n  Not enough trades to compare. Need more altFINS signal history.")
        return
    best = max(viable.items(), key=lambda kv: kv[1].get("sharpe", -99))
    print(f"\n  Best by Sharpe: {best[0]} (Sharpe={best[1]['sharpe']:.2f}, n={best[1]['trades']})")
    altfins = summaries.get("altfins-only", {})
    combined = summaries.get("combined", {})
    our = summaries.get("our-signals", {})
    if combined.get("sharpe", -99) > our.get("sharpe", -99) and combined.get("trades", 0) >= 10:
        improvement = combined["sharpe"] - our.get("sharpe", 0)
        print(f"  Combined Sharpe improves over our-only by {improvement:+.2f}")
        print(f"  → altFINS signals ADD value as a confirming filter.")
    elif altfins.get("trades", 0) < 10:
        print(f"  altFINS returned only {altfins.get('trades', 0)} trades — insufficient data for comparison.")
        print(f"  This may mean the free tier doesn't return 90d of historical signals.")
    else:
        print(f"  → altFINS signals do NOT improve our pipeline in this test period.")


# ── Main ─────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="altFINS vs Our Signals comparison backtest")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--symbols", type=str, default=",".join(s.replace("-USD", "") for s in DEFAULT_SYMBOLS))
    ap.add_argument("--db", type=str, default=str(DB_PATH))
    args = ap.parse_args()

    symbols = [f"{s.strip().upper()}-USD" for s in args.symbols.split(",") if s.strip()]
    base_coins = [s.replace("-USD", "") for s in symbols]
    db_path = Path(args.db)

    api_key = os.getenv("ALTFINS_API_KEY", "")
    if not api_key:
        # Try loading from .env
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ALTFINS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        print("ERROR: ALTFINS_API_KEY not set (check .env)", file=sys.stderr)
        sys.exit(1)

    t0 = time.time()
    print(f"SignalForge v2 — altFINS Comparison Backtest")
    print(f"  days={args.days}  symbols={len(symbols)}  db={db_path}")
    print()

    # ── Step 1: Fetch altFINS historical signals ──
    print("Step 1: Fetching altFINS BULLISH signals via MCP...")
    altfins_signals = asyncio.run(fetch_altfins_signals(api_key, base_coins, args.days))
    altfins_dates = build_altfins_signal_set(altfins_signals)
    unique_days = len({d for _, d in altfins_dates})
    print(f"  Total: {len(altfins_signals)} raw signals → {len(altfins_dates)} unique (symbol,date) pairs across {unique_days} days")
    # Show per-symbol breakdown
    by_sym: dict[str, int] = {}
    for sym, _ in altfins_dates:
        by_sym[sym] = by_sym.get(sym, 0) + 1
    for sym in sorted(by_sym, key=lambda s: -by_sym[s]):
        print(f"    {sym}: {by_sym[sym]} signal-days")
    print()

    # ── Step 2: Fetch OHLCV ──
    print(f"Step 2: Fetching 1h OHLCV ({args.days}d × {len(symbols)} symbols)...")
    per_symbol_ind: dict[str, dict] = {}
    for sym in symbols:
        rows, source = fetch_ohlcv(sym, args.days)
        if len(rows) < 60:
            print(f"  · {sym}: only {len(rows)} candles ({source}), skipping")
            continue
        per_symbol_ind[sym] = build_indicators(rows)
        print(f"  · {sym}: {len(rows)} candles ({source})")
    if not per_symbol_ind:
        print("\nNo OHLCV data fetched. Aborting.")
        return

    # ── Step 3: Fetch F&G ──
    print(f"\nStep 3: Fetching Fear & Greed history...")
    fg_daily = fetch_fear_greed_daily(days=max(args.days + 10, 120))
    print(f"  got {len(fg_daily)} days")

    # ── Step 4: Init DB + scorer ──
    conn = init_db(db_path)
    scorer = SignalScorer()

    # ── Step 5: Run 3 variants ──
    variant_configs = [
        ("our-signals", TAG_OUR),
        ("altfins-only", TAG_ALTFINS),
        ("combined", TAG_COMBINED),
    ]

    summaries: dict[str, dict] = {}
    print(f"\nStep 4: Running 3 variants across {len(per_symbol_ind)} symbols...")
    for label, tag in variant_configs:
        reset_threshold_rows(conn, tag)
        all_trades: list[dict] = []
        t_v = time.time()
        for sym, ind in per_symbol_ind.items():
            sym_trades = simulate_variant(
                sym, ind, fg_daily, scorer, tag,
                our_threshold=OUR_THRESHOLD,
                altfins_dates=altfins_dates,
            )
            all_trades.extend(sym_trades)
        write_trades(conn, all_trades)
        s = comparison_summary(all_trades, label)
        summaries[label] = s
        print(f"  · {label:<16} → {len(all_trades):>4} trades ({time.time() - t_v:.1f}s)")

    conn.close()

    # ── Step 6: Comparison report ──
    print_comparison(summaries)

    # Also print per-variant detail (reuse existing print_summary)
    print(f"\n{'─' * 90}")
    print(f"  DETAIL BY VARIANT")
    print(f"{'─' * 90}")
    for label, tag in variant_configs:
        s = summaries.get(label, {})
        if s.get("trades", 0) > 0:
            print_summary(s)
            print()

    print(f"  Total time: {time.time() - t0:.1f}s")
    print(f"  DB: {db_path}")
    print(f"  Reprint: PYTHONPATH=. python scripts/backtest_report.py --db {db_path}")


if __name__ == "__main__":
    main()
