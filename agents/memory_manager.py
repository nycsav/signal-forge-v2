"""Signal Forge v2 — Layered Memory Manager (from FinMem)

Three memory tiers with exponential decay:
- Working memory (last 2 hours): price action, recent signals, whale events
- Medium-term memory (last 48 hours): regime shifts, trade outcomes, patterns
- Long-term memory (last 7 days): macro context, weekly performance, key lessons

Memories decay over time. High-importance memories (profitable trades, whale
accumulation) decay slower. Memories cited in profitable trades get boosted.

Adapted from: github.com/pipiku915/FinMem-LLM-StockTrading
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger


@dataclass
class Memory:
    text: str
    timestamp: float          # unix timestamp
    importance: float = 50.0  # 0-100, decays over time
    recency: float = 1.0      # 0-1, exponential decay
    access_count: int = 0     # times this memory was useful
    category: str = ""        # "price", "whale", "regime", "trade", "pattern"
    symbol: str = ""

    @property
    def compound_score(self) -> float:
        return self.importance * 0.6 + self.recency * 100 * 0.4


class MemoryTier:
    """Single memory tier with decay and cleanup."""

    def __init__(self, name: str, recency_factor: float, importance_decay: float,
                 cleanup_threshold: float = 10.0, max_size: int = 200):
        self.name = name
        self.recency_factor = recency_factor    # higher = slower decay
        self.importance_decay = importance_decay  # multiplied per step
        self.cleanup_threshold = cleanup_threshold
        self.max_size = max_size
        self.memories: list[Memory] = []

    def add(self, text: str, importance: float = 50.0, category: str = "",
            symbol: str = ""):
        mem = Memory(
            text=text,
            timestamp=time.time(),
            importance=importance,
            recency=1.0,
            category=category,
            symbol=symbol,
        )
        self.memories.append(mem)
        # Enforce max size
        if len(self.memories) > self.max_size:
            self.memories.sort(key=lambda m: m.compound_score)
            self.memories = self.memories[len(self.memories) - self.max_size:]

    def decay_step(self):
        """Apply one decay step to all memories."""
        now = time.time()
        for mem in self.memories:
            age_hours = (now - mem.timestamp) / 3600
            mem.recency = math.exp(-age_hours / self.recency_factor)
            mem.importance *= self.importance_decay

        # Cleanup: remove memories below threshold
        before = len(self.memories)
        self.memories = [m for m in self.memories if m.compound_score > self.cleanup_threshold]
        removed = before - len(self.memories)
        if removed > 0:
            logger.debug(f"Memory {self.name}: cleaned {removed} decayed memories")

    def query(self, symbol: str = "", category: str = "", top_k: int = 5) -> list[Memory]:
        """Retrieve top-k memories, optionally filtered."""
        candidates = self.memories
        if symbol:
            candidates = [m for m in candidates if m.symbol == symbol or m.symbol == ""]
        if category:
            candidates = [m for m in candidates if m.category == category]

        candidates.sort(key=lambda m: m.compound_score, reverse=True)
        return candidates[:top_k]

    def boost(self, text_substring: str, amount: float = 5.0):
        """Boost importance of memories matching a substring (feedback loop)."""
        for mem in self.memories:
            if text_substring.lower() in mem.text.lower():
                mem.importance = min(100, mem.importance + amount)
                mem.access_count += 1


class LayeredMemory:
    """Three-tier memory system for trading context.

    Usage in AI prompts:
        memory = LayeredMemory()
        context = memory.build_context("BTC-USD")
        # Inject `context` into LLM prompt for richer analysis
    """

    def __init__(self):
        self.working = MemoryTier(
            name="working",
            recency_factor=2.0,      # 2-hour half-life
            importance_decay=0.98,
            cleanup_threshold=5.0,
            max_size=50,
        )
        self.medium = MemoryTier(
            name="medium",
            recency_factor=48.0,     # 48-hour half-life
            importance_decay=0.995,
            cleanup_threshold=8.0,
            max_size=100,
        )
        self.long_term = MemoryTier(
            name="long_term",
            recency_factor=168.0,    # 7-day half-life
            importance_decay=0.998,
            cleanup_threshold=10.0,
            max_size=100,
        )

    def add_price_action(self, symbol: str, text: str, importance: float = 50):
        self.working.add(text, importance, "price", symbol)

    def add_whale_event(self, text: str, importance: float = 70):
        self.working.add(text, importance, "whale")
        self.medium.add(text, importance * 0.8, "whale")

    def add_regime_shift(self, text: str, importance: float = 80):
        self.medium.add(text, importance, "regime")
        self.long_term.add(text, importance * 0.7, "regime")

    def add_trade_outcome(self, symbol: str, text: str, profitable: bool):
        importance = 75 if profitable else 60
        self.medium.add(text, importance, "trade", symbol)
        self.long_term.add(text, importance * 0.8, "trade", symbol)

    def add_pattern(self, symbol: str, text: str, importance: float = 60):
        self.medium.add(text, importance, "pattern", symbol)

    def add_lesson(self, text: str, importance: float = 85):
        self.long_term.add(text, importance, "lesson")

    def decay_all(self):
        """Run decay step on all tiers. Call every scan cycle."""
        self.working.decay_step()
        self.medium.decay_step()
        self.long_term.decay_step()

    def boost_on_profit(self, symbol: str):
        """Boost memories related to a profitable symbol."""
        self.working.boost(symbol, 3.0)
        self.medium.boost(symbol, 5.0)
        self.long_term.boost(symbol, 2.0)

    def build_context(self, symbol: str = "", top_k: int = 3) -> str:
        """Build formatted context string for LLM prompt injection.

        Returns a structured string with recent, medium-term, and long-term
        memories relevant to the symbol.
        """
        working_mems = self.working.query(symbol=symbol, top_k=top_k)
        medium_mems = self.medium.query(symbol=symbol, top_k=top_k)
        long_mems = self.long_term.query(symbol=symbol, top_k=top_k)

        parts = []

        if working_mems:
            parts.append("RECENT (last 2h):")
            for m in working_mems:
                parts.append(f"  - {m.text}")

        if medium_mems:
            parts.append("MEDIUM-TERM (last 48h):")
            for m in medium_mems:
                parts.append(f"  - {m.text}")

        if long_mems:
            parts.append("LONG-TERM (this week):")
            for m in long_mems:
                parts.append(f"  - {m.text}")

        if not parts:
            return ""

        return "\n".join(parts)

    def stats(self) -> dict:
        return {
            "working": len(self.working.memories),
            "medium": len(self.medium.memories),
            "long_term": len(self.long_term.memories),
        }
