"""Memory item construction and direct storage helpers."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from .metadata import build_metadata


def make_drawer_id(
    *,
    wing: str,
    room: str,
    source_file: str,
    chunk_index: int,
    content: str,
    session_id: str = "",
) -> str:
    digest_source = "|".join(
        [
            wing,
            room,
            source_file,
            str(chunk_index),
            str(session_id or ""),
            content,
        ]
    )
    digest = sha256(digest_source.encode("utf-8")).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{digest}"


def build_memory_item(
    *,
    runtime_ctx: dict[str, Any],
    wing: str,
    room: str,
    content: str,
    source_file: str,
    chunk_index: int,
    source: str,
    message_kind: str,
    agent_id: str,
    memory_type: str | None = None,
    importance: float | None = None,
) -> dict[str, Any]:
    metadata = build_metadata(
        runtime_ctx,
        room=room,
        source=source,
        message_kind=message_kind,
        memory_type=memory_type,
        importance=importance,
    )
    metadata["wing"] = wing
    metadata["source_file"] = source_file
    metadata["chunk_index"] = chunk_index

    item_id = make_drawer_id(
        wing=wing,
        room=room,
        source_file=source_file,
        chunk_index=chunk_index,
        content=content,
        session_id=str(runtime_ctx.get("session_id") or ""),
    )
    return {
        "id": item_id,
        "wing": wing,
        "room": room,
        "content": content,
        "source_file": source_file,
        "chunk_index": chunk_index,
        "agent": agent_id,
        "metadata": metadata,
    }


def upsert_memory_item(collection: Any, item: dict[str, Any], agent_id: str) -> str:
    metadata = dict(item.get("metadata") or {})
    metadata.setdefault("added_by", item.get("agent", agent_id))
    metadata.setdefault("wing", item["wing"])
    metadata.setdefault("room", item["room"])
    metadata.setdefault("source_file", item["source_file"])
    metadata.setdefault("chunk_index", item["chunk_index"])
    collection.upsert(
        documents=[item["content"]],
        ids=[item["id"]],
        metadatas=[metadata],
    )
    return item["id"]
