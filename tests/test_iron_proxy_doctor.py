"""Hermetic tests for ``hermes egress doctor`` (run_doctor + per-check helpers).

Each check is exercised with monkeypatched failure modes so we never touch
the network or a real iron-proxy binary.  One integration test runs the whole
``run_doctor(network=False)`` against a fully-wired fixture home and asserts
the exit-code semantics + JSON shape.  Mirrors the style of
``tests/test_iron_proxy.py``.
"""

from __future__ import annotations

import json
import os
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
    for key in list(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    return home


def _write_ca(home: Path, *, days_valid: int = 3650) -> Path:
    """Generate a real CA cert via openssl so _check_ca's openssl parse works."""
    import subprocess
    import shutil
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    proxy = home / "proxy"
    key = proxy / "ca.key"
    crt = proxy / "ca.crt"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   check=True, capture_output=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", str(key),
         "-sha256", "-days", str(days_valid), "-subj", "/CN=test CA",
         "-out", str(crt)],
        check=True, capture_output=True,
    )
    return crt


def _write_config(home: Path, *, with_imds: bool = True,
                  domains=None) -> Path:
    import yaml
    domains = domains or ["api.openai.com", "openrouter.ai"]
    deny = list(ip._DEFAULT_UPSTREAM_DENY_CIDRS) if with_imds else ["10.0.0.0/8"]
    cfg = {
        "proxy": {"http_listen": "127.0.0.1:9090", "upstream_deny_cidrs": deny},
        "transforms": [
            {"name": "allowlist", "config": {"domains": domains}},
            {"name": "secrets", "config": {"secrets": []}},
        ],
    }
    out = home / "proxy" / "proxy.yaml"
    out.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return out


def _write_mappings(home: Path, env_name="OPENROUTER_API_KEY",
                    hosts=("openrouter.ai",)) -> Path:
    return ip.write_mappings([ip.TokenMapping(
        proxy_token=ip.mint_proxy_token("t"),
        real_env_name=env_name,
        upstream_hosts=tuple(hosts),
    )])


def _fake_binary(home: Path) -> Path:
    b = home / "bin" / ip._platform_binary_name()
    b.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    b.chmod(0o755)
    return b


# ---------------------------------------------------------------------------
# Check 1: binary
# ---------------------------------------------------------------------------


def test_check_binary_missing(hermes_home, monkeypatch):
    monkeypatch.setattr(ip.shutil, "which", lambda _n: None)
    c = ip._check_binary(hermes_home / "bin")
    assert c.status == "fail"
    assert "not found" in c.detail
    assert c.fix


def test_check_binary_ok(hermes_home, monkeypatch):
    _fake_binary(hermes_home)
    monkeypatch.setattr(ip, "iron_proxy_version",
                        lambda b: f"iron-proxy v{ip._IRON_PROXY_VERSION}")
    c = ip._check_binary(hermes_home / "bin")
    assert c.status == "pass"


def test_check_binary_version_mismatch_warns(hermes_home, monkeypatch):
    _fake_binary(hermes_home)
    monkeypatch.setattr(ip, "iron_proxy_version", lambda b: "iron-proxy v0.1.0")
    c = ip._check_binary(hermes_home / "bin")
    assert c.status == "warn"
    assert "install --force" in c.fix


# ---------------------------------------------------------------------------
# Check 2: ca
# ---------------------------------------------------------------------------


def test_check_ca_missing(hermes_home):
    c = ip._check_ca(hermes_home / "proxy" / "ca.crt")
    assert c.status == "fail"


def test_check_ca_not_pem(hermes_home):
    crt = hermes_home / "proxy" / "ca.crt"
    crt.write_text("not a cert", encoding="utf-8")
    c = ip._check_ca(crt)
    assert c.status == "fail"
    assert "not valid PEM" in c.detail


def test_check_ca_valid(hermes_home):
    crt = _write_ca(hermes_home, days_valid=3650)
    c = ip._check_ca(crt)
    assert c.status == "pass"


def test_check_ca_near_expiry_warns(hermes_home, monkeypatch):
    crt = _write_ca(hermes_home, days_valid=3650)
    # Force notAfter to 100 days out -> warn tier (<365, >=30).
    soon = datetime.now(timezone.utc) + timedelta(days=100)
    monkeypatch.setattr(ip, "_ca_not_after", lambda p: soon)
    c = ip._check_ca(crt)
    assert c.status == "warn"


