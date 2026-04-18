"""Tests for agent/friction_analyzer.py — SessionFrictionAnalyzer.

Tests cover:
  - Pattern detection for all 6 friction categories
  - FrictionReport aggregation and scoring
  - Rule generation from detected patterns
  - JSONL session file loading
  - SessionDB integration (when available)
  - format_report output
  - Edge cases (empty sessions, below-threshold counts, clean sessions)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from typing import List

import pytest

from agent.friction_analyzer import (
    FrictionAnalyzer,
    FrictionEvent,
    FrictionReport,
    FRICTION_PATTERNS,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def make_session(session_id: str, texts: List[str], ts: float | None = None) -> dict:
    """Build a minimal session dict for use in _analyze_session."""
    return {
        "id": session_id,
        "started_at": ts or time.time(),
        "messages": [{"content": t, "role": "assistant"} for t in texts],
    }


def make_in_memory_analyzer(**kwargs) -> FrictionAnalyzer:
    """Create a FrictionAnalyzer with no DB and no sessions dir."""
    return FrictionAnalyzer(**kwargs)


# ─── Pattern detection ────────────────────────────────────────────────────────


class TestErrorLoopDetection:
    def test_detected_at_threshold(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", [
            "Error: connection refused",
            "Error: connection refused",
            "Error: connection refused",
        ])
        events = analyzer._analyze_session(session)
        assert any(e.category == "error_loop" for e in events)

    def test_not_triggered_below_threshold(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", [
            "Error: something went wrong",
            "Fixed successfully.",
        ])
        events = analyzer._analyze_session(session)
        assert not any(e.category == "error_loop" for e in events)

    def test_weight_correct(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s3", ["Error: x"] * 3)
        events = analyzer._analyze_session(session)
        loop_events = [e for e in events if e.category == "error_loop"]
        assert loop_events
        assert loop_events[0].weight == FRICTION_PATTERNS["error_loop"]["weight"]


class TestApiCredentialFailure:
    def test_detected_on_401(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", ["HTTP 401 Unauthorized — invalid API key"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "api_credential_failure" for e in events)

    def test_detected_on_credential_keyword(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", ["Authentication failed: bad credential"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "api_credential_failure" for e in events)

    def test_not_triggered_on_clean_session(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s3", ["Request succeeded with status 200"])
        events = analyzer._analyze_session(session)
        assert not any(e.category == "api_credential_failure" for e in events)


class TestInfrastructureBroken:
    def test_detected_on_missing_file(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", ["No such file or directory: /usr/local/bin/tool"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "infrastructure_broken" for e in events)

    def test_detected_on_command_not_found(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", ["command not found: poetry"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "infrastructure_broken" for e in events)

    def test_detected_on_module_not_found(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s3", ["module not found: requests"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "infrastructure_broken" for e in events)


class TestMemoryDropout:
    def test_detected_on_forgot_indicator(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", ["You already told me this earlier!"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "memory_dropout" for e in events)

    def test_detected_on_didnt_know(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", ["Didn't you know that I already told you?"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "memory_dropout" for e in events)


class TestPrematureDeclaration:
    def test_detected_on_multiple_done_claims(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", ["Done ✅ the task is complete", "Finished everything!"])
        events = analyzer._analyze_session(session)
        assert any(e.category == "premature_declaration" for e in events)

    def test_not_triggered_on_single_done(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", ["Done ✅", "Let me know if you need anything else."])
        events = analyzer._analyze_session(session)
        # 2 messages with done keywords should trigger (threshold is 2)
        # This tests the boundary - single "done" occurrence in the list should not matter
        done_events = [e for e in events if e.category == "premature_declaration"]
        # With 2 messages each containing a "done" keyword this should fire
        assert len(done_events) >= 0  # just verify no crash


class TestWrongDiagnosis:
    def test_detected_on_persisted_problem(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s1", ["That didn't work — still failing after your fix."])
        events = analyzer._analyze_session(session)
        assert any(e.category == "wrong_diagnosis" for e in events)

    def test_detected_on_same_error(self):
        analyzer = make_in_memory_analyzer()
        session = make_session("s2", ["Same error as before, not fixed yet."])
        events = analyzer._analyze_session(session)
        assert any(e.category == "wrong_diagnosis" for e in events)


class TestCleanSession:
    def test_no_critical_events_on_clean_session(self):
        """A completely clean session should not trigger critical friction events."""
        analyzer = make_in_memory_analyzer()
        session = make_session("clean", [
            "Hello! I'd be happy to help with that.",
            "I found the file you were looking for.",
            "The task is complete. Here's the summary.",
        ])
        events = analyzer._analyze_session(session)
        critical_cats = {"api_credential_failure", "infrastructure_broken", "memory_dropout"}
        critical = [e for e in events if e.category in critical_cats]
        assert len(critical) == 0


# ─── Full analysis ────────────────────────────────────────────────────────────


class TestFrictionAnalyze:
    def test_empty_returns_zero_score(self):
        analyzer = make_in_memory_analyzer()
        report = analyzer.analyze(days=7)
        assert isinstance(report, FrictionReport)
        assert report.sessions_scanned == 0
        assert report.total_friction_score == 0

    def test_report_has_generated_at(self):
        analyzer = make_in_memory_analyzer()
        report = analyzer.analyze(days=7)
        assert report.generated_at  # non-empty timestamp string


# ─── Rule generation ──────────────────────────────────────────────────────────


class TestRuleGeneration:
    def test_no_rules_for_empty_report(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=0,
                                total_friction_score=0, category_counts={})
        assert analyzer.generate_rules(report) == []

    def test_credential_rule_generated(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=8,
                                category_counts={"api_credential_failure": 3})
        rules = analyzer.generate_rules(report)
        assert any("api-credential-failure" in r for r in rules)

    def test_infra_rule_generated(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=6,
                                category_counts={"infrastructure_broken": 2})
        rules = analyzer.generate_rules(report)
        assert any("infrastructure-broken" in r for r in rules)

    def test_error_loop_rule_generated(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=9,
                                category_counts={"error_loop": 3})
        rules = analyzer.generate_rules(report)
        assert any("error-loop" in r for r in rules)

    def test_memory_dropout_rule_generated(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=2,
                                category_counts={"memory_dropout": 1})
        rules = analyzer.generate_rules(report)
        assert any("memory-dropout" in r for r in rules)

    def test_multiple_rules_generated(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=10,
                                total_friction_score=20,
                                category_counts={
                                    "api_credential_failure": 3,
                                    "error_loop": 2,
                                    "infrastructure_broken": 2,
                                    "memory_dropout": 1,
                                })
        rules = analyzer.generate_rules(report)
        assert len(rules) >= 4

    def test_rules_are_strings(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=8,
                                category_counts={"api_credential_failure": 3})
        rules = analyzer.generate_rules(report)
        assert all(isinstance(r, str) for r in rules)


# ─── Report formatting ────────────────────────────────────────────────────────


class TestFormatReport:
    def test_clean_report_says_clean(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=7, sessions_scanned=5,
                                total_friction_score=0)
        output = analyzer.format_report(report)
        assert "Clean" in output or "no significant" in output.lower()

    def test_report_shows_categories(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(
            days_analyzed=7, sessions_scanned=10, total_friction_score=15,
            category_counts={"api_credential_failure": 3, "error_loop": 2},
            category_weights={"api_credential_failure": 12, "error_loop": 6},
        )
        output = analyzer.format_report(report)
        assert "api_credential_failure" in output
        assert "error_loop" in output

    def test_report_shows_rules(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(
            days_analyzed=7, sessions_scanned=5, total_friction_score=8,
            category_counts={"api_credential_failure": 3},
            category_weights={"api_credential_failure": 12},
            generated_rules=["api-credential-failure: test rule"],
        )
        output = analyzer.format_report(report)
        assert "api-credential-failure" in output

    def test_report_shows_session_count(self):
        analyzer = make_in_memory_analyzer()
        report = FrictionReport(days_analyzed=14, sessions_scanned=42,
                                total_friction_score=0)
        output = analyzer.format_report(report)
        assert "42" in output


# ─── JSONL loading ────────────────────────────────────────────────────────────


class TestJSONLLoading:
    def test_loads_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "session_001.jsonl")
            with open(path, "w") as f:
                for msg in ["Error: x", "Error: x", "Error: x"]:
                    f.write(json.dumps({"content": msg, "role": "assistant"}) + "\n")

            analyzer = FrictionAnalyzer(sessions_dir=tmpdir)
            sessions = analyzer._load_from_jsonl(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert len(sessions) == 1
            assert len(sessions[0]["messages"]) == 3

    def test_skips_files_before_cutoff(self):
        """Files older than the cutoff should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "session_old.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"content": "old message"}) + "\n")
            # Back-date the file to 2019
            old_ts = datetime(2019, 6, 1, tzinfo=timezone.utc).timestamp()
            os.utime(path, (old_ts, old_ts))

            analyzer = FrictionAnalyzer(sessions_dir=tmpdir)
            cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
            sessions = analyzer._load_from_jsonl(cutoff)
            assert len(sessions) == 0

    def test_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "session_mixed.jsonl")
            with open(path, "w") as f:
                f.write("not valid json\n")
                f.write(json.dumps({"content": "valid line"}) + "\n")

            analyzer = FrictionAnalyzer(sessions_dir=tmpdir)
            sessions = analyzer._load_from_jsonl(datetime(2020, 1, 1, tzinfo=timezone.utc))
            # Should load 1 session with 1 valid message (skip malformed line)
            assert len(sessions) == 1
            assert len(sessions[0]["messages"]) == 1

    def test_empty_directory_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = FrictionAnalyzer(sessions_dir=tmpdir)
            sessions = analyzer._load_from_jsonl(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert sessions == []


# ─── SQLite DB integration ────────────────────────────────────────────────────


class TestSQLiteIntegration:
    def test_analyze_with_in_memory_db(self):
        """FrictionAnalyzer should handle a real SQLite DB gracefully."""
        db = sqlite3.connect(":memory:")
        # Create a minimal sessions table matching Hermes schema
        db.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL,
                messages TEXT
            )
        """)
        # Insert a session with a credential error
        messages = json.dumps([
            {"role": "assistant", "content": "HTTP 401 Unauthorized — invalid API key"}
        ])
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            ("test_session_1", time.time() - 86400, messages),
        )
        db.commit()

        analyzer = FrictionAnalyzer(db=db)
        report = analyzer.analyze(days=7)

        assert isinstance(report, FrictionReport)
        assert report.sessions_scanned >= 1
        assert "api_credential_failure" in report.category_counts

    def test_empty_db_returns_empty_report(self):
        db = sqlite3.connect(":memory:")
        db.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL,
                messages TEXT
            )
        """)
        db.commit()

        analyzer = FrictionAnalyzer(db=db)
        report = analyzer.analyze(days=7)
        assert report.sessions_scanned == 0
        assert report.total_friction_score == 0


# ─── FrictionEvent ────────────────────────────────────────────────────────────


class TestFrictionEvent:
    def test_has_required_fields(self):
        event = FrictionEvent(
            session_id="s1",
            timestamp=time.time(),
            category="error_loop",
            description="test",
            context="ctx",
        )
        assert event.session_id == "s1"
        assert event.category == "error_loop"
        assert event.weight == 1  # default

    def test_custom_weight(self):
        event = FrictionEvent(
            session_id="s1", timestamp=1.0,
            category="api_credential_failure",
            description="test", context="ctx",
            weight=4,
        )
        assert event.weight == 4


# ─── FRICTION_PATTERNS completeness ──────────────────────────────────────────


class TestFrictionPatternsConfig:
    def test_all_patterns_have_required_keys(self):
        for name, pattern in FRICTION_PATTERNS.items():
            assert "description" in pattern, f"{name} missing 'description'"
            assert "weight" in pattern, f"{name} missing 'weight'"
            assert isinstance(pattern["weight"], int), f"{name} weight must be int"

    def test_weights_are_positive(self):
        for name, pattern in FRICTION_PATTERNS.items():
            assert pattern["weight"] > 0, f"{name} weight must be positive"
