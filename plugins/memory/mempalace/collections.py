"""Collection and room naming strategies for the MemPalace plugin."""

from __future__ import annotations

import re
from typing import Any

from .config import MemPalaceConfig

_MAX_IDENTIFIER_LENGTH = 63


def slugify_identifier(value: object, default: str = "default") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    text = re.sub(r"-+", "-", text)
    if not text:
        text = default
    return text[:_MAX_IDENTIFIER_LENGTH].strip("-") or default


def _ctx_value(runtime_ctx: dict[str, Any], key: str, default: str = "default") -> str:
    return slugify_identifier(runtime_ctx.get(key), default=default)


def resolve_collection_name(config: MemPalaceConfig, runtime_ctx: dict[str, Any]) -> str:
    if config.collection_name:
        explicit = slugify_identifier(config.collection_name, default="mempalace")
        return explicit or "mempalace"

    template = config.collection_template or "hermes-{platform}-{user_id}"
    values = {
        "user_id": _ctx_value(runtime_ctx, "user_id"),
        "platform": _ctx_value(runtime_ctx, "platform"),
        "session_id": _ctx_value(runtime_ctx, "session_id"),
        "agent_id": _ctx_value(runtime_ctx, "agent_id", default="hermes"),
    }
    try:
        rendered = template.format(**values)
    except Exception:
        rendered = "hermes-{platform}-{user_id}".format(**values)
    return slugify_identifier(rendered, default="mempalace")


def resolve_room(
    config: MemPalaceConfig,
    runtime_ctx: dict[str, Any],
    explicit_room: str | None = None,
) -> str:
    if explicit_room:
        return slugify_identifier(explicit_room)

    strategy = config.room_strategy
    if strategy == "fixed":
        return slugify_identifier(config.fixed_room, default="memory")
    if strategy == "platform_session":
        return slugify_identifier(f"{runtime_ctx.get('platform', 'default')}-{runtime_ctx.get('session_id', 'default')}")
    if strategy == "user_platform":
        return slugify_identifier(f"{runtime_ctx.get('user_id', 'default')}-{runtime_ctx.get('platform', 'default')}")
    return slugify_identifier(runtime_ctx.get("session_id"), default="default")
