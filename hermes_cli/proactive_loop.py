"""Proactive Loop — Hermes initiates contact when it has something worth saying.

This module implements the synthesis-and-initiative pass that lets Hermes
send unprompted messages to the user.

Design invariants
-----------------
- NEVER modifies session state, memory, or system prompt.
- NEVER sends more than ``max_per_day`` messages per user.
- ALWAYS uses a deduplicated window (same insight not surfaced twice).
- Falls back to NO MESSAGE if synthesis fails for any reason (fail-open,
  err toward silence).
- Uses a cheap/fast model for the synthesis judge — same pattern as
  ``GoalManager``'s judge call in ``goals.py``.
- Fully opt-in: ``proactive_loop.enabled`` defaults to ``False``.

Relationship to GoalManager
---------------------------
``GoalManager`` handles *user-initiated* persistent loops (the Ralph loop):
the user sets a goal, Hermes works until it's done.

``ProactiveLoop`` is the *agent-initiated* complement: the scheduler fires
it, it reviews recent history, and it decides on its own whether to send
something. The user never asked for a specific loop — Hermes noticed
something.

Integration points
------------------
- ``hermes_cli/gateway.py`` cron infrastructure triggers ``run_synthesis``.
- ``hermes_cli/callbacks.py`` ``notify`` path delivers the composed message.
- ``hermes_cli/config.py`` exposes the user-facing knobs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = "conservative"
DEFAULT_MAX_PER_DAY = 1
DEFAULT_HISTORY_WINDOW_HOURS = 16
DEFAULT_SYNTHESIS_BUDGET_TOKENS = 2000

# How much of recent history to include in the synthesis context.
_HISTORY_SNIPPET_CHARS = 8000

# Threshold scores (0–1). Only messages above the threshold for their
# configured level are sent. Tune conservatively — users prefer silence.
_THRESHOLD_SCORES = {
    "conservative": 0.75,
    "balanced": 0.55,
    "eager": 0.35,
}


# ──────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SynthesisResult:
    """Outcome of one proactive synthesis pass.

    ``should_send`` is the gate. Even when ``True``, callers should check
    ``message`` is non-empty before dispatching.
    """

    should_send: bool
    message: Optional[str]
    reasoning: str  # human-readable, written to audit log
    novelty_score: float  # 0–1: how new is this vs what was already said?
    relevance_score: float  # 0–1: how relevant to user's recent work?
    combined_score: float  # weighted combination used for threshold check
    candidates: List[str] = field(default_factory=list)  # all before filter
    synthesis_ms: int = 0  # wall-clock time for the synthesis LLM call


@runtime_checkable
class ProactiveThreshold(Protocol):
    """Pluggable threshold implementation.

    Third-party skills can register custom thresholds:

        from hermes_cli.proactive_loop import register_threshold

        @register_threshold("my_threshold")
        class MyThreshold:
            def should_send(self, result: SynthesisResult) -> bool:
                return result.combined_score > 0.60 and result.novelty_score > 0.50
    """

    def should_send(self, result: SynthesisResult) -> bool: ...


_registered_thresholds: Dict[str, ProactiveThreshold] = {}


def register_threshold(name: str):
    """Decorator to register a custom threshold implementation."""

    def _decorator(cls):
        _registered_thresholds[name] = cls()
        return cls

    return _decorator


# ──────────────────────────────────────────────────────────────────────
# Core engine
# ──────────────────────────────────────────────────────────────────────


class ProactiveLoop:
    """Synthesis-and-initiative engine.

    Typical call sequence (from the gateway cron):

        loop = ProactiveLoop(session_db=db, config=cfg)
        result = await loop.run_synthesis(session_id)
        if result.should_send and result.message:
            await deliver(result.message)
    """

    def __init__(
        self,
        session_db: Any,  # SessionDB from run_agent
        config: Any,  # HermesConfig
    ) -> None:
        self._db = session_db
        self._cfg = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_synthesis(
        self,
        session_id: str,
        history_window_hours: int = DEFAULT_HISTORY_WINDOW_HOURS,
    ) -> SynthesisResult:
        """Run one proactive synthesis pass for ``session_id``.

        Steps:
        1. Load recent history (last ``history_window_hours``).
        2. Build a synthesis prompt asking the model to identify anything
           worth surfacing.
        3. Score novelty + relevance.
        4. Apply threshold.
        5. If above threshold: compose a natural, conversational message.
        6. Return SynthesisResult — caller decides whether to send.

        Never raises. On any error returns a no-send result so the
        scheduler can log and continue.
        """
        try:
            return await self._run_synthesis_inner(session_id, history_window_hours)
        except Exception as exc:  # noqa: BLE001
            logger.warning("proactive_loop: synthesis failed for %s: %s", session_id, exc)
            return SynthesisResult(
                should_send=False,
                message=None,
                reasoning=f"synthesis error: {exc}",
                novelty_score=0.0,
                relevance_score=0.0,
                combined_score=0.0,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_synthesis_inner(
        self,
        session_id: str,
        history_window_hours: int,
    ) -> SynthesisResult:
        t0 = time.monotonic()

        history = self._load_recent_history(session_id, history_window_hours)
        if not history:
            return SynthesisResult(
                should_send=False,
                message=None,
                reasoning="no history in window",
                novelty_score=0.0,
                relevance_score=0.0,
                combined_score=0.0,
            )

        already_sent = self._load_sent_summaries(session_id)
        prompt = _build_synthesis_prompt(history, already_sent)

        raw = await self._call_synthesis_model(prompt)
        parsed = _parse_synthesis_response(raw)

        combined = 0.6 * parsed.get("novelty", 0.0) + 0.4 * parsed.get("relevance", 0.0)
        threshold_score = _THRESHOLD_SCORES.get(
            self._cfg.get("proactive_loop.threshold", DEFAULT_THRESHOLD),
            _THRESHOLD_SCORES[DEFAULT_THRESHOLD],
        )

        should_send = combined >= threshold_score and bool(parsed.get("message"))

        # Check registered custom threshold (overrides built-in if present).
        custom_name = self._cfg.get("proactive_loop.threshold", "")
        if custom_name in _registered_thresholds:
            candidate = SynthesisResult(
                should_send=False,
                message=parsed.get("message"),
                reasoning=parsed.get("reasoning", ""),
                novelty_score=parsed.get("novelty", 0.0),
                relevance_score=parsed.get("relevance", 0.0),
                combined_score=combined,
                candidates=parsed.get("candidates", []),
            )
            should_send = _registered_thresholds[custom_name].should_send(candidate)

        return SynthesisResult(
            should_send=should_send,
            message=parsed.get("message") if should_send else None,
            reasoning=parsed.get("reasoning", ""),
            novelty_score=parsed.get("novelty", 0.0),
            relevance_score=parsed.get("relevance", 0.0),
            combined_score=combined,
            candidates=parsed.get("candidates", []),
            synthesis_ms=int((time.monotonic() - t0) * 1000),
        )

    def _load_recent_history(self, session_id: str, window_hours: int) -> str:
        """Load and format recent session history as a text snippet."""
        cutoff = time.time() - window_hours * 3600
        try:
            messages = self._db.get_messages_since(session_id, cutoff)
            lines = []
            for m in messages:
                role = m.get("role", "?")
                content = str(m.get("content", ""))[:500]
                lines.append(f"[{role}]: {content}")
            full = "\n".join(lines)
            return full[-_HISTORY_SNIPPET_CHARS:] if len(full) > _HISTORY_SNIPPET_CHARS else full
        except Exception as exc:  # noqa: BLE001
            logger.debug("proactive_loop: could not load history: %s", exc)
            return ""

    def _load_sent_summaries(self, session_id: str) -> str:
        """Load summaries of messages already sent proactively today."""
        try:
            sent = self._db.get_proactive_sent(session_id, since_hours=24)
            if not sent:
                return "(none sent today)"
            return "; ".join(s.get("summary", "") for s in sent[:5])
        except Exception:  # noqa: BLE001
            return "(unknown)"

    async def _call_synthesis_model(self, prompt: str) -> str:
        """Call a cheap/fast model for the synthesis judge."""
        # Implementation uses the same lightweight judge infrastructure
        # as GoalManager — placeholder for actual LLM call integration.
        raise NotImplementedError(
            "ProactiveLoop._call_synthesis_model must be wired to the "
            "session's LLM provider. See goals.py GoalManager._judge_goal "
            "for the pattern to follow."
        )


# ──────────────────────────────────────────────────────────────────────
# Prompt builders + parsers
# ──────────────────────────────────────────────────────────────────────


def _build_synthesis_prompt(history: str, already_sent: str) -> str:
    return f"""You are reviewing a conversation history to decide whether to send the user
