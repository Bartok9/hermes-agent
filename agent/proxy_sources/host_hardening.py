"""Read-only host-hardening survey for ``hermes egress harden``.

This module probes the *host* for perimeter-security signals (firewall,
SSH config, fail2ban, mesh-VPN, Docker seccomp) and reports how they
layer with iron-proxy's sandbox-egress hardening.  It is intentionally
**complementary** to ``hermes egress doctor``:

    * ``doctor`` answers "is the egress proxy itself healthy?"  (binary,
      CA, config, daemon, token-swap, SSRF deny-list).
    * ``harden`` answers "is the *machine the proxy runs on* locked
      down?"  (firewall, SSH, fail2ban, mesh-VPN) and folds the two
      iron-proxy runtime signals in so an operator sees the whole
      defense-in-depth stack in one table.

Design constraints (mirrors iron_proxy.py):
    * Stdlib only — no ``cryptography``, ``python-iptables``, or DNS libs.
    * Every probe is best-effort and graceful: a missing binary or an
      unreadable file yields a ``skip`` (not a ``fail``), so the survey
      degrades cleanly on macOS / minimal containers / locked-down hosts.
    * No side effects.  Subprocess calls are all read-only status probes
      with short timeouts; we never start, stop, or rewrite anything.

Inspired by @catalinmpit's public Hetzner + Tailscale + UFW + Cloudflare
+ fail2ban deployment of a Hermes agent, which prompted Teknium's "can we
get a security review of the egress proxy?" question on X.  The
``catalin`` baseline encodes that exact perimeter shape.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from agent.proxy_sources.iron_proxy import get_status


# ---------------------------------------------------------------------------
# Status vocabulary — bare strings (not an enum) so --json output is
# trivially serializable and stable across versions, matching the doctor
# convention in iron_proxy.py.
# ---------------------------------------------------------------------------

PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

# Default sshd_config location.  Probed read-only; missing file -> skip.
_SSHD_CONFIG = Path("/etc/ssh/sshd_config")

# Short timeout for every read-only status probe.  A hung firewall/VPN
# binary must never wedge the survey.
_PROBE_TIMEOUT = 4.0


@dataclass
class HardeningSignal:
    """Result of a single host-hardening probe.

    ``status`` is one of pass/fail/warn/skip.  ``detail`` is a one-line
    human state description.  ``fix`` is a one-line remediation hint
    (shell command or doc link), ``None`` when the signal passes or is
    skipped with nothing to do.
    """

    name: str
    status: str
    detail: str
    fix: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


# ---------------------------------------------------------------------------
# Subprocess helper — read-only, short-timeout, never raises.
# ---------------------------------------------------------------------------


def _run(cmd: List[str]) -> Tuple[Optional[int], str, str]:
    """Run a read-only probe command.

    Returns ``(returncode, stdout, stderr)``.  ``returncode`` is ``None``
    when the binary is missing, times out, or otherwise can't be spawned
    — callers treat that as "skip", never "fail".
    """
    if shutil.which(cmd[0]) is None:
        return None, "", ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "", ""
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _is_linux() -> bool:
    return platform.system() == "Linux"


# ---------------------------------------------------------------------------
# Signal 1: Tailscale (mesh VPN) — cross-platform.
# ---------------------------------------------------------------------------


def _signal_tailscale() -> HardeningSignal:
    name = "tailscale"
    rc, out, _ = _run(["tailscale", "status", "--json"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "tailscale not installed",
            "install Tailscale to put the host on a private mesh: "
            "https://tailscale.com/download",
        )
    import json
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return HardeningSignal(
            name, WARN, "tailscale status returned non-JSON output",
            "check `tailscale status --json` manually",
        )
    backend = (data or {}).get("BackendState")
    if backend == "Running":
        return HardeningSignal(name, PASS, "BackendState=Running")
    return HardeningSignal(
        name, FAIL, f"BackendState={backend or 'unknown'}",
        "run `tailscale up` to connect the mesh",
    )


# ---------------------------------------------------------------------------
# Signal 2: UFW (Linux firewall front-end).
# ---------------------------------------------------------------------------


def _signal_ufw() -> HardeningSignal:
    name = "ufw"
    if not _is_linux():
        return HardeningSignal(name, SKIP, "ufw is Linux-only", None)
    rc, out, _ = _run(["ufw", "status", "verbose"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "ufw not installed",
            "apt install ufw && ufw default deny incoming && ufw enable",
        )
    if "Status: active" not in out:
        return HardeningSignal(
            name, FAIL, "ufw installed but inactive",
            "ufw default deny incoming && ufw enable",
        )
    # Parse the default-policy line, e.g.:
    #   "Default: deny (incoming), allow (outgoing), disabled (routed)"
    m = re.search(r"Default:\s*(.+)", out)
    policy = m.group(1).strip() if m else "active"
    if m and "deny (incoming)" not in policy and "reject (incoming)" not in policy:
        return HardeningSignal(
            name, WARN, f"active but default incoming not deny ({policy})",
            "ufw default deny incoming",
        )
    return HardeningSignal(name, PASS, f"active ({policy})")


# ---------------------------------------------------------------------------
# Signal 3: firewalld (Linux firewall daemon).
# ---------------------------------------------------------------------------


def _signal_firewalld() -> HardeningSignal:
    name = "firewalld"
    if not _is_linux():
        return HardeningSignal(name, SKIP, "firewalld is Linux-only", None)
    rc, out, _ = _run(["firewall-cmd", "--state"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "firewalld not installed",
            "install firewalld or use ufw/nftables instead",
        )
    if "running" in out.strip().lower():
        return HardeningSignal(name, PASS, "running")
    return HardeningSignal(
        name, FAIL, f"not running ({out.strip() or 'unknown'})",
        "systemctl enable --now firewalld",
    )


# ---------------------------------------------------------------------------
# Signal 4: nftables (Linux packet filter).
# ---------------------------------------------------------------------------


def _signal_nftables() -> HardeningSignal:
    name = "nftables"
    if not _is_linux():
        return HardeningSignal(name, SKIP, "nftables is Linux-only", None)
    rc, out, _ = _run(["nft", "list", "ruleset"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "nft not installed",
            "apt install nftables and load a ruleset",
        )
    if rc != 0:
        return HardeningSignal(
            name, WARN, "nft present but ruleset query failed (need root?)",
            "run `sudo nft list ruleset` to inspect",
        )
    if out.strip():
        return HardeningSignal(name, PASS, "non-empty ruleset loaded")
    return HardeningSignal(
        name, FAIL, "empty ruleset",
        "load an nftables ruleset (e.g. /etc/nftables.conf)",
    )


# ---------------------------------------------------------------------------
# Signal 5: fail2ban (SSH brute-force mitigation).
# ---------------------------------------------------------------------------


def _signal_fail2ban() -> HardeningSignal:
    name = "fail2ban"
    if not _is_linux():
        return HardeningSignal(name, SKIP, "fail2ban is Linux-only", None)
    rc, out, _ = _run(["fail2ban-client", "status"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "fail2ban not installed",
            "apt install fail2ban && systemctl enable --now fail2ban",
        )
    if rc != 0:
        return HardeningSignal(
            name, FAIL, "fail2ban-client could not reach the server",
            "systemctl enable --now fail2ban",
        )
    # "Jail list:\tsshd, ..." — non-empty after the colon means at least
    # one jail is configured.
    m = re.search(r"Jail list:\s*(.*)", out)
    jails = (m.group(1).strip() if m else "")
    if jails:
        return HardeningSignal(name, PASS, f"jails: {jails}")
    return HardeningSignal(
        name, FAIL, "running but no jails configured",
        "enable the sshd jail in /etc/fail2ban/jail.local",
    )


# ---------------------------------------------------------------------------
# Signals 6 & 7: SSH config (password auth + root login).
# ---------------------------------------------------------------------------


def _read_sshd_config() -> Optional[str]:
    """Return sshd_config text, or None when it's absent/unreadable.

    A missing file maps to ``skip`` (not ``fail``) in the callers: many
    hosts (macOS dev boxes, minimal containers) simply don't run sshd.
    """
    try:
        if not _SSHD_CONFIG.exists():
            return None
        return _SSHD_CONFIG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _signal_ssh_password_auth() -> HardeningSignal:
    name = "ssh-password-auth"
    text = _read_sshd_config()
    if text is None:
        return HardeningSignal(
            name, SKIP, f"{_SSHD_CONFIG} not present/readable", None
        )
    # Last effective directive wins in sshd_config; scan all matches.
    matches = re.findall(
        r"(?im)^\s*PasswordAuthentication\s+(\S+)", text
    )
    if not matches:
        return HardeningSignal(
            name, WARN,
            "PasswordAuthentication not set (sshd default is 'yes')",
            "set `PasswordAuthentication no` in /etc/ssh/sshd_config",
        )
    value = matches[-1].lower()
    if value == "no":
        return HardeningSignal(name, PASS, "PasswordAuthentication no")
    return HardeningSignal(
        name, FAIL, f"PasswordAuthentication {value}",
        "set `PasswordAuthentication no` and use key-based auth",
    )


def _signal_ssh_root_login() -> HardeningSignal:
    name = "ssh-root-login"
    text = _read_sshd_config()
    if text is None:
        return HardeningSignal(
            name, SKIP, f"{_SSHD_CONFIG} not present/readable", None
        )
    matches = re.findall(
        r"(?im)^\s*PermitRootLogin\s+(\S+)", text
    )
    if not matches:
        return HardeningSignal(
            name, WARN,
            "PermitRootLogin not set (sshd default is 'prohibit-password')",
            "set `PermitRootLogin no` in /etc/ssh/sshd_config",
        )
    value = matches[-1].lower()
    if value in ("no", "prohibit-password"):
        return HardeningSignal(name, PASS, f"PermitRootLogin {value}")
    return HardeningSignal(
        name, FAIL, f"PermitRootLogin {value}",
        "set `PermitRootLogin no` (or prohibit-password)",
    )


# ---------------------------------------------------------------------------
# Signals 8 & 9: iron-proxy runtime (reuse get_status — the seam to the
# sandbox-egress layer this command complements).
# ---------------------------------------------------------------------------


def _signal_iron_proxy_enabled() -> HardeningSignal:
    name = "iron-proxy-enabled"
    try:
        status = get_status()
    except Exception:  # noqa: BLE001 -- status probe must never crash the survey
        return HardeningSignal(
            name, SKIP, "could not read iron-proxy status", None
        )
    if status.enabled or status.configured:
        # get_status() leaves .enabled at its dataclass default (it doesn't
        # read config.yaml); treat a generated config + CA as "enabled" so
        # this signal works without a full config load.  An explicit
        # enabled=True (future callers) also passes.
        return HardeningSignal(
            name, PASS, "iron-proxy configured (CA + proxy.yaml present)"
        )
    return HardeningSignal(
        name, FAIL, "iron-proxy not configured",
        "run `hermes egress setup` to enable sandbox-egress isolation",
    )


def _signal_iron_proxy_running() -> HardeningSignal:
    name = "iron-proxy-running"
    try:
        status = get_status()
    except Exception:  # noqa: BLE001
        return HardeningSignal(
            name, SKIP, "could not read iron-proxy status", None
        )
    if status.pid and status.listening:
        return HardeningSignal(
            name, PASS, f"daemon pid={status.pid} listening on "
            f"127.0.0.1:{status.tunnel_port}"
        )
    if status.pid and not status.listening:
        return HardeningSignal(
            name, WARN, f"daemon pid={status.pid} alive but not listening",
            "check `hermes egress doctor --check listening`",
        )
    return HardeningSignal(
        name, FAIL, "iron-proxy daemon not running",
        "run `hermes egress start`",
    )


# ---------------------------------------------------------------------------
# Signal 10: Docker seccomp.
# ---------------------------------------------------------------------------


def _signal_docker_seccomp() -> HardeningSignal:
    name = "docker-seccomp"
    rc, out, _ = _run(["docker", "info", "--format", "{{.SecurityOptions}}"])
    if rc is None:
        return HardeningSignal(
            name, SKIP, "docker not installed",
            "install Docker if you run sandboxes via the Docker backend",
        )
    if rc != 0:
        return HardeningSignal(
            name, WARN, "docker present but `docker info` failed (daemon down?)",
            "start the Docker daemon and re-run",
        )
    if "seccomp" in out.lower():
        return HardeningSignal(name, PASS, "seccomp profile active")
    return HardeningSignal(
        name, FAIL, "seccomp not listed in SecurityOptions",
        "ensure the Docker default seccomp profile is not disabled",
    )


# ---------------------------------------------------------------------------
# Probe registry — order is the report order.
# ---------------------------------------------------------------------------

_PROBES: Tuple[Tuple[str, Callable[[], HardeningSignal]], ...] = (
    ("tailscale", _signal_tailscale),
    ("ufw", _signal_ufw),
    ("firewalld", _signal_firewalld),
    ("nftables", _signal_nftables),
    ("fail2ban", _signal_fail2ban),
    ("ssh-password-auth", _signal_ssh_password_auth),
    ("ssh-root-login", _signal_ssh_root_login),
    ("iron-proxy-enabled", _signal_iron_proxy_enabled),
    ("iron-proxy-running", _signal_iron_proxy_running),
    ("docker-seccomp", _signal_docker_seccomp),
)

SIGNAL_NAMES: Tuple[str, ...] = tuple(n for n, _ in _PROBES)


# ---------------------------------------------------------------------------
# Baselines — which signals must PASS for the baseline to be "satisfied".
#
# A baseline never *forces* a run to fail; it only colors the summary
# line.  Each entry is a list of requirement groups: a group is satisfied
# when ANY of its member signals passes (OR), and the baseline is
# satisfied when EVERY group is satisfied (AND).  This lets "any firewall"
# be one OR-group while "ssh password auth disabled" is a hard single.
# ---------------------------------------------------------------------------

# Each requirement is a tuple of signal-names; the requirement is met when
# at least one of them is PASS.
_Requirement = Tuple[str, ...]

BASELINES: Dict[str, List[_Requirement]] = {
    # Solo dev / single machine: any firewall (or Tailscale as the
    # perimeter) + SSH password auth off + egress proxy enabled.
    "minimal": [
        ("ufw", "firewalld", "nftables", "tailscale"),
        ("ssh-password-auth",),
        ("iron-proxy-enabled",),
    ],
    # @catalinmpit's Hetzner shape: mesh VPN + UFW + fail2ban + SSH
    # password auth off + egress proxy enabled.
    "catalin": [
        ("tailscale",),
        ("ufw",),
        ("fail2ban",),
        ("ssh-password-auth",),
        ("iron-proxy-enabled",),
    ],
    # Prod / multi-tenant: every signal passes.
    "paranoid": [(n,) for n in SIGNAL_NAMES],
}

BASELINE_NAMES: Tuple[str, ...] = tuple(BASELINES.keys())


def _missing_requirements(
    signals: List[HardeningSignal], baseline: str
) -> List[str]:
    """Return the signal-names that keep ``baseline`` from being satisfied.

    For an OR-group where none passed, every member name is reported (so
    the operator sees the full set of acceptable fixes).  For a single
    requirement, that one name is reported.
    """
    by_name = {s.name: s.status for s in signals}
    missing: List[str] = []
    for group in BASELINES.get(baseline, []):
        if any(by_name.get(n) == PASS for n in group):
            continue
        missing.extend(group)
    return missing


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def survey_host(*, baseline: str = "minimal") -> List[HardeningSignal]:
    """Probe the host for all hardening signals.

    ``baseline`` is accepted for API symmetry / forward-compat but does
    not change which signals are probed — every signal is always
    surveyed.  Use :func:`baseline_status` (or the ``--baseline`` CLI
    flag) to evaluate a probed signal list against a named baseline.
    """
    if baseline not in BASELINES:
        raise ValueError(
            f"unknown baseline {baseline!r}; "
            f"choose one of {', '.join(BASELINE_NAMES)}"
        )
    return [probe() for _, probe in _PROBES]


def baseline_status(
    signals: List[HardeningSignal], baseline: str
) -> Tuple[bool, List[str]]:
    """Evaluate probed ``signals`` against a named ``baseline``.

    Returns ``(satisfied, missing)`` where ``missing`` is the list of
    signal-names that would need to pass for the baseline to be met.
    """
    if baseline not in BASELINES:
        raise ValueError(
            f"unknown baseline {baseline!r}; "
            f"choose one of {', '.join(BASELINE_NAMES)}"
        )
    missing = _missing_requirements(signals, baseline)
    return (not missing), missing


__all__ = [
    "HardeningSignal",
    "survey_host",
    "baseline_status",
    "BASELINES",
    "BASELINE_NAMES",
    "SIGNAL_NAMES",
    "PASS",
    "FAIL",
    "WARN",
    "SKIP",
]
