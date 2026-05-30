# Egress credential-injection proxy (iron-proxy)

When Hermes runs your agent inside a remote terminal sandbox — Docker, Modal, SSH — that sandbox normally holds your real upstream API keys (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, etc.). A prompt-injected agent in that sandbox can `cat ~/.config/openrouter/auth.json` or `printenv | grep -i key` and exfiltrate them.

The egress proxy fixes this: the sandbox holds opaque **proxy tokens**, never the real keys. All outbound traffic from the sandbox routes through a local [iron-proxy](https://github.com/ironsh/iron-proxy) daemon (Apache-2.0, Go) on the host, which terminates TLS and swaps the proxy token for the real credential before forwarding the request upstream. Compromise the sandbox and the attacker walks away with tokens that only work from behind the proxy.

This page covers the Docker backend, which is what v1 ships. Modal, Daytona, and SSH wiring will follow in later releases.

## What it is

- A managed `iron-proxy` subprocess on the host, lazy-installed into `~/.hermes/bin/iron-proxy`
- A local CA at `~/.hermes/proxy/ca.crt` that the sandbox trusts so iron-proxy can MITM TLS and rewrite headers
- A `proxy.yaml` config at `~/.hermes/proxy/proxy.yaml` listing the upstream hosts you allow and the secrets-transform mapping
- A `mappings.json` recording which proxy token corresponds to which real env var

The sandbox gets `HTTPS_PROXY=http://host.docker.internal:9090` plus a set of `HERMES_PROXY_TOKEN_<ENV_NAME>` env vars. The agent code reads those tokens instead of the real API keys. iron-proxy's `secrets` transform matches the token in the `Authorization` header and substitutes the real value sourced from its own environment.

## What it is not

- It is **not** the inbound `hermes proxy` command, which is an OAuth aggregator reverse proxy. Different command (`hermes egress`), different direction.
- It does **not** sit between your local terminal and providers — only between the sandbox and providers.
- It does **not** rewrite credentials for in-process LLM calls the host process makes. Those continue to use your `.env` keys directly. The threat model is the *sandbox*, not the host.

## Quick start

```bash
# 1. Install the iron-proxy binary (pinned version, SHA-256 verified)
hermes egress install

# 2. Run the wizard: generates CA, mints proxy tokens for every provider key
#    in your env, writes proxy.yaml.
hermes egress setup

# 3. Start the proxy daemon
hermes egress start

# 4. Check status
hermes egress status
```

Once running, the Docker terminal backend automatically:

- Mounts `~/.hermes/proxy/ca.crt` into the sandbox at `/etc/ssl/certs/hermes-egress-ca.crt`
- Sets `HTTPS_PROXY`, `HTTP_PROXY`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` to make every common HTTP runtime route through the proxy and trust the CA
- Sets `NODE_OPTIONS=--use-openssl-ca` (appended to whatever you already have in `docker_env.NODE_OPTIONS`) so Node.js routes through the OpenSSL store the other CA-bundle vars control — see [Node.js asymmetric CA caveat](#nodejs-asymmetric-ca-caveat) below for the residual gap
- Adds `--add-host=host.docker.internal:host-gateway` so the sandbox can reach the host-side proxy on Linux (Docker Desktop handles this automatically on macOS/Windows)
- Exports one `HERMES_PROXY_TOKEN_<ENV_NAME>` per minted mapping

## Configuration

The full config lives in `~/.hermes/config.yaml` under the `proxy:` section. Defaults are documented inline; everything is optional.

```yaml
proxy:
  # Master switch. When false the feature is a complete no-op — no
  # binaries downloaded, no docker mounts added, no subprocess started.
  enabled: false

  # Tunnel listener port. Sandboxes hit http://host.docker.internal:<port>.
  tunnel_port: 9090

  # Auto-download the pinned iron-proxy binary on first use.
  auto_install: true

  # Where iron-proxy looks up the real upstream secrets at egress time.
  #   env       — process env (default). Whatever is in your ~/.hermes/.env
  #               at proxy-start time is the source of truth.
  #   bitwarden — refetch from Bitwarden Secrets Manager on each proxy
  #               restart. Rotation in the BW web app propagates without
  #               touching .env. Requires `secrets.bitwarden.enabled: true`.
  credential_source: env

  # When true (default), the Docker backend refuses to start a sandbox if
  # the proxy is enabled but not running. Set to false to fall back to the
  # legacy "real credentials inside the sandbox" posture when the proxy
  # is unavailable.
  enforce_on_docker: true

  # When true, `hermes egress start` refuses to start if LLM-specific
  # non-bearer provider env vars are set (Anthropic native, Azure OpenAI,
  # Gemini) — those bypass the proxy's secrets transform and would leak
  # real credentials into the sandbox.  Defaults to false because the
  # false-positive cost (operator has the env set but doesn't actually
  # use that provider) is higher than the security cost of a warning.
  # See "Uncovered providers" below for the strict tier vs warn tier
  # distinction.
  fail_on_uncovered_providers: false

  # When `credential_source: bitwarden` but the BWS access token /
  # project_id is missing OR the bws fetch returns no values for mapped
  # providers, the daemon raises by default (matches the spirit of "I
  # asked for rotation — don't silently use stale env values").  Set
  # to true to opt back into the legacy host-env fallback — useful for
  # migrations where you want to start switching to BW mode but haven't
  # wired every secret yet.
  allow_env_fallback: false

  # SSRF deny list applied to outbound traffic.  Omit / leave null to
  # use the safe default: loopback (v4 + v6), link-local (incl. cloud
  # metadata IPs at 169.254.169.254), RFC1918, IPv6 ULA, IPv4-mapped-v6,
  # CGNAT, and the RFC2544 benchmark range.  Set to an explicit `[]`
  # to opt out entirely (only sensible in hermetic tests).
  upstream_deny_cidrs: null

  # Extra allowed upstream hosts beyond the bundled defaults.
  # Wildcards (`*.foo.com`) are supported. The defaults cover OpenRouter,
  # OpenAI, Anthropic, Google, xAI, Mistral, Groq, Together, DeepSeek,
  # and Nous Research.
  extra_allowed_hosts: []
```

### Default allowed upstream hosts

```
openrouter.ai           *.openrouter.ai
api.openai.com          api.anthropic.com
generativelanguage.googleapis.com
api.x.ai                api.mistral.ai
api.groq.com            api.together.xyz
api.deepseek.com        inference.nousresearch.com
```

If your agent needs an upstream that isn't on the list — a self-hosted inference endpoint, an extra cloud LLM, an MCP server — add it to `proxy.extra_allowed_hosts`. Wildcards are matched against the full hostname (`*.example.com` matches `api.example.com` and `staging.example.com` but not `example.com` itself).

### Default SSRF deny CIDRs

Applied regardless of allowlist. These ranges are refused by iron-proxy at the network boundary, so a DNS rebinding attack via an allowlisted hostname can't reach IMDS or your internal network:

| CIDR | Purpose |
|---|---|
| `127.0.0.0/8`, `::1/128` | Loopback (v4 + v6) |
| `169.254.0.0/16`, `fe80::/10` | Link-local — **incl. AWS / GCP / Azure IMDS at `169.254.169.254`** |
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | RFC1918 |
| `fc00::/7` | IPv6 ULA |
| `::ffff:0:0/96` | IPv4-mapped IPv6 — closes the dual-stack IMDS bypass |
| `100.64.0.0/10` | RFC6598 CGNAT (used by AWS VPC, K8s pod networks) |
| `198.18.0.0/15` | RFC2544 benchmark range |

To override: set `proxy.upstream_deny_cidrs` to your own list. To opt out entirely (e.g. for a hermetic test that needs to reach a loopback upstream): set it to an empty list `[]`.

### Bind policy

The proxy binds **loopback only** (`127.0.0.1:<tunnel_port>`). It does NOT bind `0.0.0.0`. This means:

- A LAN peer with a leaked proxy token cannot use it — the proxy is unreachable from the network.
- Containers reach the proxy via `host.docker.internal:9090`, which Docker maps to the host gateway via `--add-host=host.docker.internal:host-gateway` on Linux. On macOS / Windows Docker Desktop, Desktop manages the gateway itself.

iron-proxy v0.39 only supports a single bind per daemon process — earlier drafts of this integration emitted a plural `http_listens` list with the docker bridge IP appended for direct sandbox-to-bridge connectivity, but v0.39's YAML parser rejects that field. The `host.docker.internal -> host-gateway` mapping that Docker provides is sufficient: containers resolve the hostname to the bridge IP, then connect TO the host's loopback bind through it.

We also pin `metrics.listen: 127.0.0.1:0` so the daemon's built-in metrics server gets an ephemeral loopback port instead of its default `:9090` — otherwise it would fight `tunnel_port: 9090` for the same socket and the daemon would refuse to start with "address already in use".

If a hostile `ip` shim earlier on PATH had been able to inject a non-private IPv4 here (`0.0.0.0`, a public address, multicast, link-local, etc.) the loopback fallback still applies — we never bind anything we couldn't validate via `ipaddress.IPv4Address` + `is_*` checks.

### Anthropic native

Anthropic's native API (`api.anthropic.com`) authenticates with an `x-api-key: <key>` header instead of `Authorization: Bearer <key>`. By default the egress proxy only swaps the Bearer header, so a sandbox using the native Anthropic SDK would hold the real `ANTHROPIC_API_KEY`.

Opt in with the `--with-anthropic` flag at setup time:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
hermes egress setup --with-anthropic
hermes egress start
```

What this does:

- Mints a proxy token + `TokenMapping` for `ANTHROPIC_API_KEY` scoped to `api.anthropic.com`.
- Emits a second `secrets` rule with `match_headers: ["x-api-key"]` (the Bearer rule keeps `match_headers: ["Authorization"]`). The x-api-key rule does **not** match query params — Anthropic has no bearer-query form.
- Drops `ANTHROPIC_API_KEY` from the uncovered/blocked sets, so `fail_on_uncovered_providers: true` no longer refuses to start on its account.
- Persists `proxy.with_anthropic: true` in `config.yaml` so `hermes egress start` and `hermes egress doctor` know the key is covered.

**Why it's opt-in (default off):** an `ANTHROPIC_API_KEY` in your env might be routed through OpenRouter (`openrouter.ai`, Bearer) rather than hitting `api.anthropic.com` directly. Auto-wiring an x-api-key swap to `api.anthropic.com` in that case would be a no-op at best and confusing at worst, so you must explicitly say you want the native path covered.

The Docker backend injects `HERMES_PROXY_TOKEN_ANTHROPIC_API_KEY` into sandboxes the same way it does for Bearer providers — the `TokenMapping` data model is auth-header-agnostic.

## Uncovered providers

iron-proxy's `secrets` transform handles `Authorization: Bearer` headers, plus `x-api-key` for Anthropic native when you opt in (see [Anthropic native](#anthropic-native)). Other providers using SigV4, AAD tokens, or custom signatures cannot be proxied. If their env vars are present, the sandbox holds **real credentials** for those providers and the egress isolation guarantee is incomplete for them.

The wizard and `hermes egress status` always surface uncovered providers in your env. There are two tiers:

### Strict tier — refuses start when `fail_on_uncovered_providers: true`

| Env var | Provider | Reason |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | Azure OpenAI | api-key header + optional AAD |
| `GEMINI_API_KEY` | Google AI Studio (Gemini) | x-goog-api-key |

These are LLM-specific names. An operator who has them set is using those providers; a bypass is a real isolation failure.

> **Anthropic native is now supported** via `hermes egress setup --with-anthropic`. When you opt in, `ANTHROPIC_API_KEY` gets a real `x-api-key` swap rule and is no longer flagged as uncovered or blocked. See [Anthropic native](#anthropic-native) below. Without the flag, `ANTHROPIC_API_KEY` is still treated as warn-only (the key may be used via OpenRouter rather than `api.anthropic.com`).

### Warn-only tier — surfaced but never blocks

| Env var | Provider | Reason |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS Bedrock / SageMaker | SigV4-signed |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP Vertex AI | gcloud OAuth |
| `GOOGLE_API_KEY` | Google AI Studio | x-goog-api-key OR query param |

These env vars are present on most developer laptops for unrelated tooling (terraform, gcloud, aws CLI, ECR push). They surface as warnings in the wizard + `status` output but don't refuse-start.

### Operator playbook

If `hermes egress start` refuses because of a strict-tier env var you don't actually use:

```bash
unset ANTHROPIC_API_KEY   # or whichever one is flagged
hermes egress start
```

If you DO use that provider but accept the isolation gap:

```yaml
# config.yaml
proxy:
  fail_on_uncovered_providers: false  # default
```

Either way, the warning persists in `hermes egress status` until you remove the env var.

## Bitwarden integration

If you already use Bitwarden Secrets Manager via [`hermes secrets bitwarden setup`](../secrets/bitwarden), the egress proxy can pull real credentials from there instead of `os.environ`:

```bash
hermes egress setup --from-bitwarden
```

This sets `proxy.credential_source: bitwarden` and discovers provider env names from your BW project.

### Rotation semantics

When `credential_source: bitwarden`, the iron-proxy daemon refetches secrets from BWS via `bws secret list <project_id>` **every time it starts**. So the rotation flow is:

1. Rotate a key in the Bitwarden web app.
2. `hermes egress stop && hermes egress start` on the host.
3. Sandboxes started after that point swap proxy tokens for the new value.

No `.env` edits. No Hermes restart on the host. The proxy daemon is the only thing that touches the new value — your host process and `os.environ` are untouched.

### Fail-loud at start

When `credential_source: bitwarden`, `hermes egress start` pre-checks at the wizard layer AND `_build_proxy_subprocess_env` re-checks at the daemon layer:

- BWS access token env var is unset → refuse to start with a hint to `unset` and re-run, or `hermes egress setup --no-bitwarden` to switch back to env mode
- `secrets.bitwarden.project_id` is empty → refuse to start with a hint to run `hermes secrets bitwarden setup`
- `bws secret list` returns no values for one or more mapped providers → refuse to start, listing the missing names

This is intentional. Falling back to host env in BW mode reintroduces exactly the staleness bug the BW path is meant to defeat (operator picked BW for the rotation guarantee; silent fallback breaks that guarantee).

The `proxy.allow_env_fallback: true` config flag opts back in to the legacy "silently fall back to host env if BWS is unreachable" behavior for migration scenarios. Use it when you're moving secrets into BW one at a time and want the daemon to start with whichever values are available.

### Switching credential source

| From | To | Command |
|---|---|---|
| env | bitwarden | `hermes egress setup --from-bitwarden` |
| bitwarden | env | `hermes egress setup --no-bitwarden` |

**Re-running `hermes egress setup` WITHOUT either flag preserves the existing `credential_source`** — the wizard refuses to silently downgrade you back to env. This matters because once you've configured bitwarden mode, the rotation guarantee is what you signed up for; you have to explicitly say "I want env again" to change it.

## Slash commands

The CLI subcommand tree:

```
hermes egress install                  # download the pinned iron-proxy binary
hermes egress install --force          # re-download even if a managed copy exists

hermes egress setup                    # interactive wizard
hermes egress setup --tunnel-port N    # override the tunnel listener port
hermes egress setup --from-bitwarden   # use BWS as credential source (fail-loud)
hermes egress setup --no-bitwarden     # explicitly switch back to env mode
hermes egress setup --rotate-tokens    # mint fresh tokens for every provider
                                       #   (default preserves existing)
hermes egress setup --with-anthropic   # add an x-api-key swap rule for
                                       #   ANTHROPIC_API_KEY (api.anthropic.com)

hermes egress doctor                   # end-to-end health check (see below)
hermes egress doctor --json            # machine-readable checks[] + summary{}
hermes egress doctor --no-network      # skip reachability + token-swap probes
hermes egress doctor --check ca --check binary   # run only named checks

hermes egress audit tail [-n N] [-f]   # last N audit lines, optionally follow
hermes egress audit grep PAT --since 1h  # regex + time-window filter
hermes egress audit stats --since 1h   # status distribution + anomalies
hermes egress audit export --format csv --out a.csv  # SIEM export

hermes egress start                    # spawn the managed proxy daemon
hermes egress stop                     # SIGTERM (then SIGKILL after 5s grace)

hermes egress status                   # binary + config + pid + listening state + mappings
hermes egress status --show-tokens     # print proxy tokens in full
                                       #   (default: redacted prefix + suffix only)

hermes egress disable                  # flip proxy.enabled = false
                                       #   (does not stop a running proxy)

hermes egress config                   # print the path to proxy.yaml for debugging
```

### Token rotation

By default, `hermes egress setup` **preserves** proxy tokens for providers that already have them. Adding a new provider mints a fresh token only for the new one; existing tokens are unchanged. This avoids 401-ing running sandboxes when you re-run the wizard.

`--rotate-tokens` rolls every token:

```bash
hermes egress setup --rotate-tokens
```

When there are existing tokens AND stdin is a tty, the wizard prompts for confirmation:

```
⚠  --rotate-tokens will invalidate proxy tokens in every running
   Hermes sandbox.  They will start 401-ing against upstreams until restarted.
Type 'rotate' to confirm:
```

Non-tty invocations (CI, scripts) skip the prompt — the flag is treated as deliberate. Before any overwrite the current `mappings.json` is copied to a timestamped sibling so manual recovery is possible:

```
backup: ~/.hermes/proxy/mappings.json.rotated-20260524T143012
```

**Caveat:** rotating tokens DOES NOT automatically restart iron-proxy. The running daemon still has the old mappings in memory (and the old YAML). After `--rotate-tokens`:

```bash
hermes egress stop && hermes egress start
```

Containers already running hold the old tokens and will need to be restarted to pick up the new ones.

## Health checks

`hermes egress doctor` runs a battery of read-only checks against your egress setup and prints actionable fix-it hints — run it **before** deploying to confirm everything is wired, and **after** a problem to pinpoint what broke. It never starts, stops, or rewrites anything.

The checks, in order:

| Check | What it verifies |
|---|---|
| `binary` | iron-proxy installed, executable, version matches the pin |
| `ca` | `ca.crt` present, valid PEM, not expired (warn < 365 days, fail < 30) |
| `config` | `proxy.yaml` present and parses as YAML |
| `mappings` | `mappings.json` present, parses, and every `real_env_name` is set (or BW-declared) |
| `daemon` | pidfile present and the PID is a live iron-proxy process |
| `listening` | something accepts on `127.0.0.1:<tunnel_port>` (reports bind addrs via `lsof`) |
| `reachability` | a 1-shot HTTPS HEAD through the proxy to each allowlisted host returns 200/401/403/404 |
| `token-swap` | a HEAD with the proxy token returns 401 (swap fired, reached upstream) and **not** 403 (proxy refused) |
| `uncovered` | warns about provider env vars the proxy can't cover (Anthropic native excluded when `--with-anthropic` was used) |
| `docker-dns` | if Docker is installed, `host.docker.internal` resolves inside a one-shot container |
| `ssrf-deny` | the effective `upstream_deny_cidrs` still includes the IMDS range `169.254.0.0/16` |

Flags:

- `--json` — emit `{checks: [{name, status, detail, fix}], summary: {pass, warn, fail, skip}}`. Exit `0` if there are no failures (warns/skips are tolerated), `1` otherwise.
- `--check <name>` — run only the named check (repeatable, scriptable).
- `--no-network` — skip the `reachability` and `token-swap` probes (CI / hermetic environments).

Failure-message credentials are redacted to the last 4 characters at most — a broken token-swap never echoes a real key.

### Example: all healthy

```text
   Check         Detail
 ✓ binary        ~/.hermes/bin/iron-proxy (iron-proxy v0.39.0)
 ✓ ca            valid until 2035-06-01 (3287d)
 ✓ config        valid YAML at ~/.hermes/proxy/proxy.yaml
 ✓ mappings      3 mapping(s), all env present
 ✓ daemon        running as pid 48213
 ✓ listening     listening on 127.0.0.1:9090
 ✓ reachability  all 9 allowlisted host(s) returned 200/401/403/404
 ✓ token-swap    swap fired for OPENROUTER_API_KEY (upstream openrouter.ai returned 401)
 ✓ uncovered     no uncovered provider env vars
 ✓ docker-dns    host.docker.internal resolves inside containers
 ✓ ssrf-deny     IMDS + loopback denied (11 CIDR(s) in deny list)

Summary  11 pass  0 warn  0 fail  0 skip
✓ egress proxy looks healthy.
```

### Example: a failure with a fix

```text
   Check         Detail
 ✓ binary        ~/.hermes/bin/iron-proxy (iron-proxy v0.39.0)
 ✗ daemon        no pidfile / daemon not running
 ✗ listening     nothing accepting on 127.0.0.1:9090 (Connection refused)
 ...

Suggested fixes
  daemon: start it with `hermes egress start`
  listening: start the daemon (`hermes egress start`) or check the tunnel port

Summary  6 pass  0 warn  2 fail  2 skip
```

### Example: a warning (CA rotation reminder)

```text
 ⚠ ca            CA expires in 142d (2026-10-19)

Suggested fixes
  ca: plan a CA rotation within the year
```

## Audit log

`hermes egress audit` reads the structured audit stream at `~/.hermes/proxy/audit.log` (JSON Lines: `{ts, level, request_id, upstream_host, method, path, status, sandbox_id?, transform?}` per line). Lines that don't parse as JSON are still displayed/grepped on a best-effort basis.

### `audit tail`

```text
$ hermes egress audit tail -n 3
 ts                    method  upstream_host    path           status  sandbox_id
 2026-05-30T14:02:11Z  POST    api.openai.com   /v1/chat/...   200     sbx-91af
 2026-05-30T14:02:14Z  POST    openrouter.ai    /api/v1/chat   200     sbx-91af
 2026-05-30T14:02:19Z  GET     evil.test        /              403     sbx-3c20
```

Add `-f` to follow new lines (250ms polling loop, no extra dependency; handles log rotation). Add `--json` for raw JSON-Lines.

### `audit grep`

```text
$ hermes egress audit grep '403' --since 1h
 ts                    method  upstream_host  path  status  sandbox_id
 2026-05-30T14:02:19Z  GET     evil.test      /     403     sbx-3c20
```

`PATTERN` is a regex matched against the raw line. `--since` accepts `30m`/`2h`/`7d`/`1w`, `today`, or an ISO-8601 date/datetime. An unparseable `--since` exits non-zero with a clear message.

### `audit stats`

```text
$ hermes egress audit stats --since 1h
Audit stats  (412 events, 0 unparsed)
 Status  Count
 200     389
 401     6
 403     17
403 denied: 17

Top upstream hosts
 Host            Requests
 api.openai.com  201
 openrouter.ai   188
 evil.test       17

⚠ First-time upstream hosts in window (not seen in prior 24h):
  - evil.test

⚠ Hosts with >5% 403 rate (misconfig or probing):
  - evil.test: 100%
```

Anomaly detection surfaces two signals: **first-time upstream hosts** (a host appearing in the window but not in the prior 24h — catches a quiet DNS-rebind to a newly-allowlisted host) and **hosts with a 403 rate above 5%** (likely misconfiguration or allowlist probing). `--json` emits `{stats, anomalies}`.

### `audit export`

```bash
hermes egress audit export --format json            # to stdout (default)
hermes egress audit export --format csv --out a.csv  # to a file
hermes egress audit export --format json --since today
```

Bulk export for SIEM ingest. The synthetic parse fields (`_raw`, `_ts`) are stripped from the output; unparseable lines export as `{"raw": "..."}`.

## State directory layout

Everything iron-proxy maintains lives in `~/.hermes/proxy/`:

| Path | Mode | Purpose |
|---|---|---|
| `~/.hermes/proxy/` (dir) | `0o700` | Owned + traversable by you only |
| `ca.crt` | `0o644` | Public CA cert distributed into sandboxes |
| `ca.key` | `0o600` | CA signing key — never leaves the host |
| `proxy.yaml` | `0o600` | iron-proxy config; rewritten every `setup` |
| `mappings.json` | `0o600` | Sandbox proxy token → upstream env var |
| `mappings.json.rotated-*` | `0o600` | Backups created by `--rotate-tokens` |
| `iron-proxy.pid` | `0o600` | PID of the running daemon |
| `iron-proxy.nonce` | `0o600` | Per-start nonce for PID-recycle defense |
| `iron-proxy.log` | `0o600` | Daemon stdout/stderr — **includes per-request records on v0.39** |
| `audit.log` | `0o600` | Reserved for the dedicated per-request audit stream on future binary versions; pre-created so the privacy contract holds when upstream wires it in |

The CA private key is the most sensitive file. It's created with `0o600` from the first byte (no umask-window TOCTOU) and `O_NOFOLLOW` so a same-uid attacker can't redirect it via a planted symlink. The pidfile, nonce file, daemon log, and audit log get the same treatment.

### Logging on iron-proxy v0.39

On the currently pinned binary version (**v0.39.0**) iron-proxy writes ALL output — daemon-level diagnostics AND per-request records — to **`~/.hermes/proxy/iron-proxy.log`**. v0.39's `config.Log` struct doesn't have a separate `audit_path` field, so we can't route per-request records to a dedicated stream there.

We still pre-create `~/.hermes/proxy/audit.log` at `0o600` with `O_NOFOLLOW` because:

1. It serves as a stable logrotate / fluent-bit / monitoring target — operators can wire downstream tooling to that path today, and when we bump the pinned version to one that supports `log.audit_path`, the records will start flowing without any operator-side reconfiguration.
2. The 0o600-from-first-byte guarantee defends against the upstream-fix-day where v0.40+ creates the file under its default umask if it doesn't already exist.

Until that version bump lands, treat `iron-proxy.log` as the source of truth for both audiences:

- Daemon-level events (startup banner, bind errors, shutdown reason, transform errors). Operations + troubleshooting.
- Per-request records (CONNECT to allowlisted upstream, secret swap fired, allowlist denial). Forensics + compliance.

Both files are appended to across restarts. Rotate them with logrotate if you care about disk usage on long-lived hosts.

## How it works

```
┌──────────────┐                ┌──────────────┐                ┌─────────────┐
│ Docker       │ CONNECT /     │ iron-proxy    │ HTTPS w/       │ OpenRouter  │
│ sandbox      ├──────────────▶│ (host:9090)   ├───────────────▶│ / OpenAI /  │
│              │ HTTP forward  │               │ real API key   │ Anthropic …  │
│ has:         │ w/ proxy tok  │ mints leaf    │                │             │
│ - proxy tok  │ in Auth hdr   │ cert from CA  │                │             │
│ - CA cert    │               │ matches token │                │             │
│ - HTTPS_PROXY│               │ swaps secret  │                │             │
└──────────────┘               └──────────────┘                └─────────────┘
                                       │
                                       │ daemon + per-request log (combined on v0.39)
                                       ▼
                              ~/.hermes/proxy/iron-proxy.log
                              (~/.hermes/proxy/audit.log reserved for v0.40+ split stream)
```

1. Sandbox makes an HTTPS request, e.g. `POST https://openrouter.ai/v1/chat/completions` with `Authorization: Bearer hermes-proxy-openrouter-…` (the proxy token, not the real key).
2. Because `HTTPS_PROXY` is set, the request goes to iron-proxy as a CONNECT tunnel.
3. iron-proxy checks the allowlist. `openrouter.ai` is allowed.
4. iron-proxy mints a leaf cert signed by our CA for `openrouter.ai`, terminates the TLS connection, inspects the request.
5. The `secrets` transform matches the proxy-token string in the `Authorization` header and substitutes the real `OPENROUTER_API_KEY` value, sourced from iron-proxy's own environment.
6. Request is re-encrypted and forwarded to OpenRouter.
7. The request is logged to `~/.hermes/proxy/iron-proxy.log` on v0.39. When the pinned binary version supports the split stream (v0.40+), per-request records will flow to `~/.hermes/proxy/audit.log` and daemon-level diagnostics will stay in `iron-proxy.log`. See [Logging on iron-proxy v0.39](#logging-on-iron-proxy-v039).

A request to a non-allowlisted host (e.g. `https://attacker.example.com/leak?key=...`) is rejected with HTTP 403 before any bytes leave the host. The denial is recorded in `iron-proxy.log` with the upstream host and the source sandbox.

### CA distribution into the sandbox

When the Docker backend starts a container with `proxy.enabled: true` and the daemon is listening, it adds these arguments to `docker run`:

| Arg | Purpose |
|---|---|
| `-v ~/.hermes/proxy/ca.crt:/etc/ssl/certs/hermes-egress-ca.crt:ro` | Read-only mount of the CA |
| `-e HTTPS_PROXY=http://host.docker.internal:9090` | Python httpx / curl / go default transport / Node fetch |
| `-e HTTP_PROXY=…` | curl + wget for plain HTTP (rare in modern stacks) |
| `-e NO_PROXY=127.0.0.1,localhost,::1` | Loopback dev servers inside the sandbox bypass the proxy |
| `-e REQUESTS_CA_BUNDLE=…ca.crt` | Python `requests` |
| `-e SSL_CERT_FILE=…ca.crt` | Python `ssl` module / OpenSSL — **replaces** the system store |
| `-e CURL_CA_BUNDLE=…ca.crt` | curl — **replaces** the system store |
| `-e NODE_EXTRA_CA_CERTS=…ca.crt` | Node.js — **adds** to the system store |
| `-e NODE_OPTIONS="<your value> --use-openssl-ca"` | Node.js — route through OpenSSL store (appended; your `--max-old-space-size` etc. are preserved) |
| `-e HERMES_EGRESS_PROXY=1` | Sentinel the agent can read to know it's proxy-aware |
| `-e HERMES_PROXY_TOKEN_<NAME>=…` | One per mapping; the sandbox uses these instead of real keys |
| `--add-host=host.docker.internal:host-gateway` | Linux-only; Docker Desktop maps it automatically |

#### Node.js asymmetric CA caveat

`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE` **replace** the system CA store inside the sandbox. `NODE_EXTRA_CA_CERTS` **adds** to it. A Node.js process inside the sandbox could in principle bypass the proxy by opening a raw `net.Socket` and starting its own TLS handshake — the system CA store would still trust real upstream certs, so the request would succeed where Python / curl would fail validation.

`NODE_OPTIONS=--use-openssl-ca` is appended to whatever you already have in `docker_env.NODE_OPTIONS`. This forces Node through the OpenSSL store that `SSL_CERT_FILE` controls, narrowing the asymmetry. It does NOT cover code that explicitly passes its own `ca` option to `tls.connect()` or `https.request()`, but it closes the easy case.

This is a known v1 limitation. Track [github.com/ironsh/iron-proxy/issues](https://github.com/ironsh/iron-proxy/issues) for an upstream resolution; in the meantime, do not run untrusted Node code that opens raw sockets in a sandbox you're depending on egress isolation for.

### docker\_env collisions

If you set proxy-controlling env vars in your `docker_env:` config block (rare but possible), Hermes refuses to start the sandbox when `enforce_on_docker: true` is set. This includes both:

- Egress-control vars: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`
- Real provider env vars: every name in `mappings.json` (e.g. `OPENROUTER_API_KEY`, `OPENAI_API_KEY`)

Example error:

```
docker_env in config.yaml overrides egress-proxy variables
['HTTPS_PROXY', 'OPENROUTER_API_KEY']; enforce_on_docker is enabled.
Remove these keys from docker_env or disable enforce_on_docker to
opt out of egress isolation.
```

With `enforce_on_docker: false` the same situation surfaces as a warning and your `docker_env` values win — useful for migrations or testing, but you're explicitly opting OUT of the isolation guarantee.

## PID and nonce defense

The daemon's pidfile is written with `O_EXCL` + `O_NOFOLLOW` + ownership check. Concurrent `hermes egress start` calls produce one of two outcomes:

- The existing pidfile points at a live iron-proxy → second start refuses with "another start in progress" + a hint to run `hermes egress stop`
- The existing pidfile is stale (crashed daemon) → second start unlinks it and retries once

Beyond that, every `start_proxy` plants a fresh random nonce in two places:

- `HERMES_IRON_PROXY_NONCE=<nonce>` in the daemon's env
- `~/.hermes/proxy/iron-proxy.nonce` (0o600 sibling of the pidfile)

When `hermes egress stop` (or any other `_pid_alive` check) wants to confirm a PID still refers to *our* daemon — not an unrelated process that was assigned the same PID after iron-proxy crashed — it reads `/proc/<pid>/environ` and looks for the nonce. The on-disk copy is what makes this work across CLI invocations (the in-memory `_proxy_nonce` is per-process and resets on every `hermes` invocation).

If the nonce check fails, the code falls back to matching `argv[0]` basename against `iron-proxy`. `stop_proxy` additionally captures `/proc/<pid>/stat` starttime before SIGTERM and re-verifies after the 5s grace window — if starttime drifted, the PID was recycled mid-wait and SIGKILL is suppressed with a warning.

## Security model

**What this protects against:**

- Prompt-injected agent in a Docker sandbox reading `printenv` / credential files and exfiltrating real keys.
- Compromised dependency in the sandbox phoning home to an arbitrary host — default-deny allowlist blocks unknown destinations.
- Agent dialing cloud metadata endpoints (`169.254.169.254`) — iron-proxy denies these by default via `upstream_deny_cidrs`, including the IPv4-mapped-v6 form `::ffff:169.254.169.254`.
- DNS rebinding through an allowlisted hostname to a private IP — the deny CIDRs are checked at connect time, not at allowlist time.
- Same-uid local processes reading the iron-proxy daemon's env to scrape secrets — only the env var names referenced by mappings are forwarded, not the full host env.
- A LAN peer with a leaked sandbox proxy token spending your API quota — the proxy binds loopback only, never `0.0.0.0` (containers reach it via `host.docker.internal -> host-gateway`).

**What it does NOT protect against:**

- A compromised host process. If the agent process itself is compromised, real keys in the host's `~/.hermes/.env` are exposed regardless. This is a defense-in-depth feature for *sandbox* compromise, not host compromise.
- Sandbox processes that bypass `HTTPS_PROXY` by using a raw socket. The proxy can't intercept what doesn't route to it. Node.js is partially mitigated via `NODE_OPTIONS=--use-openssl-ca` (see caveat above).
- Allowlisted-host data exfiltration. If `api.openai.com` is allowed, an agent could embed exfil data in a request body to that host. The daemon log captures the request happened but doesn't prevent it.
- Uncovered providers (Anthropic native, AWS Bedrock, Azure OpenAI, Gemini). Their env vars stay in the sandbox; if you enable them, those credentials bypass the proxy entirely. See [Uncovered providers](#uncovered-providers).
- iron-proxy in-memory secret zeroisation. The Go binary holds swapped-in real credentials in process memory; a core-dump or `/proc/<pid>/mem` read from a same-uid attacker would expose them. Out of scope for this layer.

## Failure modes

- **Binary not installed, `auto_install: true`** — first `hermes egress setup` or `hermes egress start` downloads it. SHA-256 verified against the upstream `checksums.txt`.
- **Binary not installed, `auto_install: false`** — `start` fails with a clear message pointing to manual install.
- **`enabled: true` but proxy not running** — with `enforce_on_docker: true` (default), Docker sandbox creation refuses to start with an explanatory error. With `enforce: false`, it falls back to direct outbound with real creds and logs a warning.
- **Port collision** — iron-proxy exits immediately; `hermes egress start` reports the last 20 log lines and fails with non-zero exit.
- **Upstream-host denied** — sandbox gets HTTP 403 from the proxy with a body explaining which host wasn't allowed. The agent sees the error and reports it.
- **Cloud metadata IP (169.254.169.254) requested** — refused by `upstream_deny_cidrs` regardless of allowlist.
- **Strict-tier uncovered provider env var set** — `hermes egress start` refuses with a list of the offending env vars and the `proxy.fail_on_uncovered_providers: false` escape hatch.
- **`docker_env` collides with a proxy-controlling var (enforce on)** — sandbox creation refuses with the names of the colliding keys.
- **BWS access token missing in `credential_source: bitwarden`** — `hermes egress start` refuses with `--no-bitwarden` as the recovery hint.
- **iron-proxy doesn't bind within 5 seconds** — process is killed, pidfile unlinked, error names the port + tail of `iron-proxy.log`.
- **Concurrent `hermes egress start` calls** — second call refuses with "another start in progress" if the first's daemon is up; otherwise the second unlinks the stale pidfile and proceeds.

## Troubleshooting

### "Refusing to start: BWS_ACCESS_TOKEN is not set"

You enabled `credential_source: bitwarden` but the access-token env var isn't in your shell. Either:

```bash
export BWS_ACCESS_TOKEN=…   # one-shot
hermes egress start
```

Or move it into `~/.hermes/.env`. Or switch back to env mode:

```bash
hermes egress setup --no-bitwarden
```

### "Refusing to start: provider env vars present that bypass the proxy"

You have `fail_on_uncovered_providers: true` AND one of `ANTHROPIC_API_KEY` / `AZURE_OPENAI_API_KEY` / `GEMINI_API_KEY` is set in your env. Either unset the offending var, or flip the config flag back to `false` (default) if you accept the isolation gap.

### "iron-proxy exited immediately"

Look at the last 20 lines of `~/.hermes/proxy/iron-proxy.log`. Common causes:

- Port already in use → change `proxy.tunnel_port` or kill whatever else owns 9090
- Invalid `proxy.yaml` → run `hermes egress setup` to regenerate
- CA cert / key permissions wrong → `chmod 0o600 ~/.hermes/proxy/ca.key`

### "iron-proxy did not bind 127.0.0.1:9090 within 5s"

The daemon started but never bound the listener. Usually means the binary is wedged or doing something expensive at startup. Check `~/.hermes/proxy/iron-proxy.log`. The orphan process is killed automatically and the pidfile cleaned up so you can just retry `hermes egress start`.

### Sandbox sees `HTTP 403` from the proxy

The agent inside the sandbox tried to hit a host that isn't in `proxy.extra_allowed_hosts`. The 403 body explains which host. If you want to allow it, add to your config:

```yaml
proxy:
  extra_allowed_hosts:
    - api.example.com
    - "*.staging.example.com"
```

Then `hermes egress setup` (to regenerate `proxy.yaml`) and `hermes egress stop && hermes egress start`.

### Sandbox sees SSL verification errors

Either the CA isn't mounted in the sandbox (rare; the docker backend does this automatically when `proxy.enabled: true`), or your image's HTTP client is reading from a non-standard env var.

```bash
# Inside the sandbox:
cat /etc/ssl/certs/hermes-egress-ca.crt | head -1
# Should print: -----BEGIN CERTIFICATE-----
env | grep -E "^(REQUESTS|CURL|SSL|NODE).*CA"
# Should list all four CA-bundle env vars pointing at /etc/ssl/certs/hermes-egress-ca.crt
```

If the cert isn't there, check that `proxy.enabled: true` AND `hermes egress status` shows `Listening yes`. If the env vars are missing, the sandbox image might be running an entrypoint that strips them — check your `docker_env` config.

### Sandbox sees `HTTP 401` from upstreams

Two common causes:

1. **Token-clobber on re-setup.** You ran `hermes egress setup --rotate-tokens` (or rotated tokens some other way) and the running sandboxes still hold the old tokens. Restart the sandboxes.
2. **Bitwarden refresh failed silently.** Should not happen with the new fail-loud behavior, but if you have `proxy.allow_env_fallback: true` set, the daemon may have started with stale env values. Check the daemon's environment (`/proc/<iron-proxy-pid>/environ`) for the expected `OPENROUTER_API_KEY` etc.

### "Address in use" after the parent process died

The parent Hermes process died during `hermes egress start` (Ctrl-C during the listening probe, OOM, panic). The new fix-up logic writes the pidfile immediately after `Popen` so the orphan is recoverable:

```bash
hermes egress stop   # finds the orphan via the pidfile, kills it
hermes egress start
```

If `hermes egress stop` says "iron-proxy was not running" but you can still see the daemon in `ps`, the pidfile got out of sync. Manual recovery:

```bash
pkill -TERM iron-proxy
rm -f ~/.hermes/proxy/iron-proxy.pid ~/.hermes/proxy/iron-proxy.nonce
hermes egress start
```

### Inspecting per-request behavior

On the pinned binary version (**v0.39**) both daemon-level events and per-request records land in `~/.hermes/proxy/iron-proxy.log`. The format is line-delimited JSON. Grep for a specific upstream:

```bash
grep '"upstream":"openrouter.ai"' ~/.hermes/proxy/iron-proxy.log | tail -20
```

Or watch in real-time:

```bash
tail -f ~/.hermes/proxy/iron-proxy.log | jq
```

When the pinned version moves to v0.40+ (which adds `log.audit_path`), per-request records will move to `~/.hermes/proxy/audit.log` and `iron-proxy.log` will hold only daemon-level events. The file at `audit.log` is pre-created today at `0o600` so any logrotate / monitoring tooling you wire to that path keeps working through the version bump without operator-side reconfig.

## Limitations (v1)

- Docker backend only. Modal, Daytona, and SSH wiring will follow in separate PRs.
- Only bearer-token providers (OpenRouter, OpenAI, Anthropic-via-OR, etc.) are wired through the `secrets` transform out of the box. Providers with custom auth (x-api-key, query params, signatures) bypass the proxy entirely — see [Uncovered providers](#uncovered-providers).
- No native Windows binary upstream. Run on Linux / macOS / WSL.
- The CA is a 10-year self-signed cert on first generation. Rotation requires `openssl genrsa ...` by hand (or wait for a follow-up that adds `hermes egress rotate-ca`).
- Token rotation does not auto-restart the daemon; after `--rotate-tokens` you must `hermes egress stop && hermes egress start` and then restart running sandboxes.
- iron-proxy in-memory secret zeroisation is upstream-controlled. Same-uid attackers with `/proc/<pid>/mem` read access can read swapped-in secrets from the daemon's memory.
- iron-proxy v0.39 only supports a **single bind per daemon** and combines daemon + per-request records into a single log stream. The integration is designed to upgrade cleanly: the moment upstream adds `proxy.http_listens` (plural) and `log.audit_path`, both wire in automatically without changing operator configs.

## See also

- Upstream project: [github.com/ironsh/iron-proxy](https://github.com/ironsh/iron-proxy)
- Upstream docs: [docs.iron.sh](https://docs.iron.sh/)
- Bitwarden integration: [`hermes secrets bitwarden`](../secrets/bitwarden)
- Hermes Docker terminal backend: [Docker](../docker)
- Developer / contributor reference: [Egress proxy internals](../../developer-guide/egress-internals)
