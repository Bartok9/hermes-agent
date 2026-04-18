"""MemPalace memory plugin — MemoryProvider interface.

Local-first AI memory with semantic search, knowledge graphs, and spatial
memory palace. Stores memories in ChromaDB with entity extraction via
knowledge graph.
"""

from __future__ import annotations

from .provider import MemPalaceMemoryProvider

__all__ = ["MemPalaceMemoryProvider", "register"]


def register(ctx) -> None:
    """Register MemPalace as a memory provider plugin."""
    ctx.register_memory_provider(MemPalaceMemoryProvider())
