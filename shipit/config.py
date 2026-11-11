"""Load shipit.toml."""
from __future__ import annotations

import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


@dataclass
class App:
    name: str
    repo: str
    branch: str = "main"
    port: int = 8080              # port the app listens on inside the container
    host_port: int = 8080         # port published on this machine
    health_path: str = "/"        # HTTP path polled after start; "" = just check container is running
    health_timeout: int = 60
    start: str | None = None      # override start command (used by generated Dockerfiles)
    dockerfile: str | None = None # path inside repo; default: ./Dockerfile, else auto-generated
    env_file: str | None = None   # path on the host
    token_env: str | None = None  # env var holding a git token for private https repos
    secret_env: str | None = None # env var with the webhook secret for this app
    pre_deploy: str | None = None # command run once in the new image before release (e.g. migrations); failure aborts
    domain: str | None = None     # set to serve via Caddy with zero-downtime blue/green (needs Caddy)
    notify_env: str | None = None # env var holding a Slack/Discord/Mattermost-style webhook URL
    drain_seconds: int = 5        # proxy mode: wait before removing the old slot
    keep_images: int = 5          # old images kept on disk (current + previous are always kept)
    build_args: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    state_dir: Path
    listen_host: str
    listen_port: int
    caddy_admin: str
    apps: dict[str, App]


def load(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    server = data.get("server", {})
    apps: dict[str, App] = {}
    for raw in data.get("app", []):
        app = App(**raw)
        if not NAME_RE.match(app.name):
            raise ValueError(f"invalid app name {app.name!r} (use a-z 0-9 _ . -)")
        if app.domain and not DOMAIN_RE.match(app.domain):
            raise ValueError(f"invalid domain {app.domain!r}")
        if app.name in apps:
            raise ValueError(f"duplicate app {app.name!r}")
        apps[app.name] = app
    if not apps:
        raise ValueError("no [[app]] entries in config")
    return Config(
        # relative state_dir is relative to the config file, not the current directory
        state_dir=(Path(path).resolve().parent / server.get("state_dir", ".shipit")).resolve(),
        listen_host=server.get("host", "0.0.0.0"),
        listen_port=int(server.get("port", 9000)),
        caddy_admin=server.get("caddy_admin", "http://127.0.0.1:2019"),
        apps=apps,
    )


class LiveConfig:
    """Re-reads the config file when it changes, so `shipit up` can add apps while `serve` runs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mtime: int | None = None
        self._cfg: Config | None = None
        self._lock = threading.Lock()
        self.get()

    def get(self) -> Config:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
                if mtime != self._mtime:
                    self._cfg = load(self.path)
                    self._mtime = mtime
            except (OSError, ValueError):
                if self._cfg is None:
                    raise  # first load must succeed; later bad edits keep the last good config
            return self._cfg