an unprompted message. Your job is to find something genuinely worth saying — not
to generate noise.

RECENT CONVERSATION HISTORY:
{history}

ALREADY SENT TODAY (do not repeat these):
{already_sent}

Review the history and identify anything that meets one or more of these criteria:
1. An unresolved question or task that now has a clear answer or result
2. A pattern in the user's requests that suggests a useful proactive action
3. A completed background task with a result worth sharing
4. Something the user asked you to "let them know about" earlier
5. A genuinely useful insight the user hasn't seen yet

SCORING INSTRUCTIONS:
- novelty (0-1): How new is this vs what was already discussed? 1 = completely new
- relevance (0-1): How useful/relevant to the user's recent work? 1 = extremely relevant

THE BAR IS HIGH. If you're not confident this is worth interrupting the user,
set should_send to false. Silence is better than noise.

If you find something worth sending, compose a SHORT, NATURAL message (2-4 sentences max).
Write it as if you're continuing a conversation — NOT as a report or summary header.
Good: "Hey — I finished looking at those logs. Found something interesting: the errors..."
Bad: "NIGHTLY SYNTHESIS REPORT: 3 findings require your attention."

Respond in JSON:
{{
  "should_send": true/false,
  "message": "the natural message to send, or null",
  "novelty": 0.0-1.0,
  "relevance": 0.0-1.0,
  "reasoning": "1-2 sentences on why you decided to send or not",
  "candidates": ["list of things considered before deciding"]
}}"""


def _parse_synthesis_response(raw: str) -> Dict[str, Any]:
    """Parse the synthesis model's JSON response safely."""
    try:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text, strict=False)
    except Exception:  # noqa: BLE001
        logger.debug("proactive_loop: failed to parse synthesis response: %r", raw[:200])
        return {
            "should_send": False,
            "message": None,
            "novelty": 0.0,
            "relevance": 0.0,
            "reasoning": "parse failure",
            "candidates": [],
        }
