"""Async writer queue for MemPalace persistence."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .store import upsert_memory_item

logger = logging.getLogger(__name__)


class WriteQueue:
    """Thread-safe async write queue for non-blocking memory persistence."""

    def __init__(self, collection: Any, agent_id: str, thread_factory=threading.Thread):
        self._collection = collection
        self._agent_id = agent_id
        self._q: queue.Queue = queue.Queue()
        self._thread = thread_factory(
            target=self._loop, name="mempalace-writer", daemon=True
        )
        self._running = True
        self._thread.start()

    def enqueue(self, items: list[dict[str, Any]]) -> None:
        self._q.put(items)

    def _flush(self, items: list[dict[str, Any]]) -> None:
        try:
            for item in items:
                upsert_memory_item(self._collection, item, self._agent_id)
            logger.debug("MemPalace flushed %d items to ChromaDB", len(items))
        except Exception as exc:
            logger.warning("MemPalace flush failed: %s", exc)
            if self._running:
                time.sleep(1)
                self._q.put(items)

    def _loop(self) -> None:
        while self._running:
            try:
                item = self._q.get(timeout=2)
                if item is None:
                    break
                self._flush(item)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("MemPalace writer error: %s", exc)

    def shutdown(self) -> None:
        self._running = False
        self._q.put(None)
        self._thread.join(timeout=10)
