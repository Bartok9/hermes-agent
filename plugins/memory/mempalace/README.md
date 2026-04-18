# MemPalace Memory Provider

Local-first memory provider for Hermes Agent using [MemPalace](https://github.com/mempalace/mempalace). It stores memories in a configurable ChromaDB collection, supports semantic search, optional knowledge-graph initialization, room scoping, structured metadata, and Hermes memory-provider hooks.

## What this plugin provides

- Configurable collection naming via `collection_name` or `collection_template`
- Configurable room derivation via `room_strategy`
- Structured metadata for tool writes, sync-turn writes, compression writes, and builtin memory mirroring
- Five Hermes tools:
  - `mempalace_memorize`
  - `mempalace_search`
  - `mempalace_recall`
  - `mempalace_forget`
  - `mempalace_status`
- Hook support for:
  - `sync_turn()`
  - `prefetch()` / `queue_prefetch()`
  - `on_memory_write()`
  - `on_pre_compress()`
  - `on_session_end()`

## Requirements

- Python environment with the `mempalace` package installed
- Hermes Agent plugin system enabled
- Writable Hermes home directory

Example install:

```bash
pip install mempalace
```

## Setup

### 1. Put the plugin in the Hermes plugins directory

Expected location:

```text
plugins/memory/mempalace/
```

Files included by this plugin:

```text
__init__.py
plugin.yaml
provider.py
tools.py
hooks.py
config.py
collections.py
metadata.py
errors.py
store.py
writer.py
events.py
schemas.py
```

### 2. Enable the provider in Hermes config

Set the active memory provider to `mempalace` in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: mempalace

mempalace:
  palace_path: ~/.hermes/mempalace
  wing: conversations
  n_results: 5
  tool_max_results: 20
  enable_kg: true
  collection_template: hermes-{platform}-{user_id}
  room_strategy: platform_session
  fixed_room: memory
```

## Configuration

The plugin reads config from the nested `mempalace:` block in Hermes config.

| Key | Default | Description |
|---|---|---|
| `palace_path` | `$HERMES_HOME/mempalace` | Root directory for MemPalace persistent data |
| `wing` | `conversations` | Logical MemPalace wing used for records |
| `n_results` | `5` | Default semantic search result count |
| `tool_max_results` | `20` | Hard cap for tool result counts |
| `enable_kg` | `true` | Initialize knowledge graph when available |
| `collection_name` | empty | Explicit collection name; overrides template |
| `collection_template` | `hermes-{platform}-{user_id}` | Collection naming template |
| `room_strategy` | `platform_session` | Default room derivation strategy |
| `fixed_room` | `memory` | Used when `room_strategy: fixed` |

### `collection_name` vs `collection_template`

If `collection_name` is set, it wins.

Example:

```yaml
mempalace:
  collection_name: hermes-telegram-jessica
```

If `collection_name` is empty, the plugin renders `collection_template` using runtime fields:

- `{user_id}`
- `{platform}`
- `{session_id}`
- `{agent_id}`

Example:

```yaml
mempalace:
  collection_template: hermes-{platform}-{user_id}
```

This might resolve to:

```text
hermes-telegram-7892983586
```

### Room strategies

Available `room_strategy` values:

- `fixed`
- `session`
- `platform_session`
- `user_platform`

Examples:

```yaml
mempalace:
  room_strategy: fixed
  fixed_room: memory
```

```yaml
mempalace:
  room_strategy: platform_session
```

With `platform_session`, a Telegram thread/session may resolve to a room like:

```text
telegram-thread-42
```

## Tools

### `mempalace_memorize`

Store an explicit memory.

Arguments:

- `content` (required)
- `memory_type` (optional)
- `importance` (optional)
- `room` (optional)

Example:

```json
{
  "content": "Jessica prefers detailed technical explanations in Chinese.",
  "memory_type": "preference",
  "importance": 0.95,
  "room": "prefs"
}
```

### `mempalace_search`

Semantic search over stored memories.

Arguments:

- `query` (required)
- `room` (optional)
- `top_k` (optional)

### `mempalace_recall`

Fetch memories from a room.

Arguments:

- `room` (optional)
- `n_results` (optional)

### `mempalace_forget`

Delete a memory by ID.

Arguments:

- `memory_id` (required)

### `mempalace_status`

Returns runtime status such as:

- active collection name
- room strategy
- wing
- configured result limits
- knowledge-graph initialization status

## Notes for users

### Collection stability matters

If you previously stored memories in a collection like:

```text
hermes-telegram-jessica
```

but your current runtime resolves to:

```text
hermes-telegram-7892983586
```

the plugin is still working, but it will read/write a different collection. In that case, either:

1. set `collection_name` explicitly to the historical collection name, or
2. migrate old records into the new collection

### Metadata shape

Stored records include normalized metadata such as:

- `room`
- `wing`
- `source`
- `message_kind`
- `session_id`
- `platform`
- `user_id`
- `agent_id`
- `created_at`

Optional fields such as `memory_type` and `importance` are included when provided.

## Verification

The plugin was verified with:

```bash
pytest tests/plugins/test_mempalace_v2_foundation.py \
       tests/plugins/test_mempalace_module_layout.py \
       tests/plugins/test_mempalace_plugin_loader.py \
       tests/plugins/test_mempalace_e2e.py -q
```

It also passed a live importlib-style runtime check covering:

- plugin load
- provider initialization
- `mempalace_status`
- `mempalace_memorize`
- `mempalace_search`
- `mempalace_recall`

## Suggested reviewer quick start

1. Install `mempalace`
2. Copy the plugin into `plugins/memory/mempalace/`
3. Set `memory.provider: mempalace`
4. Add a `mempalace:` block in `~/.hermes/config.yaml`
5. Start Hermes
6. Call `mempalace_status`
7. Store a test fact with `mempalace_memorize`
8. Verify retrieval with `mempalace_search`
