"""Signal Forge v2 — Chart Pattern Agent

Runs every 4 hours. Fetches 1d OHLCV from CoinGecko, detects:
  1. Inverse Head and Shoulders (bullish reversal)
  2. Head and Shoulders (bearish reversal)
  3. Double Bottom (bullish reversal)

Uses scipy.signal.argrelextrema for peak/trough detection.
Publishes PatternEvent at HIGH priority when confidence > 70%.
"""

import asyncio
import math
from datetime import datetime
from loguru import logger
import httpx
import numpy as np
from scipy.signal import argrelextrema

from agents.event_bus import EventBus, Priority
from agents.events import PatternEvent

COINGECKO_IDS = {
    "BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana",
    "XRP-USD": "ripple", "ADA-USD": "cardano", "AVAX-USD": "avalanche-2",
    "DOGE-USD": "dogecoin", "DOT-USD": "polkadot", "LINK-USD": "chainlink",
    "UNI-USD": "uniswap", "ATOM-USD": "cosmos", "LTC-USD": "litecoin",
    "NEAR-USD": "near", "APT-USD": "aptos", "ARB-USD": "arbitrum",
    "OP-USD": "optimism", "FIL-USD": "filecoin", "INJ-USD": "injective-protocol",
    "SUI-USD": "sui",
}

MIN_CONFIDENCE = 0.70  # Only publish patterns above 70% geometric confidence


