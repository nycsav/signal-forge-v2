#!/usr/bin/env python3
"""Signal Forge v2 — Backtest Engine

Walk-forward backtest of the ATR trailing stop exit strategy on historical data.
Uses CoinGecko OHLC data (free, no auth).

Usage:
    python scripts/backtest.py --symbol BTC-USD --days 365
    python scripts/backtest.py --symbol ETH-USD --days 180 --atr-mult 2.5
"""

import argparse
import math
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

COINGECKO_IDS = {
    "BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana",
    "XRP-USD": "ripple", "ADA-USD": "cardano", "AVAX-USD": "avalanche-2",
    "DOGE-USD": "dogecoin", "DOT-USD": "polkadot", "LINK-USD": "chainlink",
    "UNI-USD": "uniswap", "ATOM-USD": "cosmos", "LTC-USD": "litecoin",
    "NEAR-USD": "near", "APT-USD": "aptos", "ARB-USD": "arbitrum",
    "OP-USD": "optimism", "FIL-USD": "filecoin", "INJ-USD": "injective-protocol",
    "SUI-USD": "sui",
}


def fetch_ohlc(symbol: str, days: int) -> list[dict]:
    """Fetch OHLC from CoinGecko. Returns list of {timestamp, open, high, low, close}."""
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        print(f"Unknown symbol: {symbol}")
        return []

    print(f"Fetching {days} days of OHLC for {symbol} ({coin_id})...")
    r = httpx.get(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
        params={"vs_currency": "usd", "days": days},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"CoinGecko error: {r.status_code}")
        return []

    raw = r.json()
    candles = []
    for c in raw:
        if len(c) >= 5:
            candles.append({
                "timestamp": c[0],
                "open": c[1],
                "high": c[2],
                "low": c[3],
                "close": c[4],
            })
    print(f"Got {len(candles)} candles")
    return candles


def compute_atr(candles: list[dict], period: int = 14) -> list[float]:
    """Compute ATR from candles."""
    atrs = [0.0] * len(candles)
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if len(trs) >= period:
            atrs[i] = sum(trs[-period:]) / period
    return atrs


