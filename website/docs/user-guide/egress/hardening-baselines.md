# Host hardening baselines

`hermes egress harden` is a **read-only survey** of the host the egress
proxy runs on. Where [`hermes egress doctor`](./iron-proxy.md) answers *"is
the egress proxy itself healthy?"*, `harden` answers *"is the machine it
runs on locked down?"* — and folds the two iron-proxy runtime signals in
so you see the whole defense-in-depth stack in one table.

```bash
hermes egress harden                 # gaps only, minimal baseline
hermes egress harden --all           # show passing signals too
hermes egress harden --baseline catalin
hermes egress harden --json          # {signals[], baseline, satisfied, missing[]}
```

The command is **informational** — it always exits `0`. It never gates a
deploy or blocks a start. The summary line tells the operator what to fix;
acting on it is a human decision.

## Why baselines

Sandbox-egress isolation and host-perimeter hardening solve *different*
threats, and you want both:

- **Perimeter** (Tailscale / UFW / firewalld / nftables / Cloudflare /
  fail2ban / SSH config) keeps attackers *off* the box: closed ports, no
  SSH password brute-force, no public exposure of the control plane.
- **Sandbox-egress** ([iron-proxy](./iron-proxy.md)) keeps a
  *compromised-from-the-inside* agent from exfiltrating real API
  credentials or reaching cloud metadata (IMDS) — even after a successful
  prompt injection.

A firewall does nothing against a prompt-injected agent that already runs
inside your sandbox and POSTs your `OPENAI_API_KEY` to an attacker. The
egress proxy does nothing against an open SSH port with password auth.
Defense-in-depth means layering them. The three baselines below name three
common layering targets.

## The ten signals

| # | Signal | Probe | Platform |
|---|---|---|---|
| 1 | `tailscale` | `tailscale status --json` → `BackendState=Running` | any |
| 2 | `ufw` | `ufw status verbose` → `Status: active` + default-deny incoming | Linux |
| 3 | `firewalld` | `firewall-cmd --state` → `running` | Linux |
| 4 | `nftables` | `nft list ruleset` → non-empty | Linux |
| 5 | `fail2ban` | `fail2ban-client status` → ≥1 jail | Linux |
| 6 | `ssh-password-auth` | `^PasswordAuthentication no` in `/etc/ssh/sshd_config` | any (needs sshd) |
| 7 | `ssh-root-login` | `^PermitRootLogin (no\|prohibit-password)` in sshd_config | any (needs sshd) |
| 8 | `iron-proxy-enabled` | reuses `get_status()` — CA + `proxy.yaml` present | any |
| 9 | `iron-proxy-running` | reuses `get_status()` — daemon pid alive + listening | any |
| 10 | `docker-seccomp` | `docker info` SecurityOptions includes `seccomp` | any (needs Docker) |

Every probe is **best-effort and graceful**: a missing binary, an
unreadable file, or a non-Linux host yields `skip` (never `fail`). On a
macOS dev box the Linux-only firewall and fail2ban signals skip cleanly
while Tailscale, SSH-config, iron-proxy, and Docker signals still run.

## Minimal baseline

For a **solo developer on a single machine**. Three requirements:

1. **Any one firewall present** — `ufw` *or* `firewalld` *or* `nftables`
   *or* `tailscale` as the network perimeter.
2. **SSH password auth disabled** (`ssh-password-auth` passes).
3. **iron-proxy enabled** (`iron-proxy-enabled` passes).

Why these three: they're the highest-leverage, lowest-friction controls.
One firewall closes the box; key-only SSH defeats the single most common
internet attack (password spray); and the egress proxy is the whole point
of running Hermes with credential isolation. If you do nothing else, do
these.

```bash
hermes egress harden --baseline minimal
# ✓ minimal baseline satisfied
```

## Catalin baseline

Credited to **[@catalinmpit](https://x.com/catalinmpit)**, who publicly
deployed a Hermes agent on a Hetzner VPS behind Tailscale + UFW + Cloudflare
+ fail2ban — a clean, opinionated perimeter that prompted Teknium's request
for a security review of the egress proxy. Catalin described the shape:

> *"I've deployed my Hermes agent on a Hetzner VPS — locked behind
> Tailscale, UFW default-deny, Cloudflare in front, and fail2ban watching
> SSH. The only thing exposed is what I explicitly allow."*

The `catalin` baseline encodes the host-detectable subset of that shape —
five hard requirements:

1. `tailscale` — host is on the private mesh.
2. `ufw` — active, default-deny incoming.
3. `fail2ban` — at least one jail (SSH brute-force mitigation).
4. `ssh-password-auth` — disabled.
5. `iron-proxy-enabled` — sandbox-egress isolation on.

```bash
hermes egress harden --baseline catalin
# ✗ catalin baseline incomplete (missing: fail2ban)
```

Cloudflare-edge protection is part of Catalin's real deployment but isn't
host-detectable from inside the box, so it's tracked as a future
enhancement (see below) rather than a `harden` signal.

## Paranoid baseline

For **production / multi-tenant** hosts: **all ten signals must pass**.
This is the strictest target — every firewall layer present, both SSH
directives locked, fail2ban active, iron-proxy enabled *and* running, and
Docker seccomp confirmed. Use it as a pre-flight gate in your deploy
runbook (read the summary, then decide), not as an automated blocker —
`harden` never gates on its own.

```bash
hermes egress harden --baseline paranoid --all
```

## Composing with iron-proxy

Each layer mitigates a distinct threat. The survey shows you the whole
column at once:

| Threat | Mitigated by |
|---|---|
| Host port scan / unexpected listener | `ufw` / `firewalld` / `nftables` default-deny |
| Public exposure of the control plane | `tailscale` (mesh-only reachability) |
| SSH password brute-force | `ssh-password-auth` off + `fail2ban` |
| Root takeover via SSH | `ssh-root-login` off |
| Sandbox credential exfiltration | `iron-proxy-enabled` (token-swap at the boundary) |
| MITM on upstream API traffic | iron-proxy CA-pinned TLS interception |
| Cloud metadata (IMDS) access from a sandbox | iron-proxy SSRF deny-list (`169.254.0.0/16`) |
| Container syscall abuse | `docker-seccomp` |

The first four rows are **perimeter** (this survey's host signals); the
middle three are **sandbox-egress** (see [iron-proxy](./iron-proxy.md) and
its [`doctor`](./iron-proxy.md) checks); the last is the container runtime.
Neither layer substitutes for the other.

## Future enhancements

Deliberately **out of scope** for the first version of `harden`, tracked as
follow-ups:

- **Hetzner-specific detection** — cloud-provider firewall (Hetzner Cloud
  Firewall) state via metadata API.
- **Cloudflare edge** — verifying the host only accepts traffic from
  Cloudflare CIDR ranges (requires DNS + CIDR caching).
- **iptables baseline** — legacy `iptables -L` parsing distinct from
  nftables.
- **nftables-vs-iptables differentiation** — which backend is authoritative.
- **Docker user-namespace remapping** — `userns-remap` in `docker info`.

These were cut to keep the first cut focused, stdlib-only, and fast.

## See also

- [Egress proxy (iron-proxy)](./iron-proxy.md) — setup, architecture, and
  the complementary `hermes egress doctor` health check.
- [CLI commands reference](../../reference/cli-commands.md) — full
  `hermes egress harden` flag list.
