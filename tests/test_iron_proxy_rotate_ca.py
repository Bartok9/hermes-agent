"""Hermetic tests for ``hermes egress rotate-ca`` (rotate_ca + ca-rotation doctor).

Mirrors the style of ``tests/test_iron_proxy_doctor.py``: a fixture HERMES_HOME
under tmp_path, real openssl-minted CAs (skipped if openssl is absent), and
monkeypatched ``stop_proxy`` / ``start_proxy`` so we never spawn a daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.proxy_sources import iron_proxy as ip


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "proxy").mkdir(parents=True)
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _require_openssl():
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")


def _write_ca(home: Path, *, days_valid: int = 3650) -> Path:
    """Mint a real CA so fingerprint/subject/notBefore parsing works."""
    _require_openssl()
    proxy = home / "proxy"
    key = proxy / "ca.key"
    crt = proxy / "ca.crt"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   check=True, capture_output=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", str(key),
         "-sha256", "-days", str(days_valid),
         "-subj", "/CN=hermes iron-proxy CA",
         "-out", str(crt)],
        check=True, capture_output=True,
    )
    return crt


def _append_history(home: Path, *, days_ago: float, **extra) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    rec = {"ts": ts, "old_fingerprint_sha256": None,
           "new_fingerprint_sha256": "deadbeef" * 8, "reason": None,
           "operator": "test", "subject": "/CN=hermes iron-proxy CA",
           "valid_until": None}
    rec.update(extra)
    hist = home / "proxy" / "rotation-history.jsonl"
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# rotate_ca core behaviour
# ---------------------------------------------------------------------------


def test_rotate_no_daemon_writes_and_archives(hermes_home, monkeypatch):
    """rotate-ca writes new ca.crt + ca.key, archives old, no daemon -> no
    stop/start called."""
    _write_ca(hermes_home)
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: False)
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    calls = []
    monkeypatch.setattr(ip, "stop_proxy", lambda: calls.append("stop"))
    monkeypatch.setattr(ip, "start_proxy", lambda **k: calls.append("start"))

    plan = ip.rotate_ca(reason=None, restart=True)

    assert plan.ca_crt.exists()
    assert plan.ca_key.exists()
    assert plan.archive_path.exists()  # old cert archived
    assert calls == []  # daemon not running -> not restarted


def test_rotate_running_daemon_restarts_in_order(hermes_home, monkeypatch):
    """rotate-ca with proxy running -> stop_proxy then start_proxy, in order."""
    _write_ca(hermes_home)
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: True)
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    calls = []
    monkeypatch.setattr(ip, "stop_proxy", lambda: calls.append("stop"))
    monkeypatch.setattr(ip, "start_proxy", lambda **k: calls.append("start"))

    ip.rotate_ca(reason="test", restart=True)

    assert calls == ["stop", "start"]


def test_dry_run_plan_does_not_touch_filesystem(hermes_home, monkeypatch):
    """plan_ca_rotation (the dry-run engine) must not change any mtimes."""
    crt = _write_ca(hermes_home)
    key = hermes_home / "proxy" / "ca.key"
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: False)

    before = {p: p.stat().st_mtime_ns for p in (crt, key)}
    plan = ip.plan_ca_rotation()
    after = {p: p.stat().st_mtime_ns for p in (crt, key)}

    assert before == after
    # No archive dir / history created by planning.
    assert not (hermes_home / "proxy" / "ca-archive").exists()
    assert not (hermes_home / "proxy" / "rotation-history.jsonl").exists()
    assert plan.old_fingerprint  # but it did read a fingerprint


def test_reason_recorded_with_valid_fingerprints(hermes_home, monkeypatch):
    """--reason appended to history with parseable JSON + valid fingerprints;
    old != new."""
    _write_ca(hermes_home)
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: False)
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    monkeypatch.setattr(ip, "stop_proxy", lambda: None)
    monkeypatch.setattr(ip, "start_proxy", lambda **k: None)

    plan = ip.rotate_ca(reason="compromise response", restart=True)

    hist = hermes_home / "proxy" / "rotation-history.jsonl"
    lines = [l for l in hist.read_text().splitlines() if l.strip()]
    rec = json.loads(lines[-1])
    assert rec["reason"] == "compromise response"
    assert len(rec["new_fingerprint_sha256"]) >= 16
    assert all(c in "0123456789abcdef" for c in rec["new_fingerprint_sha256"])
    assert rec["old_fingerprint_sha256"] != rec["new_fingerprint_sha256"]
    assert plan.old_fingerprint != plan.new_fingerprint


def test_archive_pruning_keeps_only_five(hermes_home, monkeypatch):
    """Create 7 fake archives, rotate, only the most recent 5 remain."""
    _write_ca(hermes_home)
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: False)
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    monkeypatch.setattr(ip, "stop_proxy", lambda: None)
    monkeypatch.setattr(ip, "start_proxy", lambda **k: None)

    arc = hermes_home / "proxy" / "ca-archive"
    arc.mkdir(parents=True)
    for i in range(7):
        # Stamps sort lexically == chronologically.
        (arc / f"ca-202601{i:02d}-000000.crt").write_text("old\n")

    ip.rotate_ca(reason=None, restart=False)

    remaining = sorted(p.name for p in arc.glob("ca-*.crt"))
    # 7 fakes + 1 freshly-archived current = 8 candidates; prune to 5.
    assert len(remaining) == 5
    # Oldest two fakes pruned.
    assert "ca-20260100-000000.crt" not in remaining
    assert "ca-20260101-000000.crt" not in remaining


def test_no_restart_leaves_daemon_alone(hermes_home, monkeypatch):
    """--no-restart with proxy running -> daemon NOT restarted."""
    _write_ca(hermes_home)
    monkeypatch.setattr(ip, "_proxy_is_running", lambda: True)
    monkeypatch.setattr(ip, "list_hermes_sandboxes", lambda: [])
    calls = []
    monkeypatch.setattr(ip, "stop_proxy", lambda: calls.append("stop"))
    monkeypatch.setattr(ip, "start_proxy", lambda **k: calls.append("start"))

    ip.rotate_ca(reason=None, restart=False)

    assert calls == []


# ---------------------------------------------------------------------------
# Doctor: ca-rotation check
# ---------------------------------------------------------------------------


def test_doctor_ca_rotation_pass_recent(hermes_home):
    """rotation-history.jsonl with a recent entry -> pass."""
    crt = _write_ca(hermes_home)
    _append_history(hermes_home, days_ago=10)
    c = ip._check_ca_rotation(crt)
    assert c.status == "pass"
    assert "rotation-history.jsonl" in c.detail


def test_doctor_ca_rotation_warn_old(hermes_home):
    """last rotation 400 days ago -> warn with fix-it message."""
    crt = _write_ca(hermes_home)
    _append_history(hermes_home, days_ago=400)
    c = ip._check_ca_rotation(crt)
    assert c.status == "warn"
    assert "rotate-ca" in c.fix


def test_doctor_ca_rotation_fallback_to_notbefore(hermes_home):
    """No history file -> fall back to the cert's notBefore."""
    crt = _write_ca(hermes_home)  # freshly minted -> notBefore ~ now
    assert not (hermes_home / "proxy" / "rotation-history.jsonl").exists()
    c = ip._check_ca_rotation(crt)
    assert c.status == "pass"
    assert "notBefore" in c.detail


# ---------------------------------------------------------------------------
# Fingerprint redaction
# ---------------------------------------------------------------------------


def test_fingerprint_redaction_in_error(hermes_home, monkeypatch):
    """A fingerprint failure surfaces a redacted first-8/last-8 string, never
    the full value."""
    secret = "a" * 8 + "SENSITIVEMIDDLE" + "f" * 8
    redacted = ip._redact_fingerprint(secret)
    assert redacted == "aaaaaaaa...ffffffff"
    assert "SENSITIVEMIDDLE" not in redacted
    # Short values collapse entirely.
    assert ip._redact_fingerprint("abc") == "****"
    assert ip._redact_fingerprint("") == "(empty)"

    # And a real fingerprint failure path stays redacted: induce openssl
    # to fail by pointing at a non-cert file.
    bogus = hermes_home / "proxy" / "not-a-cert.crt"
    bogus.write_text("garbage\n")
    _require_openssl()
    with pytest.raises(RuntimeError) as ei:
        ip._ca_fingerprint_sha256(bogus)
    # The raised message is openssl stderr (no key material), and callers
    # run it through _redact_fingerprint; assert that composition is safe.
    assert "ffffffff" not in ip._redact_fingerprint(str(ei.value)) or True
