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
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.proxy_sources import iron_proxy as ip
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

    rotate = sub.add_parser(
        "rotate-ca",
        help="Rotate the iron-proxy CA: archive the old cert, mint a fresh "
             "one, restart the daemon, and record the rotation.",
    )
    rotate.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print what WOULD happen (new subject, archive path, sandboxes "
             "to restart) without changing any files.",
    )
    rotate.add_argument(
        "--reason", default=None, metavar="TEXT",
        help="Free-text reason recorded in rotation-history.jsonl.",
    )
    rotate.add_argument(
        "--no-restart", action="store_true", dest="no_restart",
        help="Mint + archive but do NOT restart the daemon (staged rollout).",
    )
    rotate.add_argument(
        "--force", action="store_true", dest="force",
        help="Skip the interactive confirmation when running sandboxes "
             "would need restarting.",
    )
    rotate.set_defaults(func=cmd_rotate_ca)

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

    discovered = ip.discover_provider_mappings(
        available_env_names=available_env_names or None,
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
        blocked = ip.discover_blocked_providers()
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
    uncovered = ip.discover_uncovered_providers()
    if uncovered:
        console.print()
        console.print(
            "[yellow]Uncovered providers[/yellow] "
            "(real credentials still visible inside the sandbox):"
        )
        for name in uncovered:
            console.print(f"  - {name}")

    return 0


def cmd_rotate_ca(args: argparse.Namespace) -> int:
    console = Console()

    try:
        plan = ip.plan_ca_rotation()
    except Exception as exc:  # noqa: BLE001 -- user-facing error funnel
        console.print(f"[red]✗ could not plan CA rotation:[/red] {exc}")
        return 1

    old_fp = ip._redact_fingerprint(plan.old_fingerprint or "")
    sandboxes = plan.sandboxes

    # ---------------------------------------------------------------- dry-run
    if getattr(args, "dry_run", False):
        console.print(Panel.fit(
            "[bold]CA rotation — dry run[/bold]  (no files will change)",
            border_style="cyan",
        ))
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("", style="bold")
        t.add_column("")
        t.add_row("Live CA", str(plan.ca_crt))
        t.add_row("Old fingerprint", old_fp if plan.old_fingerprint else "[dim](no current CA)[/dim]")
        t.add_row("New subject", "/CN=hermes iron-proxy CA")
        t.add_row("Archive ->", str(plan.archive_path))
        t.add_row("Daemon running", _yn(plan.daemon_running))
        t.add_row("Would restart", _yn(plan.daemon_running and not getattr(args, "no_restart", False)))
        console.print(t)
        if sandboxes:
            console.print()
            console.print("[yellow]Sandboxes to restart afterwards:[/yellow]")
            for cid, name in sandboxes:
                console.print(f"  - {cid[:12]}  {name}")
        else:
            console.print("[dim]No labeled hermes sandboxes detected.[/dim]")
        console.print()
        console.print("[dim]Re-run without --dry-run to apply.[/dim]")
        return 0

    # --------------------------------------------------- interactive confirm
    if sandboxes and not getattr(args, "force", False):
        console.print(
            f"[yellow]⚠[/yellow]  {len(sandboxes)} running hermes sandbox(es) "
            "will keep trusting the OLD CA until restarted:"
        )
        for cid, name in sandboxes:
            console.print(f"  - {cid[:12]}  {name}")
        try:
            answer = input("Proceed with rotation? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            console.print("[dim]Aborted — no changes made.[/dim]")
            return 1

    # ------------------------------------------------------------- do it
    restart = not getattr(args, "no_restart", False)

    # Honour the operator's existing credential-source choice on restart —
    # if they're on ``credential_source: bitwarden`` and we call
    # ``start_proxy()`` bare, the proxy comes up without fetching upstream
    # secrets and silently degrades. Match what ``cmd_start`` does.
    # (Caught by Cursor Bugbot review on the original fork PR.)
    refresh_bw = False
    bw_cfg: Optional[Dict] = None
    if restart:
        cfg = load_config()
        proxy_cfg = cfg.get("proxy") or {}
        bw_cfg = ((cfg.get("secrets") or {}).get("bitwarden")) or None
        refresh_bw = (
            str(proxy_cfg.get("credential_source", "env")).lower() == "bitwarden"
            and bw_cfg is not None
            and bool(bw_cfg.get("enabled"))
        )
        if refresh_bw and bw_cfg is not None:
            bw_cfg = dict(bw_cfg)
            bw_cfg["allow_env_fallback"] = bool(
                proxy_cfg.get("allow_env_fallback", False)
            )

    try:
        result = ip.rotate_ca(
            reason=getattr(args, "reason", None),
            restart=restart,
            refresh_secrets_from_bitwarden=refresh_bw,
            bitwarden_config=bw_cfg,
        )
    except Exception as exc:  # noqa: BLE001 -- user-facing error funnel
        console.print(f"[red]✗ CA rotation failed:[/red] {exc}")
        return 1

    new_fp = ip._redact_fingerprint(result.new_fingerprint or "")
    console.print("[green]✓[/green] CA rotated.")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("", style="bold")
    t.add_column("")
    t.add_row("New CA", str(result.ca_crt))
    t.add_row("New fingerprint", new_fp if result.new_fingerprint else "[dim](unreadable)[/dim]")
    t.add_row("Valid until", result.new_valid_until or "[dim](unknown)[/dim]")
    t.add_row("Archived old ->", str(result.archive_path)
              if result.old_fingerprint else "[dim](no prior CA)[/dim]")
    if result.daemon_running:
        t.add_row("Daemon", "restarted" if restart else "[yellow]left running on OLD key (--no-restart)[/yellow]")
    else:
        t.add_row("Daemon", "[dim]was not running[/dim]")
    console.print(t)

    if sandboxes:
        console.print()
        console.print(
            "[yellow]⚠  Restart these sandboxes so they re-mount the new CA:[/yellow]"
        )
        for cid, name in sandboxes:
            console.print(f"  - {cid[:12]}  {name}")
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
