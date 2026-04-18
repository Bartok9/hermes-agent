"""Hook and background-search mixins for the MemPalace Hermes plugin."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .events import (
    MESSAGE_KIND_BUILTIN_MEMORY_WRITE,
    MESSAGE_KIND_COMPRESSED_CONTEXT,
    MESSAGE_KIND_SESSION_SUMMARY,
    SOURCE_COMPRESSION,
    SOURCE_MEMORY,
    SOURCE_SESSION_END,
)

logger = logging.getLogger(__name__)


class MemPalaceHooksMixin:
    """Lifecycle hooks and prefetch helpers mixed into the provider."""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire background search for the next turn."""
        if not self._collection or not query:
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)
        self._prefetch_thread = self._thread_factory(
            target=self._bg_search,
            args=(query, session_id),
            name="mempalace-prefetch",
            daemon=True,
        )
        self._prefetch_thread.start()

    def _bg_search(self, query: str, session_id: str = "") -> None:
        """Background search — runs without blocking the turn."""
        try:
            room = self._resolve_room(session_id=session_id) if session_id else None
            result = self._raw_search(query, n_results=self._n_results, room=room)
            formatted = self._format_search_result(result)
            with self._lock:
                self._prefetch_result = formatted
        except Exception as exc:
            logger.debug("MemPalace prefetch failed: %s", exc)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached prefetch results. Fast — reads from cache."""
        with self._lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        return result

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message."""
        if not message or not self._collection:
            return
        self.queue_prefetch(message, session_id=kwargs.get("session_id", ""))

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes into MemPalace."""
        if not content or not self._collection:
            return

        try:
            room = self._resolve_room(target)
            if action in ("add", "replace"):
                self._store_memory(
                    room=room,
                    content=content,
                    source_file=f"builtin_{target}_{action}",
                    chunk_index=int(datetime.now(timezone.utc).timestamp() * 1000) % 1000000,
                    source=SOURCE_MEMORY,
                    message_kind=MESSAGE_KIND_BUILTIN_MEMORY_WRITE,
                )
                logger.debug(
                    "MemPalace mirrored built-in %s write: %s",
                    action,
                    content[:80],
                )
        except Exception as exc:
            logger.warning("MemPalace on_memory_write failed: %s", exc)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Persist key facts before old messages are compressed away."""
        if not messages or not self._collection:
            return ""

        self._recent_messages = messages

        try:
            extracted = self._extract_key_content(messages)
            if not extracted:
                return ""

            room = self._resolve_room(f"session_{self._session_id}")
            summary_parts = []
            for item in extracted:
                self._store_memory(
                    room=room,
                    content=item["content"],
                    source_file="compression",
                    chunk_index=item.get("index", 0),
                    source=SOURCE_COMPRESSION,
                    message_kind=MESSAGE_KIND_COMPRESSED_CONTEXT,
                )
                summary_parts.append(f"- {item['content'][:150]}")

            summary = "\n".join(summary_parts[:10])
            logger.debug(
                "MemPalace on_pre_compress stored %d key facts before compression",
                len(extracted),
            )
            return (
                "\n[MemPalace: key facts preserved from compressed context]\n"
                f"{summary}\n"
                "[End MemPalace preserved facts]\n"
            )
        except Exception as exc:
            logger.warning("MemPalace on_pre_compress failed: %s", exc)
            return ""

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Store session summary facts at the end of a conversation."""
        if not messages or not self._collection:
            return

        try:
            extracted = self._extract_key_content(messages)
            if not extracted:
                return

            room = self._resolve_room("session_summaries")
            for item in extracted:
                self._store_memory(
                    room=room,
                    content=item["content"],
                    source_file=f"session_end_{self._session_id}",
                    chunk_index=item.get("index", 0),
                    source=SOURCE_SESSION_END,
                    message_kind=MESSAGE_KIND_SESSION_SUMMARY,
                )

            logger.info(
                "MemPalace on_session_end: stored %d session summary items for %s",
                len(extracted),
                self._session_id,
            )
        except Exception as exc:
            logger.warning("MemPalace on_session_end failed: %s", exc)

    def _extract_key_content(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract meaningful content from message list for compression/session end."""
        extracted = []
        skip_patterns = (
            "system prompt",
            "mempalace memory",
            "you are a helpful",
            "memory palace is active",
            "[memories from previous",
        )

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")

            if not content or len(content) < 15:
                continue
            if role == "system":
                continue

            content_lower = content.lower()
            if any(p in content_lower for p in skip_patterns):
                continue

            if role in ("user", "assistant", "model"):
                extracted.append({"content": content.strip(), "index": i})

        return extracted[-20:]
