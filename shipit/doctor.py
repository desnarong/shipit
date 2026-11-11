"""`shipit doctor`: check the host and config before the first deploy."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def _docker_ok() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker not found in PATH"
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    return r.returncode == 0, "daemon reachable" if r.returncode == 0 else "docker daemon not reachable (is it running / do you have access?)"


def _caddy_ok(admin: str) -> tuple[bool, str]:
    try:
        urllib.request.urlopen(admin.rstrip("/") + "/config/", timeout=3).close()
        return True, f"admin API reachable at {admin}"
    except OSError as e:
        return False, f"cannot reach Caddy admin API at {admin} ({e})"


def checks(cfg) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    out.append(("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    out.append(("git", bool(shutil.which("git")), shutil.which("git") or "not found in PATH"))
    out.append(("docker", *_docker_ok()))
    if any(a.domain for a in cfg.apps.values()):
        out.append(("caddy", *_caddy_ok(cfg.caddy_admin)))
    ports: dict[int, str] = {}
    for a in cfg.apps.values():
        used = [a.host_port] + ([a.host_port + 1] if a.domain else [])
        for p in used:
            if p in ports:
                out.append((f"{a.name}: port {p}", False, f"also used by {ports[p]}"))
            ports[p] = a.name
        env = a.secret_env or "SHIPIT_SECRET"
        out.append((f"{a.name}: webhook secret ${env}", True, "set" if os.environ.get(env) else f"not set; using auto-generated secret in {cfg.state_dir / 'webhook-secret'}"))
        if a.token_env:
            out.append((f"{a.name}: git token ${a.token_env}", bool(os.environ.get(a.token_env)), "set" if os.environ.get(a.token_env) else "not set"))
        if a.env_file:
            out.append((f"{a.name}: env_file", Path(a.env_file).is_file(), a.env_file))
        if a.notify_env:
            out.append((f"{a.name}: notify ${a.notify_env}", bool(os.environ.get(a.notify_env)), "set" if os.environ.get(a.notify_env) else "not set (notifications off)"))
    return out


def run(cfg) -> int:
    bad = 0
    if not cfg.apps:
        print("  (no apps configured yet - checking the machine only; add one with: shipit up <git-url>)")
    for name, ok, detail in checks(cfg):
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {detail}")
        bad += not ok
    print("all good" if not bad else f"{bad} problem(s) found")
    return 1 if bad else 0
