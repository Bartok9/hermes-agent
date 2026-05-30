# Proactive Communication Loop

> Hermes reaches out to you. Unprompted. When it sees something you can't.

## What this is

The Proactive Communication Loop is not a notification system. It's not a summary. It's not a reminder.

It is the moment the agent notices that the problem you're working on today is the same problem you solved three weeks ago — from a different angle — and you've forgotten. It reaches out and tells you.

**The bar is high.** Most days it stays silent. When it does send a message, it arrives when you're already in flow — at the hour of day when your own history shows you do your best work.

---

## How it works

```
┌─────────────────────────────────────────────────────────┐
│  1. BartokGraph                                          │
│     Walks your workspace. Extracts weighted concepts     │
│     from every file. Builds a knowledge graph.           │
│     Runs once, refreshes every 7 days. On-device only.  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  2. Flow Analysis                                        │
│     Studies 30 days of message history.                 │
│     Finds your peak creative hour — when you write      │
│     the longest messages in the longest sessions.       │
│     Updated weekly. Falls back to 9 AM if no history.  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  3. Scheduler                                            │
│     Runs inside the gateway cron ticker (every minute). │
│     Checks: is any session in its ±15 min peak window?  │
│     If yes, and synthesis hasn't fired today: trigger.  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  4. Synthesis                                            │
│     Extracts today's active topics from 72h history.    │
│     Traverses the knowledge graph for dormant nodes     │
│     that connect to those topics.                       │
│     Scores by: semantic × importance × temporal decay   │
│               × god-node boost × cluster alignment.     │
│     Judge model decides: is this worth saying?          │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  5. Delivery                                             │
│     If the judge says yes: message sent to the user's   │
│     channel (Telegram, Discord, Signal — wherever they  │
│     talk to Hermes). Natural language. No header.       │
│     The mechanism is never mentioned.                   │
└─────────────────────────────────────────────────────────┘
```

---

## BartokGraph

BartokGraph is the knowledge graph builder that makes this feature possible. It is a Python port of `bartokgraph-v2.mjs`, running entirely on-device.

### Three layers

**Layer 1 — Knowledge** (the layer the PCL reads)
Extracts concepts from prose: markdown headers, bold text, rules. Weighted by source file type.

**Layer 2 — Code intelligence**
Maps function/class/import graphs for codebase navigation. Not used by PCL directly.

**Layer 3 — Person graphs**
Filtered views per person, driven by `bartokgraph-config.json` in the workspace root. No personal names hardcoded.

### File weight system

The weight of a concept node is determined by where it came from:

| Source | Weight |
|--------|--------|
| `SOUL.md`, `USER.md`, `MEMORY.md`, `AGENTS.md` | 50 |
| Daily memory logs (`memory/YYYY-MM-DD.md`) | 20 |
| Project knowledge (`projects/**/*.md`) | 15 |
| Research notes (`research/`) | 12 |
| General prose (`.md`, `.txt`) | 8 |
| Documents (`.html`, `.pdf`) | 6 |
| Structured data (`.json`, `.jsonl`) | 4 |
| Code (`.py`, `.ts`, `.js`, `.mjs`) | 1 |
| Test files | 0.1 |

Knowledge and person layer nodes get a 10× multiplier over code layer nodes. A `SOUL.md` node in the knowledge layer has an effective weight of 500 — the maximum. A test file code node has 0.1.

### `last_seen_ts` — file mtime, not build time

Every node carries the actual last-modified time of its source file. Not the build timestamp.

This is critical. Without it, a freshly-built graph marks every node as "active right now" — and the PCL filters out all nodes active in the last 24 hours. The feature would silently produce zero connections on every fresh graph.

### God nodes and clusters

After building, BartokGraph identifies:

- **God nodes** — the 20 most connected, highest-weight nodes. These are the conceptual core of the user's knowledge. Connections to god nodes get a 1.5× surprise boost.
- **Clusters** — groups of structurally connected concepts (Union-Find). If today's active topic is in the same cluster as a dormant god node, that's a 1.3× cluster alignment boost.

### CLI

```bash
# Build knowledge graph from workspace (auto-saved to workspace/.bartokgraph/)
python -m hermes_cli.bartokgraph build ~/workspace

# Build all layers + person graphs
python -m hermes_cli.bartokgraph build ~/workspace --all

# Build for a specific person (requires bartokgraph-config.json)
python -m hermes_cli.bartokgraph build ~/workspace --person alice

# Query
python -m hermes_cli.bartokgraph query graph.json "regenerative agriculture"

# Report
python -m hermes_cli.bartokgraph report graph.json
```

