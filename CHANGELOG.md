# Changelog

## Unreleased

- `shipit doctor` works before the first app exists (and without a config file)

## 0.2.0

- `install.sh`: one-command Linux installer with a systemd service
- `shipit hook <app> --url ...`: register the GitHub webhook through `gh`
- `shipit serve` picks up apps added by `shipit up` without a restart
- Config path from `SHIPIT_CONFIG`; relative `state_dir` is relative to the config file

## 0.1.0

First public release.

- `shipit up <repo>`: zero-config deploy (free port, default branch, Dockerfile detection, webhook secret)
- Language detection for Node, Python (Django/FastAPI/Flask), Go, Rust, Java, PHP, Ruby, .NET, Elixir, static; a repo Dockerfile always wins
- GitHub/GitLab webhooks, health-checked deploys, automatic rollback, history, notifications, image pruning
- Zero-downtime blue/green releases through Caddy
- `doctor`, `status`, `logs`, `list`, `rollback`, `pre_deploy` hook
- Unit tests and real-Docker end-to-end tests
