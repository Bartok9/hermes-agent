"""Hermetic tests for ``hermes egress audit`` (iter/aggregate/anomaly +
CLI handlers).  No network, no real binary.  Mirrors the existing test style.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.proxy_sources import iron_proxy as ip
from hermes_cli import proxy_cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "proxy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_audit(home: Path, lines) -> Path:
    p = home / "proxy" / "audit.log"
    with p.open("w", encoding="utf-8") as fh:
        for ln in lines:
            if isinstance(ln, dict):
                fh.write(json.dumps(ln) + "\n")
            else:
                fh.write(ln + "\n")
    return p


def _audit_ns(**overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        n=50, follow=False, as_json=False, since=None, pattern=None,
        fmt="json", out=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# iter_audit_log + tail
# ---------------------------------------------------------------------------


def test_iter_audit_log_parses_and_marks_unparsed(hermes_home):
    now = datetime.now(timezone.utc)
    p = _write_audit(hermes_home, [
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200},
        "this is not json",
    ])
    events = list(ip.iter_audit_log(p))
    assert len(events) == 2
    assert events[0]["upstream_host"] == "api.openai.com"
    assert events[0]["_ts"] is not None
    assert events[1]["_unparsed"] is True
    assert events[1]["_raw"] == "this is not json"


def test_iter_audit_log_missing_file_yields_nothing(hermes_home):
    assert list(ip.iter_audit_log(hermes_home / "proxy" / "nope.log")) == []


def test_cmd_audit_tail_last_n(hermes_home, capsys):
    now = datetime.now(timezone.utc)
    _write_audit(hermes_home, [
        {"ts": _iso(now), "upstream_host": f"h{i}.test", "status": 200,
         "method": "GET", "path": "/"}
        for i in range(10)
    ])
    rc = proxy_cli.cmd_audit_tail(_audit_ns(n=3, as_json=True))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    # Last 3 of 10 -> h7, h8, h9
    assert "h9.test" in out[-1]
    assert "h7.test" in out[0]


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_cmd_audit_grep_regex_and_since(hermes_home, capsys):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=5)
    _write_audit(hermes_home, [
        {"ts": _iso(old), "upstream_host": "api.openai.com", "status": 200},
        {"ts": _iso(now), "upstream_host": "api.anthropic.com", "status": 403},
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200},
    ])
    # Pattern matches openai; since=1h drops the 5h-old one.
    rc = proxy_cli.cmd_audit_grep(
        _audit_ns(pattern="openai", since="1h", as_json=True))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "api.openai.com" in out[0]


def test_cmd_audit_grep_invalid_since_exits_2(hermes_home, capsys):
    _write_audit(hermes_home, [{"ts": "2026-01-01T00:00:00Z",
                                "upstream_host": "x.test", "status": 200}])
    rc = proxy_cli.cmd_audit_grep(
        _audit_ns(pattern=".*", since="not-a-time"))
    assert rc == 2


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_aggregate_audit_stats_distribution(hermes_home):
    now = datetime.now(timezone.utc)
    lines = [
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200,
         "sandbox_id": "s1"},
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200,
         "sandbox_id": "s1"},
        {"ts": _iso(now), "upstream_host": "evil.test", "status": 403,
         "sandbox_id": "s2"},
    ]
    p = _write_audit(hermes_home, lines)
    stats = ip.aggregate_audit_stats(ip.iter_audit_log(p))
    assert stats["by_status"] == {200: 2, 403: 1}
    assert stats["denied"] == 1
    top = dict(stats["top_hosts"])
    assert top["api.openai.com"] == 2
    assert top["evil.test"] == 1


def test_detect_anomalies_first_time_host():
    base = [{"upstream_host": "api.openai.com", "_unparsed": False}]
    window = [
        {"upstream_host": "api.openai.com", "_unparsed": False, "status": 200},
        {"upstream_host": "newhost.test", "_unparsed": False, "status": 200},
    ]
    res = ip.detect_audit_anomalies(window, baseline=base)
    assert res["first_time_hosts"] == ["newhost.test"]


def test_cmd_audit_stats_invalid_since_exits_2(hermes_home, capsys):
    _write_audit(hermes_home, [{"ts": "2026-01-01T00:00:00Z",
                                "upstream_host": "x.test", "status": 200}])
    rc = proxy_cli.cmd_audit_stats(_audit_ns(since="garbage"))
    assert rc == 2


def test_cmd_audit_stats_json(hermes_home, capsys):
    now = datetime.now(timezone.utc)
    _write_audit(hermes_home, [
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200},
    ])
    rc = proxy_cli.cmd_audit_stats(_audit_ns(as_json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "stats" in payload and "anomalies" in payload
    assert payload["stats"]["by_status"]["200"] == 1


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_cmd_audit_export_json_stdout(hermes_home, capsys):
    now = datetime.now(timezone.utc)
    _write_audit(hermes_home, [
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200},
    ])
    rc = proxy_cli.cmd_audit_export(_audit_ns(fmt="json"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["upstream_host"] == "api.openai.com"
    # Synthetic fields stripped.
    assert "_raw" not in data[0] and "_ts" not in data[0]


def test_cmd_audit_export_csv_to_file(hermes_home, tmp_path):
    now = datetime.now(timezone.utc)
    _write_audit(hermes_home, [
        {"ts": _iso(now), "upstream_host": "api.openai.com", "status": 200},
        {"ts": _iso(now), "upstream_host": "evil.test", "status": 403},
    ])
    out = tmp_path / "audit.csv"
    rc = proxy_cli.cmd_audit_export(_audit_ns(fmt="csv", out=str(out)))
    assert rc == 0
    rows = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 2
    assert rows[0]["upstream_host"] == "api.openai.com"
    assert rows[1]["status"] == "403"