---

## Flow analysis

The scheduler learns each user's peak creative window from their message history.

Three signals, combined:

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| Message frequency | 30% | When are they most active? |
| Message depth | 40% | Average message length — long messages signal deep work |
| Session continuity | 30% | Sustained hours (adjacent windows active), not brief check-ins |

The result is a `FlowProfile` with a `peak_hour` (0–23, local time) and a confidence score. The profile is updated weekly.

**Fallback:** If fewer than 20 user messages exist, defaults to 9 AM.

**Config override:** Set `proactive_communication.peak_flow_hour: 14` to pin the synthesis window to any hour.

---

## Surprise scoring

When the graph is traversed, connections are ranked by:

```
surprise = semantic_strength × node_importance × temporal_decay
           × god_node_boost × cluster_alignment_boost
```

- **semantic_strength** — Jaccard word overlap between today's active topic and the dormant node
- **node_importance** — normalized 0–1 from source file weight (SOUL.md = 1.0, test file = 0.0002)
- **temporal_decay** — `1 + log(1 + days_apart / 7)` — older dormant connections score higher
- **god_node_boost** — 1.5× if the dormant node is a god node
- **cluster_alignment_boost** — 1.3× if today's topic and the dormant node share a cluster

A test file node will never surface regardless of semantic match or age. A SOUL.md node from three weeks ago, strongly connected to today's work, scores maximum.

---

## Connection types

When a connection scores above the threshold and the judge model approves, the message is classified as one of:

| Type | What it means |
|------|---------------|
| `temporal_bridge` | Same concept appeared weeks ago — user likely forgot |
| `cross_domain` | Structurally identical problem in a different context |
| `person_knowledge` | Something a specific person mentioned connects to today's work |

The message never mentions the type, the graph, or the mechanism. It leads with the insight.

**Example — temporal bridge:**
> "Hey — just noticed something. Three weeks ago you were working on the same core problem from a different angle. The solution you found then applies directly to what you're building now."

**Example — person knowledge:**
> "Sarah mentioned the Kenya project last week. What you're building today connects to it in a way neither of you saw."

---

## Configuration

```yaml
proactive_communication:
  enabled: false                    # opt-in (default: false)
  threshold: conservative           # conservative (0.75) | balanced (0.55) | eager (0.35)
  max_per_day: 1                    # hard cap per session per day
  peak_flow_hour: ~                 # optional override (0-23); auto-detected if unset
  bartokgraph:
    enabled: true                   # use graph augmentation
    workspace: "~"                  # path to walk
    rebuild_interval_days: 7        # how often to rebuild the graph
    auto_build: true                # build on first use if no graph exists

timezone_offset_hours: -4           # UTC offset for local time (e.g. -4 for EDT)
```

Enable with:
```
hermes config set proactive_communication.enabled true
```

---

## Privacy

**Everything stays on your machine.**

- BartokGraph walks local files and writes `graph.json` to `workspace/.bartokgraph/`
- No data is sent to Supabase, any cloud service, or any third party
- Credential redaction runs on every file before extraction (API keys, JWTs, passwords → `[CREDENTIAL]`)
- Person graphs are filtered by local config — no personal names are hardcoded in the codebase
- The judge model call uses Hermes's already-configured provider (the same one you use for conversation)

---

## New files

| File | Purpose |
|------|---------|
| `hermes_cli/bartokgraph.py` | Full BartokGraph v2.0 Python port — graph builder, extractors, CLI |
| `hermes_cli/bartokgraph_adapter.py` | BartokGraph ↔ ProactiveCommunicationLoop bridge |
| `hermes_cli/proactive_communication_loop.py` | Synthesis engine — traverses graph, scores, judges, composes |
| `hermes_cli/proactive_scheduler.py` | Flow analyzer + gateway cron integration |

Tests: `tests/test_proactive_graph.py`, `tests/test_proactive_communication_loop.py`, `tests/test_proactive_scheduler.py`

---

## What's not in this PR (follow-up)

The gateway cron wire (`_start_cron_ticker` in `gateway/run.py`) is included. The scheduler initializes and ticks automatically when the gateway starts with `proactive_communication.enabled: true`.

What this PR does not include:
- Embedding-based semantic similarity (currently uses Jaccard word overlap — good enough for concept-level nodes, production would use vectors)
- `hermes bartokgraph` CLI command registration (the Python module can be called directly; CLI registration is a follow-up)
- Full registry hive / Prefetch parsing in the code intelligence layer
