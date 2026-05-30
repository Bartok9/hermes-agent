"""CLI handlers for ``hermes egress ...``.

Subcommands:
    install  — download the pinned iron-proxy binary
    setup    — interactive wizard: install binary, generate CA, mint tokens, write config
    start    — launch the proxy as a managed subprocess
    stop     — terminate the managed proxy
    status   — show binary version + config presence + listen state + mappings
    disable  — flip ``proxy.enabled`` to False (does not stop a running proxy)
    config   — print the generated proxy.yaml path (for debugging / external review)

The top-level command is ``hermes egress``.  Note that the inbound OAuth
reverse-proxy command (``hermes proxy``) lives elsewhere in
``hermes_cli/main.py`` — different direction, different purpose.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.proxy_sources import iron_proxy as ip
from agent.proxy_sources import host_hardening as hh
from hermes_cli.config import load_config, save_config


# ---------------------------------------------------------------------------
# Argparse wiring — called from hermes_cli.main
# ---------------------------------------------------------------------------


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Attach the egress subcommand tree to a parent parser.

    Called from ``hermes_cli.main`` as part of building the top-level
    ``hermes egress`` parser.
    """

    # dest='egress_command' — keeps this subparser tree disjoint from the
    # inbound OAuth ``hermes proxy`` subparser (which uses dest='proxy_command').
    # No runtime collision today since they live in separate parser trees,
    # but a future grep-and-refactor on ``proxy_command`` would otherwise
    # hit both handlers.
    sub = parent_parser.add_subparsers(dest="egress_command")

    install = sub.add_parser(
        "install",
        help=f"Download iron-proxy binary (v{ip._IRON_PROXY_VERSION})",
    )
    install.add_argument(
        "--force", action="store_true",
        help="Re-download even if a managed copy already exists",
    )
    install.set_defaults(func=cmd_install)

    setup = sub.add_parser(
        "setup",
        help="Interactive wizard: install + CA + mint tokens + write config",
    )
    setup.add_argument(
        "--tunnel-port", type=int, default=None,
        help=f"Override the tunnel port (default {ip._DEFAULT_TUNNEL_PORT})",
    )
    setup.add_argument(
        "--from-bitwarden", action="store_true",
        help="Treat secrets as managed by Bitwarden — discover provider keys "
             "from secrets.bitwarden config instead of the current env.  Fails "
             "loudly if BW is unreachable rather than silently falling back.",
    )
    setup.add_argument(
        "--no-bitwarden", action="store_true",
        help="Explicitly switch credential_source back to env on re-setup "
             "(only meaningful when the previous setup used --from-bitwarden).",
    )
    setup.add_argument(
        "--rotate-tokens", action="store_true",
        help="Mint fresh proxy tokens for every provider (default is to "
             "preserve tokens for providers that already had one — avoids "
             "401-ing already-running sandboxes on re-setup).",
    )
    setup.add_argument(
        "--with-anthropic", action="store_true",
        help="Also proxy Anthropic native (api.anthropic.com) by minting an "
             "x-api-key swap rule for ANTHROPIC_API_KEY.  Off by default "
             "because the key may be used via OpenRouter instead.",
    )
    setup.set_defaults(func=cmd_setup)

    start = sub.add_parser("start", help="Start the managed iron-proxy")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop the managed iron-proxy")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="Show proxy state and mappings")
    status.add_argument(
        "--show-tokens", action="store_true",
        help="Print the proxy tokens (default: redacted prefix only). "
             "Beware: tokens may persist in your shell history.",
    )
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser(
        "doctor",
        help="End-to-end egress health check (binary, CA, config, daemon, "
             "reachability, token-swap, SSRF guard, docker DNS).",
    )
    doctor.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit a single JSON object with checks[] + summary{}.",
    )
    doctor.add_argument(
        "--check", action="append", default=None, dest="only", metavar="NAME",
        help=f"Run only the named check (repeatable).  One of: "
             f"{', '.join(ip.DOCTOR_CHECK_NAMES)}",
    )
    doctor.add_argument(
        "--no-network", action="store_true", dest="no_network",
        help="Skip reachability + token-swap probes (CI / hermetic).",
    )
    doctor.set_defaults(func=cmd_doctor)

    audit = sub.add_parser(
        "audit",
        help="View / search / aggregate the iron-proxy audit log.",
    )
    audit_sub = audit.add_subparsers(dest="audit_command")

    a_tail = audit_sub.add_parser("tail", help="Show the last N audit lines.")
    a_tail.add_argument("-n", type=int, default=50, dest="n",
                        help="Number of lines (default 50).")
    a_tail.add_argument("-f", "--follow", action="store_true", dest="follow",
                        help="Follow the log (poll every 250ms).")
    a_tail.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit JSON-Lines instead of a table.")
    a_tail.set_defaults(func=cmd_audit_tail)

    a_grep = audit_sub.add_parser("grep", help="Filter audit lines by regex.")
    a_grep.add_argument("pattern", help="Regex matched against the raw line.")
    a_grep.add_argument("--since", default=None,
                        help="Time window: 30m/2h/7d/1w, 'today', or ISO date.")
    a_grep.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit matching JSON-Lines.")
    a_grep.set_defaults(func=cmd_audit_grep)

    a_stats = audit_sub.add_parser("stats", help="Aggregate counts + anomalies.")
    a_stats.add_argument("--since", default=None,
                         help="Time window: 30m/2h/7d/1w, 'today', or ISO date.")
    a_stats.add_argument("--json", action="store_true", dest="as_json",
                         help="Emit the stats object as JSON.")
    a_stats.set_defaults(func=cmd_audit_stats)

    a_export = audit_sub.add_parser("export", help="Bulk export for SIEM.")
    a_export.add_argument("--format", choices=("json", "csv"), default="json",
                          dest="fmt", help="Output format (default json).")
    a_export.add_argument("--out", default=None,
                          help="Write to PATH instead of stdout.")
    a_export.add_argument("--since", default=None,
                          help="Time window: 30m/2h/7d/1w, 'today', or ISO date.")
    a_export.set_defaults(func=cmd_audit_export)

    audit.set_defaults(func=cmd_audit_default)

    harden = sub.add_parser(
        "harden",
        help="Host-hardening survey: firewall, SSH, fail2ban, mesh-VPN, "
             "Docker seccomp, + iron-proxy runtime (complements `doctor`).",
    )
    harden.add_argument(
        "--baseline", choices=hh.BASELINE_NAMES, default="minimal",
        help="Baseline to evaluate against (default: minimal). Colors the "
             "summary line only — never gates the exit code.",
    )
    harden.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit {signals[], baseline, satisfied, missing[]} as JSON.",
    )
    harden.add_argument(
        "--all", action="store_true", dest="show_all",
        help="Show ALL signals including passing ones (default: hide "
             "passing signals to focus on gaps).",
    )
    harden.set_defaults(func=cmd_harden)

    disable = sub.add_parser("disable", help="Turn off the proxy integration")
    disable.set_defaults(func=cmd_disable)

    cfg = sub.add_parser("config", help="Print the generated proxy.yaml path")
    cfg.set_defaults(func=cmd_config)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    console = Console()
    try:
        binary = ip.install_iron_proxy(force=bool(args.force))
    except Exception as exc:  # noqa: BLE001 — top-level user-facing error funnel
        console.print(f"[red]✗ install failed:[/red] {exc}")
        console.print(
            "  Manual install: https://github.com/ironsh/iron-proxy/releases"
        )
        return 1
    version = ip.iron_proxy_version(binary) or "(version unknown)"
    console.print(f"[green]✓[/green] installed {binary}  {version}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    console = Console()
    console.print(Panel.fit(
        "[bold]iron-proxy setup[/bold]\n\n"
        "Routes outbound sandbox traffic through a local TLS-intercepting\n"
        "proxy so prompt-injected agents never see real provider API keys.\n\n"
        "[dim]Project: https://github.com/ironsh/iron-proxy  (Apache-2.0)[/dim]",
        border_style="cyan",
    ))

    # ------------------------------------------------------------------ binary
    console.print()
    console.print("[bold]Step 1[/bold]  Install the iron-proxy binary")
    try:
        binary = ip.find_iron_proxy(install_if_missing=False)
        if binary is None:
            console.print("  No iron-proxy on PATH — downloading…")
            binary = ip.install_iron_proxy()
        version = ip.iron_proxy_version(binary) or "(version unknown)"
        console.print(f"  [green]✓[/green] {binary}  {version}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗ install failed: {exc}[/red]")
        return 1

    # ------------------------------------------------------------------ CA
    console.print()
    console.print("[bold]Step 2[/bold]  Generate a CA cert")
    try:
        ca_crt, ca_key = ip.ensure_ca_cert()
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗ CA generation failed: {exc}[/red]")
        return 1
    console.print(f"  [green]✓[/green] {ca_crt}")

    # ------------------------------------------------------------------ mint
    console.print()
    console.print("[bold]Step 3[/bold]  Mint proxy tokens for known providers")

    available_env_names: List[str] = []
    if args.from_bitwarden:
        cfg = load_config()
        bw_cfg = (cfg.get("secrets") or {}).get("bitwarden") or {}
        if not bw_cfg.get("enabled"):
            console.print(
                "  [red]✗ --from-bitwarden requested but "
                "secrets.bitwarden.enabled is false.[/red]"
            )
            console.print(
                "  Run `hermes secrets bitwarden setup` first, or omit "
                "--from-bitwarden."
            )
            return 1
        try:
            from agent.secret_sources import bitwarden as bw
            access_token = os.environ.get(
                bw_cfg.get("access_token_env", "BWS_ACCESS_TOKEN"), ""
            ).strip()
            if not access_token:
                console.print(
                    f"  [red]✗ --from-bitwarden requested but "
                    f"{bw_cfg.get('access_token_env', 'BWS_ACCESS_TOKEN')} "
                    "is not set in the environment.[/red]"
                )
                return 1
            secrets, _ = bw.fetch_bitwarden_secrets(
                access_token=access_token,
                project_id=bw_cfg.get("project_id", ""),
                cache_ttl_seconds=0,
                use_cache=False,
            )
            available_env_names = list(secrets.keys())
            if not available_env_names:
                console.print(
                    "  [red]✗ Bitwarden returned an empty secrets list.[/red]\n"
                    "  Check the project_id in secrets.bitwarden and the "
                    "BWS access-token's project scope."
                )
                return 1
            console.print(
                f"  Pulled {len(available_env_names)} env names from Bitwarden."
            )
        except Exception as exc:  # noqa: BLE001 — explicit user-facing error
            console.print(
                f"  [red]✗ Could not enumerate Bitwarden secrets: {exc}[/red]"
            )
            console.print(
                "  Either fix the Bitwarden config and retry, or rerun setup "
                "without --from-bitwarden (the proxy will read secrets from "
                "the host process env at start time)."
            )
            return 1

    with_anthropic = bool(getattr(args, "with_anthropic", False))

    discovered = ip.discover_provider_mappings(
        available_env_names=available_env_names or None,
    )
    # Anthropic native (x-api-key) is opt-in via --with-anthropic.  When
    # set, mint a dedicated x-api-key mapping so api.anthropic.com traffic
    # is covered like the Bearer providers.
    discovered += ip.discover_xapikey_mappings(
        available_env_names=available_env_names or None,
        with_anthropic=with_anthropic,
    )
    if with_anthropic and not any(
        m.real_env_name == "ANTHROPIC_API_KEY" for m in discovered
    ):
        console.print(
            "  [yellow]Note: --with-anthropic was passed but "
            "ANTHROPIC_API_KEY is not set — no Anthropic rule added.[/yellow]"
        )

    # Preserve tokens for providers we already had unless the operator
    # explicitly requested rotation.  This prevents re-running `hermes
    # egress setup` from invalidating tokens baked into already-running
    # sandboxes.
    existing = ip.load_mappings()
    rotate = bool(getattr(args, "rotate_tokens", False))

    # P3 confirmation gate: --rotate-tokens invalidates every running
    # sandbox's proxy tokens immediately.  An accidental re-run (history
    # scroll-back, tmux paste) is unrecoverable, so require explicit
    # confirmation when there's something to actually rotate.  Skipped
    # when stdin isn't a tty (CI / non-interactive use), in which case
    # the operator passed the flag deliberately.
    if rotate and existing:
        import sys as _sys
        from datetime import datetime as _dt
        if _sys.stdin.isatty():
            console.print(
                "[yellow]⚠[/yellow]  --rotate-tokens will invalidate proxy "
                "tokens in every running Hermes sandbox.  They will start "
                "401-ing against upstreams until restarted."
            )
            try:
                ans = input("Type 'rotate' to confirm: ").strip().lower()
            except EOFError:
                ans = ""
            if ans != "rotate":
                console.print("[yellow]Cancelled.[/yellow]")
                return 1
        # Backup the existing mappings before we overwrite.  The
        # resulting ``.rotated-<unix>`` sibling is plain JSON and lets
        # the operator manually recover tokens if they realise the
        # rotation was a mistake.
        try:
            import shutil as _shutil
            state_dir = ip._proxy_state_dir()
            mappings_src = state_dir / "mappings.json"
            if mappings_src.exists():
                ts = _dt.now().strftime("%Y%m%dT%H%M%S")
                backup = state_dir / f"mappings.json.rotated-{ts}"
                _shutil.copy2(str(mappings_src), str(backup))
                console.print(f"  [dim]backup: {backup}[/dim]")
        except OSError as exc:
            console.print(
                f"  [yellow]Could not back up mappings before rotation: "
                f"{exc}[/yellow]"
            )
    elif rotate and not existing:
        console.print(
            "[dim]Note: --rotate-tokens is a no-op on first-time setup "
            "(no existing tokens to rotate).[/dim]"
        )

    mappings = ip.merge_mappings(
        existing=existing,
        discovered=discovered,
        rotate=rotate,
    )

    if not mappings:
        console.print(
            "  [yellow]No known provider API keys found in env/Bitwarden.[/yellow]"
        )
        console.print(
            "  Set at least one of these and rerun setup:"
        )
        for env_name in sorted(ip._BEARER_PROVIDERS):
            console.print(f"    - {env_name}")
        return 1

    # Warn the operator about providers we recognize but can't proxy
    # (Anthropic native, AWS Bedrock, Azure OpenAI, etc).  These still
    # work — they just bypass the egress isolation.
    uncovered = ip.discover_uncovered_providers(
        available_env_names=available_env_names or None,
        with_anthropic=with_anthropic,
    )
    if with_anthropic:
        console.print(
            "  [green]✓[/green] Anthropic native covered via x-api-key rule "
            "(api.anthropic.com)"
        )
    else:
        console.print(
            "  [dim]Tip: Anthropic native (api.anthropic.com) is available "
            "with [cyan]--with-anthropic[/cyan].[/dim]"
        )
    if uncovered:
        console.print()
        console.print(
            "  [yellow]⚠[/yellow]  Detected provider env vars that the "
            "proxy does not yet cover:"
        )
        for name in uncovered:
            console.print(f"    - {name}")
        console.print(
            "  [dim]These providers use non-bearer auth (x-api-key, "
            "SigV4, etc.) and will hold real credentials inside the "
            "sandbox.  Egress isolation is INCOMPLETE for these.[/dim]"
        )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider env", style="cyan")
    table.add_column("Upstream hosts", style="dim")
    table.add_column("Proxy token", style="green")
    for m in mappings:
        table.add_row(
            m.real_env_name,
            ", ".join(m.upstream_hosts),
            _redact_token(m.proxy_token),
        )
    console.print(table)

    # ------------------------------------------------------------------ write
    console.print()
    console.print("[bold]Step 4[/bold]  Write config and persist mappings")

    cfg = load_config()
    proxy_cfg = cfg.setdefault("proxy", {})
    # ``args.tunnel_port`` is None when the flag was not given; ``0`` is
    # invalid for a TCP listener so we treat it as an explicit refusal
    # and surface a clear error rather than silently substituting the
    # default.
    if args.tunnel_port is not None:
        if args.tunnel_port == 0:
            console.print(
                "  [red]✗ --tunnel-port=0 is not a valid TCP port.[/red]"
            )
            return 1
        tunnel_port = int(args.tunnel_port)
    else:
        tunnel_port = int(proxy_cfg.get("tunnel_port", ip._DEFAULT_TUNNEL_PORT))
    proxy_cfg["tunnel_port"] = tunnel_port

    extra_hosts = list(proxy_cfg.get("extra_allowed_hosts") or [])
    allowed = list(ip._DEFAULT_ALLOWED_HOSTS) + [
        h for h in extra_hosts if h not in ip._DEFAULT_ALLOWED_HOSTS
    ]

    audit_log_path = ip._proxy_state_dir() / "audit.log"
    # Pre-create the audit log with 0o600 so iron-proxy inherits private
    # perms instead of letting the daemon create it under the default
    # umask (potentially world-readable).  Raises on failure (planted
    # symlink, immutable parent, full disk) — the wizard must surface
    # that rather than print "✓" for a file the daemon will create
    # under a slacker umask.
    try:
        ip.ensure_audit_log(audit_log_path)
    except RuntimeError as exc:
        console.print(f"  [red]✗ {exc}[/red]")
        return 1

    # Allow operator override of the deny list via
    # ``proxy.upstream_deny_cidrs`` — but the default (None) gives a safe
    # default-deny list (loopback, IMDS, RFC1918) that matches the docs
    # promise.
    deny_cidrs = proxy_cfg.get("upstream_deny_cidrs")
    iron_cfg = ip.build_proxy_config(
        mappings=mappings,
        ca_cert=ca_crt,
        ca_key=ca_key,
        tunnel_port=tunnel_port,
        audit_log=audit_log_path,
        allowed_hosts=allowed,
        upstream_deny_cidrs=deny_cidrs,
    )
    cfg_path = ip.write_proxy_config(iron_cfg)
    mappings_path = ip.write_mappings(mappings)
    console.print(f"  [green]✓[/green] config:   {cfg_path}")
    console.print(f"  [green]✓[/green] mappings: {mappings_path}")
    console.print(f"  [green]✓[/green] audit log: {audit_log_path}")

    # ------------------------------------------------------------------ enable
    proxy_cfg["enabled"] = True
    proxy_cfg.setdefault("auto_install", True)
    proxy_cfg.setdefault("enforce_on_docker", True)
    # CRITICAL: do NOT silently downgrade credential_source on re-run.
    # If the operator previously configured `bitwarden` mode (e.g. for
    # rotation), running `hermes egress setup` again WITHOUT
    # --from-bitwarden must not rewrite credential_source to "env" —
    # that silently breaks the Bitwarden rotation guarantee the docs
    # make.  Require an explicit --no-bitwarden to switch back.
    existing_source = proxy_cfg.get("credential_source")
    if args.from_bitwarden:
        proxy_cfg["credential_source"] = "bitwarden"
    elif getattr(args, "no_bitwarden", False):
        proxy_cfg["credential_source"] = "env"
        if existing_source == "bitwarden":
            console.print(
                "[yellow]Switched credential_source from bitwarden to env.[/yellow]"
            )
    elif existing_source == "bitwarden":
        # Preserve the existing bitwarden mode.  Surface the decision so
        # the operator knows we kept it.
        console.print(
            "[dim]Keeping credential_source=bitwarden from existing config. "
            "Pass --no-bitwarden to switch to env-based credentials.[/dim]"
        )
    else:
        proxy_cfg["credential_source"] = "env"
    proxy_cfg.setdefault("fail_on_uncovered_providers", False)
    # Persist the Anthropic opt-in so `hermes egress start`'s
    # fail_on_uncovered_providers gate doesn't re-flag Anthropic as a
    # blocker after the operator wired up the x-api-key rule at setup.
    proxy_cfg["with_anthropic"] = with_anthropic
    save_config(cfg)

    console.print()
    console.print(
        "[green]✓ iron-proxy is configured.[/green]  "
        "Sandboxes will route outbound traffic through it."
    )
    console.print(
        "  Start:   [cyan]hermes egress start[/cyan]\n"
        "  Status:  [cyan]hermes egress status[/cyan]\n"
        "  Stop:    [cyan]hermes egress stop[/cyan]\n"
        "  Disable: [cyan]hermes egress disable[/cyan]"
    )
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    console = Console()
    cfg = load_config()
    proxy_cfg = cfg.get("proxy") or {}
    if not proxy_cfg.get("enabled"):
        console.print(
            "[yellow]proxy.enabled is false — run `hermes egress setup` "
            "first.[/yellow]"
        )
        return 1

    # If the operator opted in to Bitwarden-rotation semantics, refresh
    # upstream secrets from BSM at startup.  This is what delivers the
    # rotation guarantee that distinguishes ``credential_source:
    # bitwarden`` from ``credential_source: env``.  Without it, rotating
    # a key in the Bitwarden web app doesn't reach the proxy.
    credential_source = proxy_cfg.get("credential_source", "env")
    bw_cfg = (cfg.get("secrets") or {}).get("bitwarden")
    refresh_bw = (
        credential_source == "bitwarden"
        and bw_cfg is not None
        and bool(bw_cfg.get("enabled"))
    )
    # Pass the proxy-side allow_env_fallback opt-in through to
    # start_proxy.  This is a deliberate, documented escape hatch: when
    # set, the daemon silently falls back to host env if BWS is
    # unreachable, instead of raising.  Default is strict (raise).
    if refresh_bw and bw_cfg is not None:
        bw_cfg = dict(bw_cfg)
        bw_cfg["allow_env_fallback"] = bool(
            proxy_cfg.get("allow_env_fallback", False)
        )

    # fail_on_uncovered_providers: when true, refuse to start if any
    # LLM-specific non-bearer providers (Anthropic native, Azure OpenAI,
    # Gemini) have env vars set in the host process — those would
    # otherwise leak real credentials into the sandbox while bypassing
    # the proxy.  Only the strict LLM-specific subset blocks; generic
    # cloud creds (AWS_*, GOOGLE_APPLICATION_CREDENTIALS) still surface
    # as warnings via `discover_uncovered_providers` but don't block, to
    # avoid tripping every operator with terraform / gcloud set up.
    if bool(proxy_cfg.get("fail_on_uncovered_providers", False)):
        blocked = ip.discover_blocked_providers(
            with_anthropic=bool(proxy_cfg.get("with_anthropic", False)),
        )
        if blocked:
            console.print(
                "[red]✗ Refusing to start: provider env vars present "
                "that bypass the proxy:[/red]"
            )
            for name in blocked:
                console.print(f"    - {name}")
            console.print(
                "  Set `proxy.fail_on_uncovered_providers: false` in "
                "config.yaml to start anyway (sandbox will hold real "
                "credentials for those providers)."
            )
            return 1

    # stephenschoettler #1: when `credential_source: bitwarden`, the
    # operator picked BWS specifically to get the rotation guarantee —
    # silently falling back to parent-env at start_proxy time reintroduces
    # exactly the bug class the BW mode is supposed to defeat (host env
    # is stale / mismatched).  Pre-check at the wizard layer so we fail
    # loud with actionable error messages BEFORE start_proxy degrades.
    if refresh_bw:
        bw_access_env = (bw_cfg or {}).get("access_token_env", "BWS_ACCESS_TOKEN")
        if not os.environ.get(bw_access_env, "").strip():
            console.print(
                f"[red]✗ Refusing to start: credential_source=bitwarden but "
                f"{bw_access_env} is not set in the environment.[/red]"
            )
            console.print(
                "  Either export the access token, or run "
                "`hermes egress setup --no-bitwarden` to switch back to "
                "env-based credentials."
            )
            return 1
        if not (bw_cfg or {}).get("project_id"):
            console.print(
                "[red]✗ Refusing to start: credential_source=bitwarden but "
                "secrets.bitwarden.project_id is empty.[/red]"
            )
            console.print(
                "  Run `hermes secrets bitwarden setup` to configure the "
                "project, or switch back via `hermes egress setup "
                "--no-bitwarden`."
            )
            return 1

    try:
        status = ip.start_proxy(
            refresh_secrets_from_bitwarden=refresh_bw,
            bitwarden_config=bw_cfg,
        )
    except Exception as exc:  # noqa: BLE001 — top-level user-facing funnel
        console.print(f"[red]✗ failed to start iron-proxy:[/red] {exc}")
        return 1
    if status.pid:
        listening = (
            "[green]listening[/green]"
            if status.listening
            else "[yellow]not yet listening[/yellow]"
        )
        console.print(
            f"[green]✓[/green] iron-proxy running  pid={status.pid}  "
            f"port={status.tunnel_port}  {listening}"
        )
    else:
        console.print("[red]✗ iron-proxy did not come up cleanly[/red]")
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    console = Console()
    if ip.stop_proxy():
        console.print("[green]✓[/green] iron-proxy stopped")
    else:
        console.print("[dim]iron-proxy was not running[/dim]")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    console = Console()
    cfg = load_config()
    proxy_cfg = cfg.get("proxy") or {}
    status = ip.get_status()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("Enabled",        _yn(bool(proxy_cfg.get("enabled"))))
    table.add_row("Binary",         str(status.binary_path or "[dim](missing)[/dim]"))
    table.add_row("Binary version", status.binary_version or "[dim](unknown)[/dim]")
    table.add_row("Config",         str(status.config_path or "[dim](not generated)[/dim]"))
    table.add_row("CA cert",        str(status.ca_cert_path or "[dim](not generated)[/dim]"))
    table.add_row("Tunnel port",    str(status.tunnel_port))
    table.add_row("Process",        f"pid {status.pid}" if status.pid else "[dim](stopped)[/dim]")
    table.add_row("Listening",      _yn(status.listening))
    table.add_row("Credential src", str(proxy_cfg.get("credential_source", "env")))
    table.add_row("Docker enforce", _yn(bool(proxy_cfg.get("enforce_on_docker", True))))
    console.print(table)

    mappings = ip.load_mappings()
    if mappings:
        console.print()
        console.print("[bold]Token mappings[/bold]")
        m_table = Table(show_header=True, header_style="bold")
        m_table.add_column("Real env", style="cyan")
        m_table.add_column("Upstream", style="dim")
        m_table.add_column("Proxy token", style="green")
        for m in mappings:
            tok = m.proxy_token if args.show_tokens else _redact_token(m.proxy_token)
            m_table.add_row(m.real_env_name, ", ".join(m.upstream_hosts), tok)
        console.print(m_table)
        if args.show_tokens:
            console.print(
                "[yellow]⚠[/yellow]  proxy tokens just printed in full — "
                "they may persist in your shell history.  Consider clearing "
                "it after this command."
            )

    # Surface uncovered providers so the operator knows the isolation
    # boundary is incomplete for those upstreams.
    uncovered = ip.discover_uncovered_providers(
        with_anthropic=bool(proxy_cfg.get("with_anthropic", False)),
    )
    if uncovered:
        console.print()
        console.print(
            "[yellow]Uncovered providers[/yellow] "
            "(real credentials still visible inside the sandbox):"
        )
        for name in uncovered:
            console.print(f"  - {name}")

    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    console = Console()
    only = getattr(args, "only", None)
    if only:
        invalid = [c for c in only if c not in ip.DOCTOR_CHECK_NAMES]
        if invalid:
            console.print(
                f"[red]✗ unknown check(s): {', '.join(invalid)}[/red]"
            )
            console.print(
                f"  valid checks: {', '.join(ip.DOCTOR_CHECK_NAMES)}"
            )
            return 2

    network = not bool(getattr(args, "no_network", False))
    report = ip.run_doctor(network=network, only=only)

    if getattr(args, "as_json", False):
        import json as _json
        # Plain stdout (not rich) so the JSON is machine-parseable.
        print(_json.dumps(report.to_dict(), indent=2))
        return 0 if report.ok else 1

    _glyph = {
        "pass": "[green]✓[/green]",
        "warn": "[yellow]⚠[/yellow]",
        "fail": "[red]✗[/red]",
        "skip": "[dim]–[/dim]",
    }
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Detail")
    for c in report.checks:
        table.add_row(_glyph.get(c.status, "?"), c.name, c.detail)
    console.print(table)

    # Actionable fix-it block (brew-doctor style) for anything not passing.
    actionable = [c for c in report.checks if c.fix and c.status in ("warn", "fail")]
    if actionable:
        console.print()
        console.print("[bold]Suggested fixes[/bold]")
        for c in actionable:
            tone = "red" if c.status == "fail" else "yellow"
            console.print(f"  [{tone}]{c.name}[/{tone}]: {c.fix}")

    console.print()
    console.print(
        f"[bold]Summary[/bold]  "
        f"[green]{report.n_pass} pass[/green]  "
        f"[yellow]{report.n_warn} warn[/yellow]  "
        f"[red]{report.n_fail} fail[/red]  "
        f"[dim]{report.n_skip} skip[/dim]"
    )
    if report.ok:
        console.print("[green]✓ egress proxy looks healthy.[/green]")
    return 0 if report.ok else 1


# ---------------------------------------------------------------------------
# Host hardening survey handler
# ---------------------------------------------------------------------------


def cmd_harden(args: argparse.Namespace) -> int:
    """Survey host-perimeter hardening and show how it layers with the
    sandbox-egress proxy.  Always exits 0 — informational, never gates."""
    console = Console()
    baseline = getattr(args, "baseline", "minimal")
    show_all = bool(getattr(args, "show_all", False))

    signals = hh.survey_host(baseline=baseline)
    satisfied, missing = hh.baseline_status(signals, baseline)

    if getattr(args, "as_json", False):
        import json as _json
        # Plain stdout (not rich) so the JSON is machine-parseable.
        print(_json.dumps({
            "signals": [s.to_dict() for s in signals],
            "baseline": baseline,
            "satisfied": satisfied,
            "missing": missing,
        }, indent=2))
        # Informational command: always exit 0.
        return 0

    _glyph = {
        hh.PASS: "[green]\u2713[/green]",
        hh.WARN: "[yellow]\u26a0[/yellow]",
        hh.FAIL: "[red]\u2717[/red]",
        hh.SKIP: "[dim]\u2013[/dim]",
    }

    # Default view hides passing signals to focus the operator on gaps.
    shown = signals if show_all else [
        s for s in signals if s.status != hh.PASS
    ]

    table = Table(show_header=True, header_style="bold", box=None,
                  padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("Signal", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Action")
    for s in shown:
        table.add_row(
            _glyph.get(s.status, "?"),
            s.name,
            s.status,
            s.fix or "\u2014",
        )
    if shown:
        console.print(table)
    elif not show_all:
        console.print(
            "[green]\u2713 all signals pass[/green] "
            "[dim](pass --all to list them)[/dim]"
        )

    # Counts across the full set (not just the shown subset).
    n_pass = sum(1 for s in signals if s.status == hh.PASS)
    n_warn = sum(1 for s in signals if s.status == hh.WARN)
    n_fail = sum(1 for s in signals if s.status == hh.FAIL)
    n_skip = sum(1 for s in signals if s.status == hh.SKIP)
    console.print()
    console.print(
        f"[bold]Signals[/bold]  "
        f"[green]{n_pass} pass[/green]  "
        f"[yellow]{n_warn} warn[/yellow]  "
        f"[red]{n_fail} fail[/red]  "
        f"[dim]{n_skip} skip[/dim]"
    )

    if satisfied:
        console.print(
            f"[green]\u2713 {baseline} baseline satisfied[/green]"
        )
    else:
        # De-dupe while preserving order for the summary line.
        seen = []
        for n in missing:
            if n not in seen:
                seen.append(n)
        console.print(
            f"[red]\u2717 {baseline} baseline incomplete[/red] "
            f"(missing: {', '.join(seen)})"
        )

    # Informational only — never gate the exit code on hardening gaps.
    return 0


# ---------------------------------------------------------------------------
# Audit log handlers
# ---------------------------------------------------------------------------


_AUDIT_TABLE_FIELDS = ("ts", "method", "upstream_host", "path", "status",
                       "sandbox_id")


def _resolve_since(console: Console, spec):
    """Parse --since, printing a clear error and returning the sentinel
    ``False`` on failure (None means 'no window')."""
    if spec is None:
        return None
    try:
        return ip.parse_since(spec)
    except ValueError as exc:
        console.print(f"[red]✗ invalid --since: {exc}[/red]")
        return False


def _audit_row(ev: dict):
    if ev.get("_unparsed"):
        return None
    return [str(ev.get(f, "")) for f in _AUDIT_TABLE_FIELDS]


def _print_audit_table(console: Console, events):
    table = Table(show_header=True, header_style="bold", box=None,
                  padding=(0, 1))
    for f in _AUDIT_TABLE_FIELDS:
        table.add_column(f, overflow="fold")
    any_row = False
    for ev in events:
        row = _audit_row(ev)
        if row is None:
            # Show unparsed lines dimmed in the first column.
            table.add_row(f"[dim]{ev.get('_raw','')[:80]}[/dim]",
                          *([""] * (len(_AUDIT_TABLE_FIELDS) - 1)))
        else:
            table.add_row(*row)
        any_row = True
    if any_row:
        console.print(table)
    else:
        console.print("[dim](no audit events)[/dim]")


def cmd_audit_default(args: argparse.Namespace) -> int:
    # `hermes egress audit` with no subcommand -> show recent tail.
    return cmd_audit_tail(_audit_args(n=50))


def _audit_args(**overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        n=50, follow=False, as_json=False, since=None, pattern=None,
        fmt="json", out=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def cmd_audit_tail(args: argparse.Namespace) -> int:
    console = Console()
    path = ip.audit_log_path()
    n = max(1, int(getattr(args, "n", 50)))
    as_json = bool(getattr(args, "as_json", False))

    events = list(ip.iter_audit_log(path))
    tail = events[-n:]

    if as_json:
        for ev in tail:
            print(ev.get("_raw", ""))
    else:
        if not path.exists():
            console.print(f"[dim](no audit log at {path})[/dim]")
        else:
            _print_audit_table(console, tail)

    if not getattr(args, "follow", False):
        return 0

    # Follow mode: poll for new lines (no extra dependency).  Track byte
    # offset so we only emit appended content.
    import time
    try:
        offset = path.stat().st_size if path.exists() else 0
    except OSError:
        offset = 0
    try:
        while True:
            time.sleep(0.25)
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:
                # File truncated / rotated -- restart from the top.
                offset = 0
            if size > offset:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    if as_json:
                        print(line)
                    else:
                        console.print(line)
    except KeyboardInterrupt:
        return 0


def cmd_audit_grep(args: argparse.Namespace) -> int:
    import re
    console = Console()
    path = ip.audit_log_path()
    since = _resolve_since(console, getattr(args, "since", None))
    if since is False:
        return 2
    try:
        rx = re.compile(args.pattern)
    except re.error as exc:
        console.print(f"[red]✗ invalid regex: {exc}[/red]")
        return 2
    as_json = bool(getattr(args, "as_json", False))

    matched = []
    for ev in ip.iter_audit_log(path):
        if since is not None:
            ts = ev.get("_ts")
            if ts is None or ts < since:
                continue
        if rx.search(ev.get("_raw", "")):
            matched.append(ev)

    if as_json:
        for ev in matched:
            print(ev.get("_raw", ""))
    else:
        if not matched:
            console.print("[dim](no matching audit events)[/dim]")
        else:
            _print_audit_table(console, matched)
    return 0


def cmd_audit_stats(args: argparse.Namespace) -> int:
    console = Console()
    path = ip.audit_log_path()
    since = _resolve_since(console, getattr(args, "since", None))
    if since is False:
        return 2

    events = list(ip.iter_audit_log(path))
    stats = ip.aggregate_audit_stats(iter(events), since=since)
    # Anomalies compare the in-window events against the prior 24h baseline.
    if since is not None:
        from datetime import timedelta
        baseline_start = since - timedelta(hours=24)
        window = [e for e in events
                  if e.get("_ts") is not None and e.get("_ts") >= since]
        baseline = [e for e in events
                    if e.get("_ts") is not None
                    and baseline_start <= e.get("_ts") < since]
        anomalies = ip.detect_audit_anomalies(window, baseline=baseline)
    else:
        anomalies = ip.detect_audit_anomalies(events, baseline=None)

    if bool(getattr(args, "as_json", False)):
        import json as _json
        print(_json.dumps({"stats": stats, "anomalies": anomalies}, indent=2))
        return 0

    console.print(f"[bold]Audit stats[/bold]  ({stats['total']} events, "
                  f"{stats['unparsed']} unparsed)")
    st = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    st.add_column("Status", style="cyan")
    st.add_column("Count", justify="right")
    for code, cnt in sorted(stats["by_status"].items(),
                            key=lambda kv: str(kv[0])):
        st.add_row(str(code), str(cnt))
    console.print(st)
    console.print(f"[red]403 denied:[/red] {stats['denied']}")

    if stats["top_hosts"]:
        console.print("\n[bold]Top upstream hosts[/bold]")
        ht = Table(show_header=True, header_style="bold", box=None,
                   padding=(0, 2))
        ht.add_column("Host", style="cyan")
        ht.add_column("Requests", justify="right")
        for host, cnt in stats["top_hosts"]:
            ht.add_row(str(host), str(cnt))
        console.print(ht)

    if stats["top_sandboxes"]:
        console.print("\n[bold]Top sandboxes[/bold]")
        for sid, cnt in stats["top_sandboxes"]:
            console.print(f"  {sid}: {cnt}")

    if anomalies["first_time_hosts"]:
        console.print("\n[yellow]⚠ First-time upstream hosts in window[/yellow] "
                      "(not seen in prior 24h):")
        for h in anomalies["first_time_hosts"]:
            console.print(f"  - {h}")
    if anomalies["high_403_hosts"]:
        console.print("\n[yellow]⚠ Hosts with >5% 403 rate[/yellow] "
                      "(misconfig or probing):")
        for h, rate in anomalies["high_403_hosts"]:
            console.print(f"  - {h}: {rate:.0%}")
    return 0


def cmd_audit_export(args: argparse.Namespace) -> int:
    console = Console()
    path = ip.audit_log_path()
    since = _resolve_since(console, getattr(args, "since", None))
    if since is False:
        return 2
    fmt = getattr(args, "fmt", "json")
    out_path = getattr(args, "out", None)

    events = []
    for ev in ip.iter_audit_log(path):
        if since is not None:
            ts = ev.get("_ts")
            if ts is None or ts < since:
                continue
        # Strip the synthetic fields for a clean export.
        clean = {k: v for k, v in ev.items()
                 if k not in ("_raw", "_ts", "_unparsed")}
        if not clean and ev.get("_unparsed"):
            clean = {"raw": ev.get("_raw", "")}
        events.append(clean)

    if fmt == "json":
        import json as _json
        text = _json.dumps(events, indent=2)
    else:  # csv
        import csv
        import io
        cols = []
        for ev in events:
            for k in ev:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols or ["raw"],
                                extrasaction="ignore")
        writer.writeheader()
        for ev in events:
            writer.writerow(ev)
        text = buf.getvalue()

    if out_path:
        try:
            Path(out_path).write_text(text, encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]✗ could not write {out_path}: {exc}[/red]")
            return 1
        console.print(f"[green]✓[/green] exported {len(events)} event(s) "
                      f"to {out_path}")
    else:
        # Plain stdout so the export is pipeable.
        print(text)
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    console = Console()
    cfg = load_config()
    proxy_cfg = cfg.setdefault("proxy", {})
    if not proxy_cfg.get("enabled"):
        console.print("[dim]proxy.enabled was already false.[/dim]")
        return 0
    proxy_cfg["enabled"] = False
    save_config(cfg)
    console.print("[green]✓[/green] proxy.enabled set to false")
    # Use the public get_status() pid (which already incorporates the
    # _pid_alive check) instead of reaching into ip._read_pid().  That
    # private accessor only proves the pidfile is non-empty — a stale
    # pidfile from a crashed previous run would fire the warning
    # spuriously.
    if ip.get_status().pid is not None:
        console.print(
            "  iron-proxy is still running — stop it with "
            "[cyan]hermes egress stop[/cyan] if you want it down too."
        )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    console = Console()
    status = ip.get_status()
    if status.config_path is None:
        console.print(
            "[yellow](no config generated — run `hermes egress setup`)[/yellow]"
        )
        return 1
    console.print(str(status.config_path))
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yn(value: bool) -> str:
    return "[green]yes[/green]" if value else "[dim]no[/dim]"


def _redact_token(token: str) -> str:
    if len(token) < 16:
        return token
    return f"{token[:12]}…{token[-4:]}"