class ChartPatternAgent:
    def __init__(self, event_bus: EventBus):
        self.bus = event_bus

    async def run_forever(self, interval_seconds: int = 14400):
        """Run every 4 hours (14400 seconds)."""
        logger.info("ChartPatternAgent: scanning for patterns every 4 hours")
        while True:
            try:
                await self._scan_all()
            except Exception as e:
                logger.error(f"ChartPatternAgent error: {e}")
            await asyncio.sleep(interval_seconds)

    async def _scan_all(self):
        """Fetch candles and detect patterns for watchlist."""
        logger.info(f"ChartPatternAgent: scanning {len(COINGECKO_IDS)} coins for patterns...")
        found = 0

        for symbol, cg_id in COINGECKO_IDS.items():
            try:
                candles = await self._fetch_candles(cg_id)
                if not candles or len(candles) < 30:
                    continue

                closes = np.array([c[4] for c in candles])  # close prices
                highs = np.array([c[2] for c in candles])
                lows = np.array([c[3] for c in candles])
                current = closes[-1]

                # Detect patterns
                for detect_fn in [self._detect_inv_head_shoulders, self._detect_head_shoulders, self._detect_double_bottom]:
                    result = detect_fn(closes, highs, lows)
                    if result and result["confidence"] >= MIN_CONFIDENCE:
                        event = PatternEvent(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            pattern_type=result["pattern"],
                            breakout_direction=result["direction"],
                            confidence=result["confidence"],
                            target_price=result["target"],
                            neckline_price=result["neckline"],
                            current_price=float(current),
                            candles_used=len(closes),
                        )
                        await self.bus.publish(event, priority=Priority.HIGH)
                        found += 1
                        logger.warning(
                            f"PATTERN: {symbol} {result['pattern']} ({result['direction']}) "
                            f"conf={result['confidence']:.0%} target=${result['target']:,.2f} "
                            f"neckline=${result['neckline']:,.2f}"
                        )

            except Exception as e:
                logger.debug(f"Pattern scan failed for {symbol}: {e}")

            await asyncio.sleep(2.5)  # CoinGecko rate limit

        logger.info(f"ChartPatternAgent: scan complete — {found} patterns detected")

    async def _fetch_candles(self, cg_id: str) -> list:
        """Fetch 90 days of daily OHLC from CoinGecko."""
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(
                    f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc",
                    params={"vs_currency": "usd", "days": 90},
                )
                if r.status_code == 200:
                    return r.json()  # [[timestamp, open, high, low, close], ...]
            except Exception:
                pass
        return []

    # ── Pattern Detection ──

    def _detect_inv_head_shoulders(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> dict | None:
        """Inverse Head and Shoulders — bullish reversal.

        Structure: trough - peak - deeper_trough - peak - trough
        The two shoulders should be at similar levels.
        The head is the deepest trough.
        Neckline connects the two peaks between the troughs.
        """
        # Find local minima (troughs) and maxima (peaks)
        order = max(3, len(closes) // 15)
        trough_idx = argrelextrema(lows, np.less_equal, order=order)[0]
        peak_idx = argrelextrema(highs, np.greater_equal, order=order)[0]

        if len(trough_idx) < 3 or len(peak_idx) < 2:
            return None

        # Look for pattern in the most recent troughs
        for i in range(len(trough_idx) - 2):
            t1, t2, t3 = trough_idx[i], trough_idx[i+1], trough_idx[i+2]
            left_shoulder = float(lows[t1])
            head = float(lows[t2])
            right_shoulder = float(lows[t3])

            # Head must be the deepest
            if head >= left_shoulder or head >= right_shoulder:
                continue

            # Shoulders should be within 5% of each other (symmetry)
            shoulder_diff = abs(left_shoulder - right_shoulder) / max(left_shoulder, 0.001)
            if shoulder_diff > 0.05:
                continue

            # Find neckline (peaks between troughs)
            peaks_between = [p for p in peak_idx if t1 < p < t3]
            if len(peaks_between) < 2:
                # Use the two highest points between shoulder troughs
                between_prices = highs[t1:t3+1]
                if len(between_prices) < 3:
                    continue
                sorted_idx = np.argsort(between_prices)[-2:]
                neckline = float(np.mean(between_prices[sorted_idx]))
            else:
                neckline = float(np.mean([highs[p] for p in peaks_between[:2]]))

            # Geometric confidence
            # Based on: symmetry, head depth, neckline clarity
            symmetry = 1.0 - shoulder_diff
            head_depth = (neckline - head) / neckline if neckline > 0 else 0
            depth_score = min(head_depth * 5, 1.0)  # Deeper head = stronger pattern
            recency = 1.0 if t3 > len(closes) - 10 else 0.7  # Recent pattern = more relevant

            confidence = symmetry * 0.4 + depth_score * 0.4 + recency * 0.2

            if confidence >= MIN_CONFIDENCE:
                # Target = neckline + (neckline - head)
                target = neckline + (neckline - head)
                return {
                    "pattern": "inverse_head_shoulders",
                    "direction": "bullish",
                    "confidence": round(confidence, 2),
                    "neckline": round(neckline, 2),
                    "target": round(target, 2),
                }

        return None

    def _detect_head_shoulders(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> dict | None:
        """Head and Shoulders — bearish reversal.

        Structure: peak - trough - higher_peak - trough - peak
        The head is the highest peak.
        Neckline connects the two troughs.
        """
        order = max(3, len(closes) // 15)
        peak_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
        trough_idx = argrelextrema(lows, np.less_equal, order=order)[0]

        if len(peak_idx) < 3 or len(trough_idx) < 2:
            return None

        for i in range(len(peak_idx) - 2):
            p1, p2, p3 = peak_idx[i], peak_idx[i+1], peak_idx[i+2]
            left_shoulder = float(highs[p1])
            head = float(highs[p2])
            right_shoulder = float(highs[p3])

            # Head must be the highest
            if head <= left_shoulder or head <= right_shoulder:
                continue

            # Shoulders within 5% (symmetry)
            shoulder_diff = abs(left_shoulder - right_shoulder) / max(left_shoulder, 0.001)
            if shoulder_diff > 0.05:
                continue

            # Neckline from troughs between peaks
            troughs_between = [t for t in trough_idx if p1 < t < p3]
            if len(troughs_between) < 2:
                between_prices = lows[p1:p3+1]
                if len(between_prices) < 3:
                    continue
                sorted_idx = np.argsort(between_prices)[:2]
                neckline = float(np.mean(between_prices[sorted_idx]))
            else:
                neckline = float(np.mean([lows[t] for t in troughs_between[:2]]))

            symmetry = 1.0 - shoulder_diff
            head_height = (head - neckline) / head if head > 0 else 0
            height_score = min(head_height * 5, 1.0)
            recency = 1.0 if p3 > len(closes) - 10 else 0.7

            confidence = symmetry * 0.4 + height_score * 0.4 + recency * 0.2

            if confidence >= MIN_CONFIDENCE:
                target = neckline - (head - neckline)
                return {
                    "pattern": "head_shoulders",
                    "direction": "bearish",
                    "confidence": round(confidence, 2),
                    "neckline": round(neckline, 2),
                    "target": round(target, 2),
                }

        return None

    def _detect_double_bottom(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> dict | None:
        """Double Bottom — bullish reversal.

        Structure: trough - peak - trough (at similar level)
        The two bottoms should be within 3% of each other.
        Neckline = the peak between the two bottoms.
        """
        order = max(3, len(closes) // 15)
        trough_idx = argrelextrema(lows, np.less_equal, order=order)[0]
        peak_idx = argrelextrema(highs, np.greater_equal, order=order)[0]

        if len(trough_idx) < 2:
            return None

        for i in range(len(trough_idx) - 1):
            t1, t2 = trough_idx[i], trough_idx[i+1]
            bottom1 = float(lows[t1])
            bottom2 = float(lows[t2])

            # Bottoms within 3%
            bottom_diff = abs(bottom1 - bottom2) / max(bottom1, 0.001)
            if bottom_diff > 0.03:
                continue

            # Need at least 5 candles between bottoms
            if t2 - t1 < 5:
                continue

            # Neckline = highest point between the two bottoms
            between = highs[t1:t2+1]
            neckline = float(np.max(between))

            # The peak must be meaningful (at least 3% above bottoms)
            avg_bottom = (bottom1 + bottom2) / 2
            if (neckline - avg_bottom) / avg_bottom < 0.03:
                continue

            symmetry = 1.0 - bottom_diff
            depth = (neckline - avg_bottom) / neckline if neckline > 0 else 0
            depth_score = min(depth * 5, 1.0)
            recency = 1.0 if t2 > len(closes) - 10 else 0.7

            confidence = symmetry * 0.4 + depth_score * 0.4 + recency * 0.2

            if confidence >= MIN_CONFIDENCE:
                target = neckline + (neckline - avg_bottom)
                return {
                    "pattern": "double_bottom",
                    "direction": "bullish",
                    "confidence": round(confidence, 2),
                    "neckline": round(neckline, 2),
                    "target": round(target, 2),
                }

        return None
