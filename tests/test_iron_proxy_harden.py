"""Hermetic tests for ``hermes egress harden`` (host_hardening.survey_host).

Each signal is exercised with monkeypatched subprocess / filesystem so we
never touch the network, a real firewall, or a real iron-proxy daemon.
Mirrors the style of ``tests/test_iron_proxy_doctor.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent.proxy_sources import host_hardening as hh
from agent.proxy_sources import iron_proxy as ip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _patch_run(monkeypatch, table):
    """Patch hh._run with a dispatch table keyed on the first arg (binary).

    ``table`` maps binary name -> (returncode, stdout, stderr).  A binary
    absent from the table behaves as "not installed" (returncode None).
    """
    def fake_run(cmd):
        entry = table.get(cmd[0])
        if entry is None:
            return None, "", ""
        return entry
    monkeypatch.setattr(hh, "_run", fake_run)


def _force_linux(monkeypatch):
    monkeypatch.setattr(hh, "_is_linux", lambda: True)


def _signal(signals, name):
    return next(s for s in signals if s.name == name)


@pytest.fixture
def no_iron_proxy(monkeypatch):
    """Default: iron-proxy not configured / not running so the proxy
    signals don't accidentally pass from the host's real state."""
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=False, pid=None, listening=False),
    )


# ---------------------------------------------------------------------------
# PASS path, one per signal (10 tests)
# ---------------------------------------------------------------------------


def test_tailscale_pass(monkeypatch, no_iron_proxy):
    _patch_run(monkeypatch, {
        "tailscale": (0, json.dumps({"BackendState": "Running"}), ""),
    })
    s = _signal(hh.survey_host(), "tailscale")
    assert s.status == hh.PASS


def test_ufw_pass(monkeypatch, no_iron_proxy):
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "ufw": (0, "Status: active\nDefault: deny (incoming), allow (outgoing)", ""),
    })
    s = _signal(hh.survey_host(), "ufw")
    assert s.status == hh.PASS
    assert "deny (incoming)" in s.detail


def test_firewalld_pass(monkeypatch, no_iron_proxy):
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {"firewall-cmd": (0, "running\n", "")})
    s = _signal(hh.survey_host(), "firewalld")
    assert s.status == hh.PASS


def test_nftables_pass(monkeypatch, no_iron_proxy):
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "nft": (0, "table inet filter {\n  chain input { type filter }\n}", ""),
    })
    s = _signal(hh.survey_host(), "nftables")
    assert s.status == hh.PASS


def test_fail2ban_pass(monkeypatch, no_iron_proxy):
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "fail2ban-client": (0, "Status\n|- Number of jail:\t1\n`- Jail list:\tsshd", ""),
    })
    s = _signal(hh.survey_host(), "fail2ban")
    assert s.status == hh.PASS
    assert "sshd" in s.detail


def test_ssh_password_auth_pass(monkeypatch, no_iron_proxy):
    monkeypatch.setattr(hh, "_read_sshd_config",
                        lambda: "PasswordAuthentication no\n")
    s = _signal(hh.survey_host(), "ssh-password-auth")
    assert s.status == hh.PASS


def test_ssh_root_login_pass(monkeypatch, no_iron_proxy):
    monkeypatch.setattr(hh, "_read_sshd_config",
                        lambda: "PermitRootLogin prohibit-password\n")
    s = _signal(hh.survey_host(), "ssh-root-login")
    assert s.status == hh.PASS


def test_iron_proxy_enabled_pass(monkeypatch):
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=True, pid=4242, listening=True),
    )
    s = _signal(hh.survey_host(), "iron-proxy-enabled")
    assert s.status == hh.PASS


def test_iron_proxy_running_pass(monkeypatch):
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=True, pid=4242, listening=True,
                               tunnel_port=9090),
    )
    s = _signal(hh.survey_host(), "iron-proxy-running")
    assert s.status == hh.PASS
    assert "4242" in s.detail


def test_docker_seccomp_pass(monkeypatch, no_iron_proxy):
    _patch_run(monkeypatch, {
        "docker": (0, "[name=seccomp,profile=builtin name=cgroupns]", ""),
    })
    s = _signal(hh.survey_host(), "docker-seccomp")
    assert s.status == hh.PASS


# ---------------------------------------------------------------------------
# SKIP path: binary missing (parametrized over binary-dependent signals)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signal_name", [
    "tailscale", "ufw", "firewalld", "nftables", "fail2ban", "docker-seccomp",
])
def test_binary_missing_skips(monkeypatch, no_iron_proxy, signal_name):
    _force_linux(monkeypatch)
    # Empty dispatch table -> every binary reads as "not installed".
    _patch_run(monkeypatch, {})
    s = _signal(hh.survey_host(), signal_name)
    assert s.status == hh.SKIP


# ---------------------------------------------------------------------------
# Baseline evaluation
# ---------------------------------------------------------------------------


def _wire_minimal_pass(monkeypatch):
    """Host with ufw + ssh-no-password + iron-proxy enabled."""
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "ufw": (0, "Status: active\nDefault: deny (incoming), allow (outgoing)", ""),
    })
    monkeypatch.setattr(hh, "_read_sshd_config",
                        lambda: "PasswordAuthentication no\n")
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=True, pid=4242, listening=True),
    )


def test_minimal_baseline_satisfied(monkeypatch):
    _wire_minimal_pass(monkeypatch)
    signals = hh.survey_host(baseline="minimal")
    satisfied, missing = hh.baseline_status(signals, "minimal")
    assert satisfied is True
    assert missing == []


def test_catalin_baseline_missing_fail2ban(monkeypatch):
    # Tailscale + ufw + ssh-no-password + iron-proxy, but NO fail2ban.
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "tailscale": (0, json.dumps({"BackendState": "Running"}), ""),
        "ufw": (0, "Status: active\nDefault: deny (incoming)", ""),
        # fail2ban-client absent -> skip (not pass)
    })
    monkeypatch.setattr(hh, "_read_sshd_config",
                        lambda: "PasswordAuthentication no\n")
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=True, pid=4242, listening=True),
    )
    signals = hh.survey_host(baseline="catalin")
    satisfied, missing = hh.baseline_status(signals, "catalin")
    assert satisfied is False
    assert "fail2ban" in missing


def test_paranoid_baseline_requires_all(monkeypatch):
    # Wire everything to pass.
    _force_linux(monkeypatch)
    _patch_run(monkeypatch, {
        "tailscale": (0, json.dumps({"BackendState": "Running"}), ""),
        "ufw": (0, "Status: active\nDefault: deny (incoming)", ""),
        "firewall-cmd": (0, "running\n", ""),
        "nft": (0, "table inet filter { chain c {} }", ""),
        "fail2ban-client": (0, "Jail list:\tsshd", ""),
        "docker": (0, "[name=seccomp]", ""),
    })
    monkeypatch.setattr(
        hh, "_read_sshd_config",
        lambda: "PasswordAuthentication no\nPermitRootLogin no\n",
    )
    monkeypatch.setattr(
        hh, "get_status",
        lambda: ip.ProxyStatus(enabled=True, pid=4242, listening=True),
    )
    signals = hh.survey_host(baseline="paranoid")
    satisfied, missing = hh.baseline_status(signals, "paranoid")
    assert satisfied is True
    assert missing == []
    # Sanity: every signal name is in the paranoid requirement set.
    assert {n for n, in (g for g in hh.BASELINES["paranoid"])} == set(hh.SIGNAL_NAMES)


def test_paranoid_baseline_incomplete_when_one_skips(monkeypatch):
    # Same as minimal-pass but missing most binaries -> paranoid fails.
    _wire_minimal_pass(monkeypatch)
    signals = hh.survey_host(baseline="paranoid")
    satisfied, missing = hh.baseline_status(signals, "paranoid")
    assert satisfied is False
    # firewalld / nftables / fail2ban / docker not wired -> in missing.
    assert "fail2ban" in missing


# ---------------------------------------------------------------------------
# JSON schema + --all behavior (via the CLI handler)
# ---------------------------------------------------------------------------


def test_json_schema(monkeypatch, capsys):
    import argparse
    from hermes_cli import proxy_cli

    _wire_minimal_pass(monkeypatch)
    ns = argparse.Namespace(baseline="minimal", as_json=True, show_all=False)
    rc = proxy_cli.cmd_harden(ns)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) == {"signals", "baseline", "satisfied", "missing"}
    assert out["baseline"] == "minimal"
    assert isinstance(out["satisfied"], bool)
    assert isinstance(out["missing"], list)
    for sig in out["signals"]:
        assert set(sig.keys()) == {"name", "status", "detail", "fix"}


def test_all_shows_passing_signals(monkeypatch, capsys):
    import argparse
    from hermes_cli import proxy_cli

    _wire_minimal_pass(monkeypatch)
    # default view: passing signals hidden
    proxy_cli.cmd_harden(argparse.Namespace(
        baseline="minimal", as_json=False, show_all=False))
    default_out = capsys.readouterr().out
    # --all view: passing signals shown
    proxy_cli.cmd_harden(argparse.Namespace(
        baseline="minimal", as_json=False, show_all=True))
    all_out = capsys.readouterr().out
    # "ssh-password-auth" passes and its name appears in no other signal's
    # fix text, so it's a clean marker: present under --all, absent in the
    # default (gaps-only) view.
    assert "ssh-password-auth" in all_out
    assert "ssh-password-auth" not in default_out


# ---------------------------------------------------------------------------
# Platform + missing-file graceful degradation
# ---------------------------------------------------------------------------


def test_non_linux_skips_linux_signals(monkeypatch, no_iron_proxy):
    monkeypatch.setattr(hh, "_is_linux", lambda: False)
    monkeypatch.setattr(hh.platform, "system", lambda: "Darwin")
    # Tailscale + docker still probe-able on macOS; wire tailscale to pass.
    _patch_run(monkeypatch, {
        "tailscale": (0, json.dumps({"BackendState": "Running"}), ""),
    })
    # sshd config absent on this mock box.
    monkeypatch.setattr(hh, "_read_sshd_config", lambda: None)
    signals = hh.survey_host()
    by_name = {s.name: s.status for s in signals}
    # Linux-specific firewall + fail2ban signals skip cleanly.
    for n in ("ufw", "firewalld", "nftables", "fail2ban"):
        assert by_name[n] == hh.SKIP
    # macOS-friendly signals still run.
    assert by_name["tailscale"] == hh.PASS
    assert by_name["iron-proxy-enabled"] in (hh.PASS, hh.FAIL)


def test_missing_sshd_config_skips_both_ssh_signals(monkeypatch, no_iron_proxy):
    monkeypatch.setattr(hh, "_read_sshd_config", lambda: None)
    signals = hh.survey_host()
    assert _signal(signals, "ssh-password-auth").status == hh.SKIP
    assert _signal(signals, "ssh-root-login").status == hh.SKIP


# ---------------------------------------------------------------------------
# survey_host rejects unknown baselines
# ---------------------------------------------------------------------------


def test_survey_host_rejects_unknown_baseline(no_iron_proxy):
    with pytest.raises(ValueError):
        hh.survey_host(baseline="bogus")
