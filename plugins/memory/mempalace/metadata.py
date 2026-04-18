"""Metadata helpers for the MemPalace plugin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_metadata(
    runtime_ctx: dict[str, Any],
    *,
    room: str,
    source: str,
    message_kind: str,
    memory_type: str | None = None,
    importance: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "created_at": _utc_now_iso(),
        "room": room,
        "source": source,
        "message_kind": message_kind,
        "session_id": str(runtime_ctx.get("session_id") or ""),
        "platform": str(runtime_ctx.get("platform") or ""),
        "user_id": str(runtime_ctx.get("user_id") or ""),
        "agent_id": str(runtime_ctx.get("agent_id") or ""),
    }
    if memory_type is not None:
        metadata["memory_type"] = memory_type
    if importance is not None:
        metadata["importance"] = float(importance)
    return metadata
