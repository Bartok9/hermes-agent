"""Event/source constants for the MemPalace Hermes plugin."""

from __future__ import annotations

SOURCE_TOOL = "tool"
SOURCE_SYNC_TURN = "sync_turn"
SOURCE_MEMORY = "memory"
SOURCE_COMPRESSION = "compression"
SOURCE_SESSION_END = "session_end"

MESSAGE_KIND_EXPLICIT_MEMORY = "explicit_memory"
MESSAGE_KIND_USER_MESSAGE = "user_message"
MESSAGE_KIND_ASSISTANT_MESSAGE = "assistant_message"
MESSAGE_KIND_BUILTIN_MEMORY_WRITE = "builtin_memory_write"
MESSAGE_KIND_COMPRESSED_CONTEXT = "compressed_context"
MESSAGE_KIND_SESSION_SUMMARY = "session_summary"
