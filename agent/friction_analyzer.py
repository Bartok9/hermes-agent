"""
Session Friction Analyzer for Hermes Agent.

Analyzes historical session transcripts to detect recurring error patterns,
tool-loop failures, and anti-patterns that reduce agent effectiveness.
Generates actionable rule suggestions to prevent repeat failures.

Inspired by the "compound learning" architecture: every mistake becomes data,
every pattern becomes a rule, every rule compounds into better performance.

Key friction categories tracked:
    - error_loop: Same error repeated 3+ times before resolution
    - repeated_tool_calls: Same tool called with identical args multiple times
    - api_credential_failure: Auth/credential errors requiring intervention
    - infrastructure_broken: Tools/scripts that fail due to missing dependencies
    - memory_dropout: VIP information received but not persisted

Usage:
    analyzer = FrictionAnalyzer(db)
    report = analyzer.analyze(days=7)
    rules = analyzer.generate_rules(report)
    print(analyzer.format_report(report))
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Friction pattern definitions ────────────────────────────────────────────

FRICTION_PATTERNS = {
    "error_loop": {
        "description": "Same error repeated 3+ times before resolution",
        "weight": 3,
        "keywords": ["error", "failed", "exception", "traceback", "cannot", "unable"],
        "threshold": 3,
    },
    "repeated_tool_calls": {
        "description": "Identical tool call made 3+ times without state change",
        "weight": 2,
        "threshold": 3,
    },
    "api_credential_failure": {
        "description": "Authentication or credential error requiring intervention",
        "weight": 4,
        "keywords": ["401", "403", "unauthorized", "invalid.*key", "api key", "credential",
                    "authentication failed", "permission denied", "token expired"],
    },
    "infrastructure_broken": {
        "description": "Tool or script fails due to missing dependency or broken path",
        "weight": 3,
        "keywords": ["no such file", "command not found", "module not found",
                    "not installed", "enoent", "sigkill", "sigterm"],
    },
    "memory_dropout": {
        "description": "VIP information received but agent failed to persist it",
        "weight": 2,
        "keywords": ["didn't know", "forgot", "you already told me", "i already told you",
                    "didn't you know"],
    },
    "premature_declaration": {
        "description": "Agent declared task complete without verification",
        "weight": 2,
        "keywords": ["done ✅", "completed", "finished", "all set"],
    },
    "wrong_diagnosis": {
        "description": "Agent attempted fix without verifying the actual root cause",
        "weight": 2,
        "keywords": ["that didn't work", "still failing", "same error", "not fixed"],
    },
}



# ─── Noisy context suppression ───────────────────────────────────────────────
# These patterns indicate expected high-error workflows (Docker builds, npm
# installs, test runs) where error_loop detection would produce false positives.
# Sessions containing these context markers suppress error_loop scoring.

NOISY_CONTEXT_PATTERNS = [
    "docker build",
    "docker-compose",
    "npm install",
    "npm ci",
    "npm rebuild",
    "yarn install",
    "pip install",
    "uv sync",
    "cargo build",
    "go build",
    "pytest",
    "running tests",
    "test suite",
    "running npm",
    "reinstall",
    "compiling",
    "building image",
    "pulling image",
    "running migrations",
]

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FrictionEvent:
    session_id: str
    timestamp: float
    category: str
    description: str
    context: str
    weight: int = 1


@dataclass
class FrictionReport:
    days_analyzed: int
    sessions_scanned: int
    total_friction_score: int
    events: List[FrictionEvent] = field(default_factory=list)
    category_counts: Dict[str, int] = field(default_factory=dict)
    category_weights: Dict[str, int] = field(default_factory=dict)
    top_sessions: List[Tuple[str, int]] = field(default_factory=list)
    generated_rules: List[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Main analyzer ────────────────────────────────────────────────────────────

class FrictionAnalyzer:
    """Analyzes Hermes sessions for friction patterns and generates improvement rules."""

    def __init__(self, db=None, sessions_dir: Optional[str] = None):
        """
        Args:
            db: SQLite database connection (from Hermes state)
            sessions_dir: Optional path to JSONL session files
        """
        self._db = db
        self._sessions_dir = sessions_dir

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze(self, days: int = 7) -> FrictionReport:
        """Run full friction analysis over the last N days.

        Returns a FrictionReport with detected events, category breakdown,
        friction scores, and auto-generated rule suggestions.
        """
        sessions = self._load_sessions(days)
        events: List[FrictionEvent] = []

        for session in sessions:
            session_events = self._analyze_session(session)
            events.extend(session_events)

        # Aggregate
        category_counts: Counter = Counter(e.category for e in events)
        category_weights: Dict[str, int] = {}
        session_scores: Dict[str, int] = defaultdict(int)

        for event in events:
            weight = FRICTION_PATTERNS.get(event.category, {}).get("weight", 1)
            category_weights[event.category] = (
                category_weights.get(event.category, 0) + weight
            )
            session_scores[event.session_id] += weight

        total_score = sum(category_weights.values())
        top_sessions = sorted(session_scores.items(), key=lambda x: x[1], reverse=True)[:5]

        report = FrictionReport(
            days_analyzed=days,
            sessions_scanned=len(sessions),
            total_friction_score=total_score,
            events=events,
            category_counts=dict(category_counts),
            category_weights=category_weights,
            top_sessions=top_sessions,
        )

        report.generated_rules = self.generate_rules(report)
        return report

    def generate_rules(self, report: FrictionReport) -> List[str]:
        """Generate actionable rule suggestions from a friction report.

        Rules follow the format: rule-name: description of preventive action.
        """
        rules = []
        counts = report.category_counts

        if counts.get("api_credential_failure", 0) >= 2:
            rules.append(
                "api-credential-failure: Before using any API → verify credentials "
                "are valid (not placeholder values) by testing a simple request first."
            )

        if counts.get("infrastructure_broken", 0) >= 2:
            rules.append(
                "infrastructure-broken: Before relying on any tool, script, or service "
                "→ verify it exists and works with a quick test before building on top of it."
            )

        if counts.get("error_loop", 0) >= 2:
            rules.append(
                "error-loop: When seeing the same error 3+ times → stop, read the full "
                "error message, address the root cause instead of retrying."
            )

        if counts.get("repeated_tool_calls", 0) >= 2:
            rules.append(
                "repeated-tool-calls: When a tool call fails twice → stop and analyze "
                "why before trying again — check inputs, permissions, and dependencies."
            )

        if counts.get("memory_dropout", 0) >= 1:
            rules.append(
                "memory-dropout: When receiving important information from user → "
                "write it to memory immediately, not later."
            )

        if counts.get("premature_declaration", 0) >= 2:
            rules.append(
                "phantom-documentation: When claiming something is done → ALWAYS "
                "verify with a test/check before reporting success."
            )

        if counts.get("wrong_diagnosis", 0) >= 2:
            rules.append(
                "wrong-diagnosis: When first attempting to solve a problem → verify "
                "the diagnosis before implementing fixes — check assumptions explicitly."
            )

        return rules

    def format_report(self, report: FrictionReport) -> str:
        """Format a friction report as human-readable text."""
        lines = [
            f"🔍 Session Friction Analysis",
            f"{'─' * 50}",
            f"Sessions scanned: {report.sessions_scanned} (last {report.days_analyzed} days)",
            f"Total friction score: {report.total_friction_score}",
            f"",
        ]

        if report.total_friction_score == 0:
            lines.append("✅ Clean — no significant friction detected")
        else:
            lines.append("📊 Friction by category:")
            for cat, count in sorted(report.category_counts.items(), key=lambda x: -x[1]):
                weight = report.category_weights.get(cat, count)
                pattern = FRICTION_PATTERNS.get(cat, {})
                desc = pattern.get("description", cat)
                lines.append(f"  {cat} ({count}x, weight={weight}): {desc}")

            if report.top_sessions:
                lines.append("")
                lines.append("🔥 Highest-friction sessions:")
                for sid, score in report.top_sessions[:3]:
                    lines.append(f"  {sid[:16]}... score={score}")

        if report.generated_rules:
            lines.append("")
            lines.append("💡 Generated rules to prevent recurrence:")
            for rule in report.generated_rules:
                name, _, desc = rule.partition(":")
                lines.append(f"  • {name.strip()}: {desc.strip()}")

        return "\n".join(lines)

    # ── Session loading ───────────────────────────────────────────────────────

    def _load_sessions(self, days: int) -> List[Dict[str, Any]]:
        """Load session data from DB or JSONL files."""
        sessions = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        if self._db is not None:
            sessions = self._load_from_db(cutoff)
        elif self._sessions_dir is not None:
            sessions = self._load_from_jsonl(cutoff)

        return sessions

    def _load_from_db(self, cutoff: datetime) -> List[Dict[str, Any]]:
        """Load sessions from Hermes SQLite state DB."""
        try:
            cutoff_ts = cutoff.timestamp()
            cursor = self._db.execute(
                """
                SELECT id, started_at, messages
                FROM sessions
                WHERE started_at >= ?
                ORDER BY started_at DESC
                LIMIT 200
                """,
                (cutoff_ts,),
            )
            rows = cursor.fetchall()
            sessions = []
            for row in rows:
                try:
                    messages = json.loads(row[2]) if row[2] else []
                    sessions.append({
                        "id": row[0],
                        "started_at": row[1],
                        "messages": messages,
                    })
                except (json.JSONDecodeError, Exception):
                    continue
            return sessions
        except Exception as e:
            logger.warning(f"FrictionAnalyzer: DB load failed: {e}")
            return []

    def _load_from_jsonl(self, cutoff: datetime) -> List[Dict[str, Any]]:
        """Load sessions from JSONL session files."""
        import os
        import glob

        sessions = []
        pattern = os.path.join(self._sessions_dir, "*.jsonl")

        for filepath in sorted(glob.glob(pattern), reverse=True)[:50]:
            try:
                stat = os.stat(filepath)
                if datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) < cutoff:
                    continue

                messages = []
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                messages.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass

                if messages:
                    sessions.append({
                        "id": os.path.basename(filepath),
                        "started_at": stat.st_mtime,
                        "messages": messages,
                    })
            except Exception:
                continue

        return sessions

    # ── Pattern detection ────────────────────────────────────────────────────

    def _analyze_session(self, session: Dict[str, Any]) -> List[FrictionEvent]:
        """Detect friction events in a single session."""
        events: List[FrictionEvent] = []
        session_id = session.get("id", "unknown")
        messages = session.get("messages", [])
        started_at = session.get("started_at", 0)

        # Extract text content from messages
        texts = []
        for msg in messages:
            content = ""
            if isinstance(msg, dict):
                content = msg.get("content", "") or msg.get("text", "") or ""
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
            texts.append(str(content).lower())

        full_text = " ".join(texts)

        # ── Check each pattern ────────────────────────────────────────────────

        # 1. Error loop detection
        # Skip if this looks like a known noisy context (Docker builds, npm installs, etc.)
        is_noisy_context = any(pat in full_text for pat in NOISY_CONTEXT_PATTERNS)
        error_keywords = FRICTION_PATTERNS["error_loop"]["keywords"]
        error_count = sum(
            1 for t in texts
            if any(kw in t for kw in error_keywords)
        )
        if not is_noisy_context and error_count >= FRICTION_PATTERNS["error_loop"]["threshold"]:
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="error_loop",
                description=f"Error-like messages appeared {error_count} times in session",
                context=f"Error count: {error_count}",
                weight=FRICTION_PATTERNS["error_loop"]["weight"],
            ))

        # 2. API credential failure
        cred_keywords = FRICTION_PATTERNS["api_credential_failure"]["keywords"]
        cred_matches = [kw for kw in cred_keywords if re.search(kw, full_text)]
        if cred_matches:
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="api_credential_failure",
                description="Credential/authentication failure detected",
                context=f"Matched: {cred_matches[:3]}",
                weight=FRICTION_PATTERNS["api_credential_failure"]["weight"],
            ))

        # 3. Infrastructure broken
        infra_keywords = FRICTION_PATTERNS["infrastructure_broken"]["keywords"]
        infra_matches = [kw for kw in infra_keywords if kw in full_text]
        if infra_matches:
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="infrastructure_broken",
                description="Infrastructure/dependency failure detected",
                context=f"Matched: {infra_matches[:3]}",
                weight=FRICTION_PATTERNS["infrastructure_broken"]["weight"],
            ))

        # 4. Memory dropout indicators
        dropout_keywords = FRICTION_PATTERNS["memory_dropout"]["keywords"]
        if any(kw in full_text for kw in dropout_keywords):
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="memory_dropout",
                description="Memory dropout indicator detected",
                context="User indicated agent forgot previously shared info",
                weight=FRICTION_PATTERNS["memory_dropout"]["weight"],
            ))

        # 5. Premature declaration
        declare_keywords = FRICTION_PATTERNS["premature_declaration"]["keywords"]
        declare_count = sum(
            1 for t in texts
            if any(kw in t for kw in declare_keywords)
        )
        if declare_count >= 2:
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="premature_declaration",
                description=f"Completion declared {declare_count} times",
                context=f"May indicate premature success claims",
                weight=FRICTION_PATTERNS["premature_declaration"]["weight"],
            ))

        # 6. Wrong diagnosis
        wrong_diag_keywords = FRICTION_PATTERNS["wrong_diagnosis"]["keywords"]
        if any(kw in full_text for kw in wrong_diag_keywords):
            events.append(FrictionEvent(
                session_id=session_id,
                timestamp=started_at,
                category="wrong_diagnosis",
                description="Fix did not resolve the issue on first attempt",
                context="User indicated problem persisted after fix",
                weight=FRICTION_PATTERNS["wrong_diagnosis"]["weight"],
            ))

        return events
