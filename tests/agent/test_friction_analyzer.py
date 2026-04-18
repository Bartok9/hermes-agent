"""Tests for agent/friction_analyzer.py."""
import json, os, sqlite3, tempfile, time
from datetime import datetime, timezone
import pytest
from agent.friction_analyzer import FrictionAnalyzer, FrictionReport, FRICTION_PATTERNS

def sess(sid, texts, ts=None):
    return {"id": sid, "started_at": ts or time.time(),
            "messages": [{"content": t, "role": "assistant"} for t in texts]}

def test_error_loop_detected():
    a = FrictionAnalyzer()
    evs = a._analyze_session(sess("s1", ["Error: x"]*3))
    assert any(e.category == "error_loop" for e in evs)

def test_error_loop_below_threshold_not_triggered():
    a = FrictionAnalyzer()
    evs = a._analyze_session(sess("s2", ["Error: x", "Done."]))
    assert not any(e.category == "error_loop" for e in evs)

def test_api_credential_failure_detected():
    evs = FrictionAnalyzer()._analyze_session(sess("s3", ["HTTP 401 invalid API key"]))
    assert any(e.category == "api_credential_failure" for e in evs)

def test_infrastructure_broken_detected():
    evs = FrictionAnalyzer()._analyze_session(sess("s4", ["No such file or directory: /bin/tool"]))
    assert any(e.category == "infrastructure_broken" for e in evs)

def test_memory_dropout_detected():
    evs = FrictionAnalyzer()._analyze_session(sess("s5", ["you already told me that"]))
    assert any(e.category == "memory_dropout" for e in evs)

def test_wrong_diagnosis_detected():
    evs = FrictionAnalyzer()._analyze_session(sess("s6", ["that did not work, still failing"]))
    assert any(e.category == "wrong_diagnosis" for e in evs)

def test_clean_session_no_critical_events():
    evs = FrictionAnalyzer()._analyze_session(sess("s7", ["Hello!", "Task complete.", "Welcome!"]))
    crit = {e.category for e in evs} & {"api_credential_failure", "infrastructure_broken", "memory_dropout"}
    assert not crit

def test_analyze_empty_returns_zero():
    r = FrictionAnalyzer().analyze(days=7)
    assert isinstance(r, FrictionReport)
    assert r.total_friction_score == 0
    assert r.sessions_scanned == 0

def test_rule_generated_for_credential_failure():
    r = FrictionReport(days_analyzed=7, sessions_scanned=5, total_friction_score=8,
                       category_counts={"api_credential_failure": 3})
    rules = FrictionAnalyzer().generate_rules(r)
    assert any("api-credential-failure" in rule for rule in rules)

def test_rule_generated_for_infra_broken():
    r = FrictionReport(days_analyzed=7, sessions_scanned=5, total_friction_score=6,
                       category_counts={"infrastructure_broken": 2})
    rules = FrictionAnalyzer().generate_rules(r)
    assert any("infrastructure-broken" in rule for rule in rules)

def test_no_rules_for_clean_report():
    r = FrictionReport(days_analyzed=7, sessions_scanned=5,
                       total_friction_score=0, category_counts={})
    assert FrictionAnalyzer().generate_rules(r) == []

def test_format_report_clean():
    r = FrictionReport(days_analyzed=7, sessions_scanned=5, total_friction_score=0)
    out = FrictionAnalyzer().format_report(r)
    assert "Clean" in out or "no significant" in out.lower()

def test_format_report_shows_categories():
    r = FrictionReport(days_analyzed=7, sessions_scanned=5, total_friction_score=10,
                       category_counts={"api_credential_failure": 2},
                       category_weights={"api_credential_failure": 8})
    assert "api_credential_failure" in FrictionAnalyzer().format_report(r)

def test_jsonl_loading():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s1.jsonl")
        with open(p, "w") as f:
            for _ in range(3):
                f.write(json.dumps({"content": "Error x", "role": "assistant"}) + "\n")
        sessions = FrictionAnalyzer(sessions_dir=d)._load_from_jsonl(
            datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(sessions) == 1 and len(sessions[0]["messages"]) == 3

def test_jsonl_empty_directory():
    with tempfile.TemporaryDirectory() as d:
        sessions = FrictionAnalyzer(sessions_dir=d)._load_from_jsonl(
            datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert sessions == []

def test_sqlite_integration():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, messages TEXT)")
    db.execute("INSERT INTO sessions VALUES (?,?,?)",
               ("s1", time.time()-3600,
                json.dumps([{"role": "assistant", "content": "HTTP 401 invalid API key"}])))
    db.commit()
    r = FrictionAnalyzer(db=db).analyze(days=7)
    assert r.sessions_scanned >= 1 and "api_credential_failure" in r.category_counts

def test_all_patterns_have_description_and_weight():
    for name, p in FRICTION_PATTERNS.items():
        assert "description" in p and "weight" in p, f"{name} missing required keys"

def test_pattern_weights_are_positive():
    assert all(p["weight"] > 0 for p in FRICTION_PATTERNS.values())

def test_error_loop_suppressed_in_noisy_context():
    """Docker builds and npm installs should not trigger error_loop false positives."""
    from agent.friction_analyzer import NOISY_CONTEXT_PATTERNS
    a = FrictionAnalyzer()
    # Simulate a docker build session with expected error output
    session = sess("docker", [
        "docker build -t myapp .",
        "Step 3/12: RUN npm install",
        "Error: peer dependency conflict",
        "Error: peer dependency conflict",
        "Error: peer dependency conflict",
        "Build complete.",
    ])
    evs = a._analyze_session(session)
    # Should NOT fire error_loop because "docker build" / "npm install" are noisy context
    assert not any(e.category == "error_loop" for e in evs)

def test_error_loop_still_fires_outside_noisy_context():
    """Real error loops outside build contexts should still be detected."""
    a = FrictionAnalyzer()
    session = sess("realerr", [
        "Calling external API...",
        "Error: connection refused",
        "Error: connection refused",
        "Error: connection refused",
    ])
    evs = a._analyze_session(session)
    assert any(e.category == "error_loop" for e in evs)

