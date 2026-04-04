"""Signal Forge v2 — Central Event Bus

Pub/sub event bus for all agent communication.
Agents publish typed Pydantic events; subscribers receive them by type.
"""

import asyncio
from typing import Callable, Type
from collections import defaultdict
from loguru import logger


class EventBus:
    def __init__(self):
        self._subscribers: dict[Type, list[Callable]] = defaultdict(list)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running: bool = False

    def subscribe(self, event_type: Type, handler: Callable):
        self._subscribers[event_type].append(handler)
        logger.debug(f"Subscribed {handler.__qualname__} to {event_type.__name__}")

    async def publish(self, event):
        await self._queue.put(event)

    async def run(self):
        self._running = True
        logger.info("EventBus started")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            event_type = type(event)
            handlers = self._subscribers.get(event_type, [])
            for handler in handlers:
                try:
                    await handler(event)
                except Exception as e:
                    logger.error(f"EventBus handler error ({handler.__qualname__}): {e}")
            self._queue.task_done()

    def stop(self):
        self._running = False
        logger.info("EventBus stopped")
