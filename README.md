# 🚢 shipit

Self-hosted, open-source auto deploy that works with **any language**.
`git push` → webhook → clone → build (Docker) → run → health check → **auto rollback** on failure.

- Zero dependencies (Python 3.11+ stdlib only) + Docker + git
- Auto-detects Node, Python, Go, Rust, Java (Maven/Gradle), PHP, Ruby, .NET, Elixir, static sites
- **Any other language:** put a `Dockerfile` in your repo – it always takes priority
- GitHub / Gitea / Forgejo (HMAC) and GitLab (token) webhooks
- Private repos via token from an env var (never written to disk)
- **Safe deploys:** the new version starts beside the live one and must pass its health check *before* the swap; a bad release never touches production
- Automatic rollback if it fails right after the swap, or manually with `shipit rollback <app>`
- Slack / Discord / Mattermost notifications, deploy history, automatic pruning of old images
- `shipit status <app>` and `shipit logs <app> -f`

## Quick start

```bash
pip install .                      # or: python -m shipit ...
shipit init                    # writes shipit.toml
# edit shipit.toml, then:
shipit detect ./my-project     # preview the generated Dockerfile
shipit deploy myapp            # deploy once, manually
export SHIPIT_SECRET=change-me
shipit serve                   # listen for webhooks on :9000
```

Add a webhook in your repo: URL `http://your-server:9000/hook/myapp`, content type
`application/json`, secret = `SHIPIT_SECRET` (GitLab: put it in "Secret token").
Only pushes to the configured `branch` deploy.

## Config (`shipit.toml`)

| key | meaning |
|---|---|
| `name` | app name (`a-z 0-9 _ . -`), also the container name |
| `repo`, `branch` | git source |
| `port` / `host_port` | container port / published port |
| `health_path` | HTTP path polled after start (`""` = only check it stays running) |
| `start` | start command for generated Dockerfiles |
| `dockerfile` | custom Dockerfile path inside the repo |
| `env_file` | host env file passed to the container |
| `token_env` | env var holding a git token (private https repos) |
| `secret_env` | env var with this app's webhook secret |
| `notify_env` | env var holding a Slack/Discord/Mattermost webhook URL |
| `keep_images` | old images to keep (default 5; current + previous always kept) |
| `build_args` | table of Docker build args |

## Zero-downtime with Caddy

Set `domain` on an app and shipit switches to **blue/green** deploys behind [Caddy](https://caddyserver.com):

```toml
[server]
caddy_admin = "http://127.0.0.1:2019"   # default

[[app]]
name = "myapp"
domain = "app.example.com"   # DNS must point at this server
host_port = 9000             # slots use 127.0.0.1:9000 (blue) and :9001 (green)
```

A release starts the idle slot, waits for its health check, flips the Caddy route, waits
`drain_seconds`, then removes the old slot - no gap, and HTTPS certificates come from Caddy automatically.
If the health check or the Caddy update fails, the live version is untouched.

Requirements: Caddy running with its admin API enabled (the default). **shipit owns the Caddy config** -
it pushes a full config on every release and on `shipit serve` start, so don't hand-edit that Caddy instance.
Apps without `domain` keep the simple mode (brief swap gap).

## Run it as a service (systemd)

```ini
[Service]
Environment=SHIPIT_SECRET=change-me
WorkingDirectory=/opt/shipit
ExecStart=/usr/local/bin/shipit serve
Restart=always
```

Put nginx/Caddy in front for HTTPS and for routing domains to each app's `host_port`.

## Security notes

- The webhook endpoint rejects anything without a valid signature/token.
- Deploying runs `docker build` on code from your repo – only point it at repos you trust.
- Run `shipit serve` behind HTTPS; the host running it controls Docker, so keep it locked down.

## Limitations (v0.1)

Single server, single container per app. Without `domain` there is a brief gap while the port is handed over.
PRs welcome – ideas: multi-server, PR preview deploys, web UI, Traefik/nginx backends.

## Develop

```bash
python -m unittest discover -s tests
```

MIT licensed.