def test_check_ca_expired_fails(hermes_home, monkeypatch):
    crt = _write_ca(hermes_home, days_valid=3650)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(ip, "_ca_not_after", lambda p: past)
    c = ip._check_ca(crt)
    assert c.status == "fail"
    assert "expired" in c.detail


# ---------------------------------------------------------------------------
# Check 3: config
# ---------------------------------------------------------------------------


def test_check_config_missing(hermes_home):
    c = ip._check_config(hermes_home / "proxy" / "proxy.yaml")
    assert c.status == "fail"


def test_check_config_bad_yaml(hermes_home):
    p = hermes_home / "proxy" / "proxy.yaml"
    p.write_text("key: [unterminated\n", encoding="utf-8")
    c = ip._check_config(p)
    assert c.status == "fail"


def test_check_config_ok(hermes_home):
    p = _write_config(hermes_home)
    c = ip._check_config(p)
    assert c.status == "pass"


# ---------------------------------------------------------------------------
# Check 4: mappings
# ---------------------------------------------------------------------------


def test_check_mappings_missing(hermes_home):
    c = ip._check_mappings(hermes_home / "proxy", env_names=set())
    assert c.status == "fail"


def test_check_mappings_env_missing_warns(hermes_home):
    _write_mappings(hermes_home, env_name="OPENROUTER_API_KEY")
    c = ip._check_mappings(hermes_home / "proxy", env_names=set())
    assert c.status == "warn"
    assert "OPENROUTER_API_KEY" in c.detail


def test_check_mappings_ok(hermes_home):
    _write_mappings(hermes_home, env_name="OPENROUTER_API_KEY")
    c = ip._check_mappings(hermes_home / "proxy",
                           env_names={"OPENROUTER_API_KEY"})
    assert c.status == "pass"


# ---------------------------------------------------------------------------
# Check 5: daemon
# ---------------------------------------------------------------------------


def test_check_daemon_no_pidfile(hermes_home, monkeypatch):
    monkeypatch.setattr(ip, "_read_pid", lambda: None)
    c = ip._check_daemon()
    assert c.status == "fail"


def test_check_daemon_dead_pid(hermes_home, monkeypatch):
    monkeypatch.setattr(ip, "_read_pid", lambda: 4242)
    monkeypatch.setattr(ip, "_pid_alive", lambda pid: False)
    c = ip._check_daemon()
    assert c.status == "fail"
    assert "not alive" in c.detail


def test_check_daemon_alive(hermes_home, monkeypatch):
    monkeypatch.setattr(ip, "_read_pid", lambda: 4242)
    monkeypatch.setattr(ip, "_pid_alive", lambda pid: True)
    c = ip._check_daemon()
    assert c.status == "pass"


# ---------------------------------------------------------------------------
# Check 6: listening
# ---------------------------------------------------------------------------


def test_check_listening_closed(hermes_home, monkeypatch):
    import socket

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(socket, "create_connection", boom)
    c = ip._check_listening(9090)
    assert c.status == "fail"


def test_check_listening_open(hermes_home, monkeypatch):
    import socket

    class _Dummy:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Dummy())
    monkeypatch.setattr(ip.shutil, "which", lambda _n: None)  # skip lsof
    c = ip._check_listening(9090)
    assert c.status == "pass"


# ---------------------------------------------------------------------------
# Check 7: reachability
# ---------------------------------------------------------------------------


def test_check_reachability_all_ok(hermes_home, monkeypatch):
    _write_config(hermes_home, domains=["api.openai.com", "openrouter.ai"])
    monkeypatch.setattr(ip, "_https_head_via_proxy",
                        lambda host, **k: (401, None))
    c = ip._check_reachability(hermes_home / "proxy" / "proxy.yaml", 9090)
    assert c.status == "pass"


def test_check_reachability_unreachable_fails(hermes_home, monkeypatch):
    _write_config(hermes_home, domains=["api.openai.com"])
    monkeypatch.setattr(ip, "_https_head_via_proxy",
                        lambda host, **k: (None, "connection refused"))
    c = ip._check_reachability(hermes_home / "proxy" / "proxy.yaml", 9090)
    assert c.status == "fail"
    assert "unreachable" in c.detail


# ---------------------------------------------------------------------------
# Check 8: token-swap
# ---------------------------------------------------------------------------


