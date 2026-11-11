from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load
from .deploy import Deployer
from .detect import detect
from .proxy import sync as _proxy_sync

def resync_proxy(cfg) -> None:
    """Re-apply Caddy routes at startup (e.g. after Caddy restarted)."""
    if not any(a.domain for a in cfg.apps.values()):
        return
    try:
        _proxy_sync(cfg, {n: Deployer(a, cfg.state_dir).state() for n, a in cfg.apps.items()})
    except OSError as e:
        print(f"[shipit] warning: could not sync Caddy: {e}", file=sys.stderr)


SAMPLE = '''[server]
port = 9000
state_dir = ".shipit"

[[app]]
name = "myapp"
repo = "https://github.com/you/myapp.git"
branch = "main"
port = 8080          # port inside the container
host_port = 8080     # port on this machine
health_path = "/"
# token_env  = "GIT_TOKEN"        # private repos
# secret_env = "MYAPP_SECRET"     # webhook secret (default: SHIPIT_SECRET)
# notify_env = "SLACK_URL"        # env var with a Slack/Discord webhook URL
# pre_deploy = "python manage.py migrate"   # runs in the new image before release
# domain = "app.example.com"   # zero-downtime via Caddy (blue/green on host_port and host_port+1)
# keep_images = 5
# env_file   = "/etc/myapp.env"
# start      = "gunicorn app:app -b 0.0.0.0:8080"
'''


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shipit", description="Self-hosted auto deploy for any language.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default="shipit.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write a sample shipit.toml")
    d = sub.add_parser("detect", help="show detected language / print generated Dockerfile")
    d.add_argument("path", nargs="?", default=".")
    d.add_argument("--port", type=int, default=8080)
    for name, h in (("deploy", "deploy an app now"), ("rollback", "roll back to previous release")):
        s = sub.add_parser(name, help=h)
        s.add_argument("app")
    st = sub.add_parser("status", help="show current release and recent history")
    st.add_argument("app")
    lg = sub.add_parser("logs", help="show container logs")
    lg.add_argument("app")
    lg.add_argument("-n", "--tail", type=int, default=100)
    lg.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("doctor", help="check docker/git/caddy/secrets before deploying")
    sub.add_parser("serve", help="run the webhook server")
    a = p.parse_args(argv)

    try:
        if a.cmd == "init":
            f = Path(a.config)
            if f.exists():
                print(f"{f} already exists", file=sys.stderr)
                return 1
            f.write_text(SAMPLE, encoding="utf-8")
            print(f"wrote {f}")
            return 0
        if a.cmd == "detect":
            r = detect(a.path, a.port)
            print(f"# language: {r.lang}\n{r.dockerfile}")
            return 0
        cfg = load(a.config)
        if a.cmd == "doctor":
            from . import doctor
            return doctor.run(cfg)
        if a.cmd == "serve":
            from .webhook import make_server
            resync_proxy(cfg)
            srv = make_server(cfg)
            print(f"[shipit] listening on {cfg.listen_host}:{cfg.listen_port}  (POST /hook/<app>)", flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        if a.app not in cfg.apps:
            print(f"unknown app {a.app!r}; known: {', '.join(cfg.apps)}", file=sys.stderr)
            return 2
        dep = Deployer(cfg.apps[a.app], cfg.state_dir, cfg)
        if a.cmd == "status":
            print(dep.status())
            return 0
        if a.cmd == "logs":
            dep.logs(a.tail, a.follow)
            return 0
        ok = dep.deploy() if a.cmd == "deploy" else dep.rollback()
        return 0 if ok else 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
