# 🚢 shipit

[![CI](https://github.com/desnarong/shipit/actions/workflows/ci.yml/badge.svg)](https://github.com/desnarong/shipit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/shipit-deploy)](https://pypi.org/project/shipit-deploy/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Self-hosted, open-source auto deploy that works with **any language**.
`git push` → webhook → clone → build (Docker) → run → health check → **auto rollback** on failure.

- Zero dependencies (Python 3.11+ stdlib only) + Docker + git
- Auto-detects Node, Python, Go, Rust, Java (Maven/Gradle), PHP, Ruby, .NET, Elixir, static sites
- Python start command is inferred for Django (gunicorn), FastAPI (uvicorn) and Flask (gunicorn)
- **Any other language:** put a `Dockerfile` in your repo – it always takes priority
- GitHub / Gitea / Forgejo (HMAC) and GitLab (token) webhooks
- Private repos via token from an env var (never written to disk)
- **Safe deploys:** the new version starts beside the live one and must pass its health check *before* the swap; a bad release never touches production
- Automatic rollback if it fails right after the swap, or manually with `shipit rollback <app>`
- Slack / Discord / Mattermost notifications, deploy history, automatic pruning of old images
- `shipit status <app>` and `shipit logs <app> -f`

## Install on a Linux server (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/desnarong/shipit/main/install.sh | sudo bash
```

Installs git, Python and Docker if missing, puts shipit in `/opt/shipit`, creates a `shipit` user and a
systemd service that listens for webhooks on port 9000. Read the script first if you like - it is short.
Options: `--dry-run`, `--no-docker`, `--no-service`, `--from-git`.

Then deploy and enable auto deploy:

```bash
sudo -u shipit SHIPIT_CONFIG=/var/lib/shipit/shipit.toml shipit up https://github.com/you/your-app.git
shipit hook your-app --url http://YOUR-SERVER-IP:9000      # registers the GitHub webhook for you (needs `gh`)
```

## Quick start (any machine with Docker)

```bash
pip install shipit-deploy          # the command is `shipit`
# or the latest from GitHub: pip install git+https://github.com/desnarong/shipit.git
shipit up https://github.com/you/your-app.git
```

That's it. `shipit up` picks the default branch, a free port and the right Dockerfile (or writes one),
builds, starts the app, health-checks it and prints the URL. No config file to write -
it creates `shipit.toml` for you. Add `--domain app.example.com` for HTTPS + zero-downtime releases (needs Caddy).

**Turn on auto deploy** (every push redeploys): run `shipit serve`, then register the webhook:
`shipit hook myapp --url http://YOUR-SERVER:9000` (GitHub, uses the `gh` CLI), or add the URL and secret
that `shipit up` printed to your repo by hand (Settings -> Webhooks). Apps added with `shipit up`
are picked up by a running `shipit serve` automatically - no restart.

Other commands:

```bash
shipit list                  # apps, status, URLs
shipit hook myapp --url URL  # register the GitHub push webhook
shipit deploy myapp          # redeploy now
shipit rollback myapp        # back to the previous release
shipit logs myapp -f         # follow logs
shipit status myapp          # current release + history
shipit doctor                # check docker / git / caddy / secrets
shipit detect ./my-project   # preview the generated Dockerfile
```

The webhook secret is generated for you (kept in `.shipit/webhook-secret`); set `SHIPIT_SECRET` to use your own.
If your Dockerfile has a single `EXPOSE`, shipit uses that port automatically.

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
| `pre_deploy` | command run once in the new image before release (e.g. `python manage.py migrate`); failure aborts and the live version stays |
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

End-to-end tests deploy real sample apps with Docker (needs Docker + git, takes a few minutes):

```bash
SHIPIT_E2E=1 python -m unittest tests.test_e2e -v
```

## ☕ Buy me a coffee

shipit is free and open source. If it saves you time, you can support it with a coffee - thank you!

<img src="docs/donate-usdt-trc20.png" alt="Deposit USDT (TRC20) QR code" width="260">

| | |
|---|---|
| **Coin** | USDT |
| **Network** | TRX - Tron (TRC20) |
| **Address** | `TPqzFLc4VhmbSh52G68JVL6rNLWUNMc8Nx` |

Send USDT on the **Tron (TRC20)** network only. Double-check the address before sending - crypto transfers can't be reversed.

Contributions welcome - see [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md). MIT licensed.