def test_check_token_swap_403_is_broken(hermes_home, monkeypatch):
    _write_mappings(hermes_home, env_name="OPENROUTER_API_KEY",
                    hosts=("openrouter.ai",))
    monkeypatch.setattr(ip, "_https_head_via_proxy",
                        lambda host, **k: (403, None))
    c = ip._check_token_swap(hermes_home / "proxy", 9090)
    assert c.status == "fail"
    assert "403" in c.detail
    # The proxy token must NOT appear in full in the failure detail.
    mappings = ip.load_mappings()
    assert mappings[0].proxy_token not in c.detail


def test_check_token_swap_401_means_swap_fired(hermes_home, monkeypatch):
    _write_mappings(hermes_home, env_name="OPENROUTER_API_KEY",
                    hosts=("openrouter.ai",))
    monkeypatch.setattr(ip, "_https_head_via_proxy",
                        lambda host, **k: (401, None))
    c = ip._check_token_swap(hermes_home / "proxy", 9090)
    assert c.status == "pass"


def test_check_token_swap_no_bearer_skips(hermes_home):
    # No mappings written -> skip.
    c = ip._check_token_swap(hermes_home / "proxy", 9090)
    assert c.status == "skip"


# ---------------------------------------------------------------------------
# Check 9: uncovered
# ---------------------------------------------------------------------------


def test_check_uncovered_clean(hermes_home):
    c = ip._check_uncovered(with_anthropic=False)
    assert c.status == "pass"


def test_check_uncovered_anthropic_warns(hermes_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    c = ip._check_uncovered(with_anthropic=False)
    assert c.status == "warn"
    assert "ANTHROPIC_API_KEY" in c.detail
    # With opt-in, Anthropic is no longer uncovered.
    c2 = ip._check_uncovered(with_anthropic=True)
    assert "ANTHROPIC_API_KEY" not in c2.detail


# ---------------------------------------------------------------------------
# Check 11: ssrf-deny
# ---------------------------------------------------------------------------


def test_check_ssrf_deny_present(hermes_home):
    p = _write_config(hermes_home, with_imds=True)
    c = ip._check_ssrf_deny(p)
    assert c.status == "pass"


def test_check_ssrf_deny_imds_removed_fails(hermes_home):
    p = _write_config(hermes_home, with_imds=False)
    c = ip._check_ssrf_deny(p)
    assert c.status == "fail"
    assert "169.254.0.0/16" in c.detail


# ---------------------------------------------------------------------------
# --check selection
# ---------------------------------------------------------------------------


def test_run_doctor_only_runs_named_checks(hermes_home, monkeypatch):
    _fake_binary(hermes_home)
    monkeypatch.setattr(ip, "iron_proxy_version",
                        lambda b: f"v{ip._IRON_PROXY_VERSION}")
    _write_ca(hermes_home)
    report = ip.run_doctor(network=False, only=["binary", "ca"])
    names = {c.name for c in report.checks}
    assert names == {"binary", "ca"}


# ---------------------------------------------------------------------------
# Integration: full hermetic run_doctor(network=False)
# ---------------------------------------------------------------------------


def test_run_doctor_no_network_end_to_end(hermes_home, monkeypatch):
    """A fully-wired fixture home should produce a report whose network
    checks are skipped and whose JSON shape is stable."""
    _fake_binary(hermes_home)
    monkeypatch.setattr(ip, "iron_proxy_version",
                        lambda b: f"iron-proxy v{ip._IRON_PROXY_VERSION}")
    _write_ca(hermes_home)
    _write_config(hermes_home, with_imds=True)
    _write_mappings(hermes_home, env_name="OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(ip, "_read_pid", lambda: 4242)
    monkeypatch.setattr(ip, "_pid_alive", lambda pid: True)
    # listening socket -> pretend open, skip lsof.
    import socket

    class _D:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _D())
    # docker probe -> skip (pretend docker not installed) by patching which
    # only for "docker"/"lsof", keep openssl reachable.
    real_which = ip.shutil.which
    monkeypatch.setattr(ip.shutil, "which",
                        lambda n: None if n in ("docker", "lsof") else real_which(n))

    report = ip.run_doctor(network=False)

    # JSON shape
    d = report.to_dict()
    assert set(d.keys()) == {"checks", "summary"}
    assert set(d["summary"].keys()) == {"pass", "warn", "fail", "skip"}
    for c in d["checks"]:
        assert set(c.keys()) == {"name", "status", "detail", "fix"}

    # Network checks were skipped.
    by_name = {c.name: c.status for c in report.checks}
    assert by_name["reachability"] == "skip"
    assert by_name["token-swap"] == "skip"
    # No failures in a wired hermetic home -> ok + exit-0 semantics.
    assert report.n_fail == 0
    assert report.ok is True
