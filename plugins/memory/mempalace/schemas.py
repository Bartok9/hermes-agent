from __future__ import annotations

MEMORIZE_SCHEMA = {
    "name": "mempalace_memorize",
    "description": "Store an important fact, preference, or decision in MemPalace for long-term memory. Use this when the user shares personal information, preferences, goals, or decisions that should be remembered across sessions.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact, preference, or decision to remember.",
            },
            "memory_type": {
                "type": "string",
                "enum": ["factual", "preference", "goal", "instruction", "event", "opinion"],
                "description": "Category of memory (default: factual).",
            },
            "importance": {
                "type": "number",
                "description": "Importance score 0-1 (default: 0.7).",
            },
            "room": {
                "type": "string",
                "description": "Optional room/thread name to organize this memory (e.g. project name).",
            },
        },
        "required": ["content"],
    },
}

MEMORY_SEARCH_SCHEMA = {
    "name": "mempalace_search",
    "description": "Search MemPalace long-term memory using semantic similarity. Finds relevant memories even when the query doesn't match exact words.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for.",
            },
            "room": {
                "type": "string",
                "description": "Optional room/thread to limit search scope.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max results (default: 5, max: 20).",
            },
        },
        "required": ["query"],
    },
}

MEMORY_RECALL_SCHEMA = {
    "name": "mempalace_recall",
    "description": "Recall recent memories from the memory palace. Returns the most recent stored memories across all rooms.",
    "parameters": {
        "type": "object",
        "properties": {
            "room": {
                "type": "string",
                "description": "Optional room/thread to recall from.",
            },
            "n_results": {
                "type": "integer",
                "description": "Number of memories to recall (default: 5).",
            },
        },
        "required": [],
    },
}

MEMORY_FORGET_SCHEMA = {
    "name": "mempalace_forget",
    "description": "Delete a specific memory by its ID from MemPalace.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "The memory ID to delete.",
            },
        },
        "required": ["memory_id"],
    },
}

MEMORY_STATUS_SCHEMA = {
    "name": "mempalace_status",
    "description": "Get MemPalace memory system status — total memories, knowledge graph stats, collection info, and system health.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOL_SCHEMAS = [
    MEMORIZE_SCHEMA,
    MEMORY_SEARCH_SCHEMA,
    MEMORY_RECALL_SCHEMA,
    MEMORY_FORGET_SCHEMA,
    MEMORY_STATUS_SCHEMA,
]
