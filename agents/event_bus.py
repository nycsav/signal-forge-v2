"""Signal Forge v2 — Priority Event Bus

Events processed by priority: CRITICAL → HIGH → NORMAL → LOW.
Handlers dispatched concurrently per event.
Whale signals and circuit breakers always process first.
"""

import asyncio
from enum import IntEnum
from typing import Type, Callable
from collections import defaultdict
from loguru import logger


class Priority(IntEnum):
    CRITICAL = 0   # Whale signals, circuit breakers
    HIGH = 1       # Technical + market data events
    NORMAL = 2     # Sentiment, on-chain, trade proposals
    LOW = 3        # Logging, learning agent


class EventBus:
    def __init__(self):
        self._queues = {p: asyncio.Queue() for p in Priority}
        self._subscribers: dict[Type, list[tuple[Callable, Priority]]] = defaultdict(list)
        self._running: bool = False

    def subscribe(self, event_type: Type, handler: Callable,
                  priority: Priority = Priority.NORMAL):
        self._subscribers[event_type].append((handler, priority))
        logger.debug(f"Subscribed {handler.__qualname__} to {event_type.__name__} (priority={priority.name})")

    async def publish(self, event, priority: Priority = Priority.NORMAL):
        await self._queues[priority].put(event)

    async def run(self):
        self._running = True
        logger.info("EventBus started (priority queues: CRITICAL → HIGH → NORMAL → LOW)")
        while self._running:
            # Always drain higher priority queues first
            event = None
            for p in Priority:
                if not self._queues[p].empty():
                    event = await self._queues[p].get()
                    break

            if event is None:
                await asyncio.sleep(0.01)  # Yield — no busy-wait
                continue

            event_type = type(event)
            handlers = self._subscribers.get(event_type, [])

            if not handlers:
                continue

            # Dispatch handlers CONCURRENTLY — don't let one handler block others
            results = await asyncio.gather(
                *[handler(event) for handler, _ in handlers],
                return_exceptions=True,
            )

            # Log any handler errors
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    handler_name = handlers[i][0].__qualname__ if i < len(handlers) else "unknown"
                    logger.error(f"EventBus handler error ({handler_name}): {result}")

    def stop(self):
        self._running = False
        logger.info("EventBus stopped")
