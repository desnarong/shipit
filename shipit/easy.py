"""Zero-config helpers behind `shipit up`."""
from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
from pathlib import Path


def app_name_from_repo(url: str) -> str:
    base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    base = base.removesuffix(".git").lower()
    name = re.sub(r"[^a-z0-9_.-]+", "-", base).strip("-._")
    return name[:60] or "app"


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def free_port(taken: set[int], start: int = 8080) -> int:
    """Lowest port >= start that no configured app uses and nothing is listening on."""
    port = start
    while port < 65535:
        if port not in taken and port + 1 not in taken and not _port_in_use(port) and not _port_in_use(port + 1):
            return port
        port += 2
    raise RuntimeError("no free port found")


def default_branch(repo: str) -> str:
    """Ask the remote which branch HEAD points to (falls back to 'main')."""
    try:
        out = subprocess.run(["git", "ls-remote", "--symref", repo, "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "main"
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    return "main"


def get_secret(state_dir: Path) -> str:
    """Webhook secret: auto-generated once and kept in the state dir."""
    f = state_dir / "webhook-secret"
    if f.exists():
        return f.read_text().strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(20)
    f.write_text(value)
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return value


def append_app(config_path: Path, fields: dict) -> None:
    """Append an [[app]] block, creating the config file if needed."""
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else '[server]\nport = 9000\nstate_dir = ".shipit"\n'
    if not text.endswith("\n"):
        text += "\n"
    block = "\n[[app]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items() if v is not None)
    config_path.write_text(text + block, encoding="utf-8")
