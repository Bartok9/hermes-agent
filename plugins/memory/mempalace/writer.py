"""Async writer queue for MemPalace persistence."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .store import upsert_memory_item

logger = logging.getLogger(__name__)


_QUEUE_MAXSIZE = 512     # bounded to prevent OOM if ChromaDB is wedged
_MAX_FLUSH_RETRIES = 3  # stop retrying after this many failures per batch


class WriteQueue:
    """Thread-safe async write queue for non-blocking memory persistence."""

    def __init__(
        self,
        collection: Any,
        agent_id: str,
        thread_factory=threading.Thread,
        maxsize: int = _QUEUE_MAXSIZE,
        max_retries: int = _MAX_FLUSH_RETRIES,
    ):
        self._collection = collection
        self._agent_id = agent_id
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._max_retries = max_retries
        self._thread = thread_factory(
            target=self._loop, name="mempalace-writer", daemon=True
        )
        self._running = True
        self._thread.start()

    def enqueue(self, items: list[dict[str, Any]]) -> None:
        try:
            self._q.put_nowait(items)
        except queue.Full:
            logger.warning(
                "MemPalace write queue full (%d slots); dropping %d items",
                self._q.maxsize,
                len(items),
            )

    def _flush(self, items: list[dict[str, Any]], attempt: int = 0) -> None:
        try:
            for item in items:
                upsert_memory_item(self._collection, item, self._agent_id)
            logger.debug("MemPalace flushed %d items to ChromaDB", len(items))
        except Exception as exc:
            logger.warning("MemPalace flush failed (attempt %d): %s", attempt + 1, exc)
            if self._running and attempt < self._max_retries - 1:
                time.sleep(min(2 ** attempt, 8))
                self._flush(items, attempt + 1)
            else:
                logger.error(
                    "MemPalace dropping %d items after %d flush failures",
                    len(items), attempt + 1,
                )

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
