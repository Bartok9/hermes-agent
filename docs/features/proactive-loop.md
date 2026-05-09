# Proactive Loop — Hermes Initiates

> *"I'd love to have Hermes message me occasionally on its own."* — @charlesmcdowell, 2.2K views, Teknium replied "This is a good idea 🤔"

## What This Is

The Proactive Loop gives Hermes a **synthesis-and-initiative pass** that runs on a configurable schedule — by default nightly, but adjustable per user. After each synthesis window, Hermes:

1. Reviews the day's conversation history and tool outputs
2. Identifies unresolved threads, patterns, and opportunities
3. Decides **autonomously** whether something is worth surfacing
4. If yes — sends the user a message **without being asked**

This is the difference between a tool and a partner. A tool waits. A partner notices.

---

## Design Philosophy

**The bar for reaching out must be high.** Nobody wants spam. Hermes only initiates when:

- Something genuinely new was learned or discovered
- A task it was running has a result worth sharing
- A question from earlier now has an answer
- A pattern in the user's requests suggests a proactive action that saves them time

The default threshold is `conservative` — Hermes would rather stay quiet than interrupt unnecessarily.

**The message must feel natural, not robotic.** No "NIGHTLY SYNTHESIS REPORT" headers. Just a message like a thoughtful assistant would send — conversational, warm, specific.

---

## Architecture

### Components

```
hermes_cli/
├── proactive_loop.py       ← NEW: Core synthesis + initiative engine
├── proactive_scheduler.py  ← NEW: Schedule management, trigger conditions
└── proactive_threshold.py  ← NEW: Decides whether to reach out (pluggable)

config options (hermes_cli/config.py additions):
  proactive_loop.enabled         bool    default: false (opt-in)
  proactive_loop.schedule        str     cron expr, default: "0 22 * * *" (10pm)  
  proactive_loop.threshold       str     "conservative" | "balanced" | "eager"
  proactive_loop.max_per_day     int     default: 1
  proactive_loop.channels        list    which channels to send on (Telegram, Discord, etc.)
```

### The Synthesis Pass

```python
# hermes_cli/proactive_loop.py

class ProactiveLoop:
    """
    Runs a synthesis pass over recent session history and decides
    whether to send the user an unprompted message.
    
    Design invariants:
    - NEVER modifies session state, memory, or system prompt
    - NEVER sends more than max_per_day messages  
    - ALWAYS uses a deduplicated window (won't surface the same insight twice)
    - Falls back to NO MESSAGE if synthesis fails for any reason
    - Uses a cheap/fast model for the synthesis judge call
    """
    
    async def run_synthesis(self, session_id: str, history_window_hours: int = 16) -> SynthesisResult:
        """
        1. Load recent session history (last N hours)
        2. Extract: unresolved questions, completed tasks, new learnings, patterns
        3. Score each candidate message by: novelty, relevance, actionability
        4. Apply threshold filter
        5. If threshold met: compose a natural message
        6. Return SynthesisResult with message (or None if below threshold)
        """
    
    async def should_reach_out(self, synthesis: SynthesisResult) -> bool:
        """
        The gate. Only returns True if something genuinely worth saying
        was found. Errs heavily toward False.
        """

@dataclass
class SynthesisResult:
    should_send: bool
    message: str | None
    reasoning: str           # for audit log — why it decided to send or not
    novelty_score: float     # 0-1, how new is this vs prior messages
    candidates: list[str]    # all potential messages before filtering
```

### Threshold Engine

Three built-in thresholds, all pluggable:

| Threshold | When it sends | Good for |
|-----------|---------------|---------|
| `conservative` | Only truly significant findings | Most users (default) |
| `balanced` | Useful insights + completed task results | Power users |
| `eager` | Anything interesting | Users who want maximum initiative |

Custom thresholds can be registered via `ProactiveThreshold` protocol — same plugin architecture as skills.

### Message Composition

Hermes composes proactive messages to feel natural, not automated:

**Bad (robotic):**
> NIGHTLY SYNTHESIS: 3 items require your attention. Item 1: The build task you requested...

**Good (natural):**
> Hey — I finished looking at those logs you asked about earlier. Found something interesting: the error pattern repeats every 4 hours, always at :15 past. That's almost certainly a cron job. Want me to find which one?

The composition prompt instructs the model to write as if continuing a conversation, not broadcasting a report.

---

## What Gets Synthesized

The synthesis window considers:

1. **Unresolved threads** — questions asked but not fully answered, tasks started but not completed
2. **Tool output summaries** — results from searches, code runs, file scans that the user might want to know about
3. **Pattern detection** — recurring requests that suggest a workflow improvement
4. **External triggers** — calendar events, monitored URLs, subscribed alerts (if configured)
5. **User-defined watchpoints** — explicit "tell me when X happens" instructions from prior turns

---

## User Controls

```bash
# Enable in config
hermes config set proactive_loop.enabled true

# Set schedule (cron expression)
hermes config set proactive_loop.schedule "0 22 * * *"   # 10pm nightly

# Set threshold
hermes config set proactive_loop.threshold conservative

# Disable for a period
hermes proactive pause 24h

# Review what it would have said (dry run)
hermes proactive dry-run

# See recent proactive messages sent
hermes proactive history
```

The user can also say **in conversation**: "Message me tonight if you find anything about X" — and the proactive loop respects these inline instructions.

---

## Privacy and Safety

- **Opt-in by default** — proactive_loop.enabled is false until the user explicitly turns it on
- **No external data collection** — synthesis only looks at the user's own session history
- **Audit log** — every synthesis pass is logged with its reasoning, whether it sent or not
- **Rate limiting** — hard cap of max_per_day regardless of threshold
- **Kill switch** — `hermes proactive off` immediately disables all future proactive messages
- **Per-channel control** — can enable only for specific channels (e.g., Telegram but not Discord)

---

## Implementation Notes for Contributors

The existing `GoalManager` (`hermes_cli/goals.py`) handles **user-initiated** loops. The `ProactiveLoop` is the **agent-initiated** complement — structurally similar but triggered by a scheduler rather than user input, and subject to stricter send-or-don't-send gating.

The scheduler integrates with the existing cron infrastructure in `hermes_cli/gateway.py`. On platforms without a running gateway, the synthesis pass is deferred until the next gateway start.

The synthesis model call uses the same provider/model as the session's configured judge (from `goals.py` DEFAULT_JUDGE_TIMEOUT pattern) — lightweight, cheap, fast.

---

## Related

- `hermes_cli/goals.py` — user-initiated persistent loop (the Ralph loop)
- `hermes_cli/callbacks.py` — event hooks (subscribe/notify pattern)
- `docs/features/goals.md` — persistent goals documentation
- Issue: [#charlesmcdowell request](https://x.com/charlesmcdowell/status/2052846836356907209) (surfaced May 8, 2026)
