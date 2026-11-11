from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

from . import __version__
from .config import LiveConfig, load
from .deploy import Deployer
from .detect import detect
from .easy import app_name_from_repo, append_app, default_branch, free_port, get_secret, webhook_secret
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


def cmd_up(a) -> int:
    """Zero-config: register a repo, deploy it now, and explain how to enable auto deploy."""
    path = Path(a.config)
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing = raw.get("app", [])
    name = a.name or app_name_from_repo(a.repo)
    if any(x["name"] == name for x in existing):
        print(f"app {name!r} already exists - redeploy it with: shipit deploy {name}", file=sys.stderr)
        return 2
    taken = {p for x in existing for p in (x.get("host_port", 8080), x.get("host_port", 8080) + 1)}
    host_port = free_port(taken)
    fields = {"name": name, "repo": a.repo, "branch": a.branch or default_branch(a.repo),
              "port": a.port, "host_port": host_port, "domain": a.domain}
    append_app(path, fields)
    cfg = load(path)
    print(f"[shipit] added {name!r} to {path} (branch {fields['branch']}, port {host_port})", flush=True)
    ok = Deployer(cfg.apps[name], cfg.state_dir, cfg).deploy()
    if not ok:
        print(f"\nDeploy failed. Fix the problem, then retry with: shipit deploy {name}", file=sys.stderr)
        return 1
    url = f"https://{a.domain}" if a.domain else f"http://localhost:{host_port}"
    print(f"\n{name} is live: {url}\n")
    print("Turn on auto deploy (every push to the branch redeploys):")
    print("  1. run:  shipit serve")
    print(f"  2. GitHub repos, automatic:  shipit hook {name} --url http://<this-server>:{cfg.listen_port}")
    print(f"     or by hand: Settings -> Webhooks -> http://<this-server>:{cfg.listen_port}/hook/{name}")
    print(f"     content type: application/json     secret: {get_secret(cfg.state_dir)}")
    return 0


def cmd_hook(a, cfg) -> int:
    """Register the GitHub webhook for an app (needs the `gh` CLI, logged in)."""
    from . import hooks
    app = cfg.apps[a.app]
    url = hooks.hook_url(a.url, app.name)
    secret = webhook_secret(app, cfg.state_dir)
    try:
        result = hooks.register(app.repo, url, secret)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"could not register automatically: {e}", file=sys.stderr)
        print(f"Add it by hand: URL {url}, content type application/json, secret: {secret}", file=sys.stderr)
        return 1
    print(f"webhook {result}: pushes to {app.branch} will now redeploy {app.name} ({url})")
    return 0


def cmd_list(cfg) -> int:
    for app in cfg.apps.values():
        d = Deployer(app, cfg.state_dir, cfg)
        url = f"https://{app.domain}" if app.domain else f"http://localhost:{app.host_port}"
        state = "running" if d._running(d._live_name()) else "stopped"
        print(f"{app.name:<20} {state:<8} {url:<34} {d.state().get('current', '-')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shipit", description="Self-hosted auto deploy for any language.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default=os.environ.get("SHIPIT_CONFIG", "shipit.toml"))
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
    up = sub.add_parser("up", help="zero-config: deploy a git repo right now")
    up.add_argument("repo", help="git URL of the project")
    up.add_argument("--name")
    up.add_argument("--branch", help="default: the repo's default branch")
    up.add_argument("--port", type=int, help="port the app listens on in the container (usually auto-detected)")
    up.add_argument("--domain", help="serve at this domain via Caddy with zero-downtime releases")
    hk = sub.add_parser("hook", help="register the GitHub push webhook for an app (uses gh)")
    hk.add_argument("app")
    hk.add_argument("--url", required=True, help="public URL of this server, e.g. https://deploy.example.com or http://1.2.3.4:9000")
    sub.add_parser("list", help="list apps and their status")
    sub.add_parser("serve", help="run the webhook server")
    a = p.parse_args(argv)

    try:
        if a.cmd == "up":
            return cmd_up(a)
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
        if a.cmd == "hook":
            if a.app not in cfg.apps:
                print(f"unknown app {a.app!r}; known: {', '.join(cfg.apps)}", file=sys.stderr)
                return 2
            return cmd_hook(a, cfg)
        if a.cmd == "list":
            return cmd_list(cfg)
        if a.cmd == "doctor":
            from . import doctor
            return doctor.run(cfg)
        if a.cmd == "serve":
            from .webhook import make_server
            resync_proxy(cfg)
            srv = make_server(LiveConfig(a.config))
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