def run_backtest(
    candles: list[dict],
    atr_trail_mult: float = 2.5,
    atr_activation_mult: float = 1.5,
    tp1_mult: float = 3.75,
    tp2_mult: float = 7.5,
    tp3_mult: float = 12.5,
    initial_capital: float = 100000,
    position_pct: float = 0.02,
    signal_threshold: float = 0.6,  # Simple: buy when RSI-proxy < threshold
) -> dict:
    """Walk-forward backtest with ATR trailing stop."""
    atrs = compute_atr(candles)
    capital = initial_capital
    trades = []
    position = None
    equity_curve = [capital]

    # Simple RSI proxy: oversold if close < 20-period SMA * 0.97
    sma_period = 20

    for i in range(max(sma_period, 15), len(candles)):
        price = candles[i]["close"]
        high = candles[i]["high"]
        low = candles[i]["low"]
        atr = atrs[i]

        if atr <= 0:
            equity_curve.append(capital)
            continue

        # ── If in a position, evaluate exits ──
        if position:
            # Update HWM (highest close)
            if price > position["hwm"]:
                position["hwm"] = price

            # Hard stop
            if price <= position["stop"]:
                pnl = (price - position["entry"]) * position["qty"]
                capital += pnl + position["size"]
                trades.append({
                    "entry": position["entry"], "exit": price,
                    "pnl_pct": (price - position["entry"]) / position["entry"],
                    "pnl_usd": pnl, "reason": "stop",
                    "hold_bars": i - position["entry_bar"],
                })
                position = None
                equity_curve.append(capital)
                continue

            # ATR trailing activation
            activation = position["entry"] + atr * atr_activation_mult
            if price >= activation and not position["trailing"]:
                position["trailing"] = True

            if position["trailing"]:
                new_stop = position["hwm"] - atr * atr_trail_mult
                if new_stop > position["stop"]:
                    position["stop"] = new_stop

                if price <= position["stop"]:
                    pnl = (price - position["entry"]) * position["qty"]
                    capital += pnl + position["size"]
                    trades.append({
                        "entry": position["entry"], "exit": price,
                        "pnl_pct": (price - position["entry"]) / position["entry"],
                        "pnl_usd": pnl, "reason": "trailing",
                        "hold_bars": i - position["entry_bar"],
                    })
                    position = None
                    equity_curve.append(capital)
                    continue

            # Take profit 1 (33%)
            tp1 = position["entry"] + atr * tp1_mult
            if price >= tp1 and not position.get("tp1_hit"):
                sell_qty = position["qty"] * 0.33
                pnl = (price - position["entry"]) * sell_qty
                capital += pnl + position["size"] * 0.33
                position["qty"] -= sell_qty
                position["size"] *= 0.67
                position["tp1_hit"] = True
                position["stop"] = position["entry"]  # Move to breakeven

            # Take profit 2 (33%)
            tp2 = position["entry"] + atr * tp2_mult
            if price >= tp2 and not position.get("tp2_hit") and position.get("tp1_hit"):
                sell_qty = position["qty"] * 0.5  # 50% of remaining
                pnl = (price - position["entry"]) * sell_qty
                capital += pnl + position["size"] * 0.5
                position["qty"] -= sell_qty
                position["size"] *= 0.5
                position["tp2_hit"] = True

            # Take profit 3 (close all)
            tp3 = position["entry"] + atr * tp3_mult
            if price >= tp3:
                pnl = (price - position["entry"]) * position["qty"]
                capital += pnl + position["size"]
                trades.append({
                    "entry": position["entry"], "exit": price,
                    "pnl_pct": (price - position["entry"]) / position["entry"],
                    "pnl_usd": pnl, "reason": "tp3",
                    "hold_bars": i - position["entry_bar"],
                })
                position = None

            # Time exit (72 bars ≈ 72 candles)
            if position and (i - position["entry_bar"]) >= 72:
                pnl = (price - position["entry"]) * position["qty"]
                capital += pnl + position["size"]
                trades.append({
                    "entry": position["entry"], "exit": price,
                    "pnl_pct": (price - position["entry"]) / position["entry"],
                    "pnl_usd": pnl, "reason": "time",
                    "hold_bars": i - position["entry_bar"],
                })
                position = None

        # ── If no position, check entry signal ──
        elif not position:
            # Simple mean reversion: buy when price is below SMA by threshold
            sma = sum(c["close"] for c in candles[i-sma_period:i]) / sma_period
            if price < sma * (1 - 0.03) and atr > 0:
                size = capital * position_pct
                qty = size / price
                stop = price - atr * atr_trail_mult
                position = {
                    "entry": price, "stop": stop, "qty": qty, "size": size,
                    "hwm": price, "trailing": False, "entry_bar": i,
                    "tp1_hit": False, "tp2_hit": False,
                }
                capital -= size

        equity_curve.append(capital + (position["qty"] * price if position else 0))

    # Close any remaining position
    if position:
        price = candles[-1]["close"]
        pnl = (price - position["entry"]) * position["qty"]
        capital += pnl + position["size"]
        trades.append({
            "entry": position["entry"], "exit": price,
            "pnl_pct": (price - position["entry"]) / position["entry"],
            "pnl_usd": pnl, "reason": "end_of_data",
            "hold_bars": len(candles) - position["entry_bar"],
        })

    # ── Compute metrics ──
    if not trades:
        return {"error": "No trades generated", "candles": len(candles)}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]

    # Sharpe
    mean_pnl = sum(pnls) / len(pnls)
    std_pnl = math.sqrt(sum((p - mean_pnl)**2 for p in pnls) / len(pnls)) if len(pnls) > 1 else 1
    sharpe = (mean_pnl / (std_pnl + 1e-8)) * math.sqrt(365 / max(1, len(candles) / 24))

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd

    # Sortino
    downside = [p for p in pnls if p < 0]
    downside_std = math.sqrt(sum(p**2 for p in downside) / len(downside)) if downside else 1
    sortino = (mean_pnl / (downside_std + 1e-8)) * math.sqrt(365 / max(1, len(candles) / 24))

    # Profit factor
    gross_wins = sum(t["pnl_usd"] for t in wins)
    gross_losses = abs(sum(t["pnl_usd"] for t in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Exit breakdown
    exit_reasons = {}
    for t in trades:
        r = t["reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "symbol": candles[0]["close"] if candles else 0,
        "candles": len(candles),
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_return_pct": (equity_curve[-1] - initial_capital) / initial_capital * 100,
        "total_return_usd": equity_curve[-1] - initial_capital,
        "final_equity": equity_curve[-1],
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) * 100 if wins else 0,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) * 100 if losses else 0,
        "avg_hold_bars": sum(t["hold_bars"] for t in trades) / len(trades),
        "exit_breakdown": exit_reasons,
        "atr_trail_mult": atr_trail_mult,
        "atr_activation_mult": atr_activation_mult,
    }


def main():
    parser = argparse.ArgumentParser(description="Signal Forge v2 Backtest")
    parser.add_argument("--symbol", default="BTC-USD", help="Trading pair (default: BTC-USD)")
    parser.add_argument("--days", type=int, default=365, help="Lookback period in days")
    parser.add_argument("--atr-mult", type=float, default=2.5, help="ATR trailing stop multiplier")
    parser.add_argument("--capital", type=float, default=100000, help="Initial capital")
    parser.add_argument("--position-pct", type=float, default=0.02, help="Position size as pct of capital")
    args = parser.parse_args()

    candles = fetch_ohlc(args.symbol, args.days)
    if not candles:
        print("No data fetched")
        return

    results = run_backtest(
        candles,
        atr_trail_mult=args.atr_mult,
        initial_capital=args.capital,
        position_pct=args.position_pct,
    )

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — {args.symbol} ({args.days} days)")
    print(f"{'='*60}")
    print(f"  Candles:        {results['candles']}")
    print(f"  Total Trades:   {results['total_trades']}")
    print(f"  Win Rate:       {results['win_rate']:.1f}%")
    print(f"  Total Return:   {results['total_return_pct']:+.2f}% (${results['total_return_usd']:+,.2f})")
    print(f"  Final Equity:   ${results['final_equity']:,.2f}")
    print(f"  Sharpe Ratio:   {results['sharpe_ratio']}")
    print(f"  Sortino Ratio:  {results['sortino_ratio']}")
    print(f"  Max Drawdown:   {results['max_drawdown_pct']}%")
    print(f"  Profit Factor:  {results['profit_factor']}")
    print(f"  Avg Win:        {results['avg_win_pct']:+.2f}%")
    print(f"  Avg Loss:       {results['avg_loss_pct']:+.2f}%")
    print(f"  Avg Hold:       {results['avg_hold_bars']:.0f} bars")
    print(f"  ATR Multiplier: {results['atr_trail_mult']}x")
    print(f"  Exit Breakdown: {results['exit_breakdown']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
