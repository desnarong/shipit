"""Register the push webhook on GitHub through the `gh` CLI."""
from __future__ import annotations

import json
import re
import subprocess

_GITHUB = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def parse_github_repo(url: str) -> tuple[str, str] | None:
    m = _GITHUB.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def hook_url(base: str, app: str) -> str:
    base = base.rstrip("/")
    return base if "/hook/" in base else f"{base}/hook/{app}"


def payload(url: str, secret: str, events: tuple[str, ...] = ("push",)) -> dict:
    return {
        "name": "web",
        "active": True,
        "events": list(events),
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }


def _gh(method: str, path: str, body: dict | None = None) -> str:
    """Call the GitHub API via gh. The body goes through stdin so the secret never shows in `ps`."""
    cmd = ["gh", "api", "--method", method, path] + (["--input", "-"] if body is not None else [])
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"gh exited with {r.returncode}")
    return r.stdout


def register(repo_url: str, url: str, secret: str, events: tuple[str, ...] = ("push",)) -> str:
    """Create (or update) the webhook. Returns 'created' or 'updated'."""
    parsed = parse_github_repo(repo_url)
    if not parsed:
        raise ValueError("automatic registration only supports github.com repos; add the webhook manually")
    owner, repo = parsed
    base = f"repos/{owner}/{repo}/hooks"
    for h in json.loads(_gh("GET", base) or "[]"):
        if h.get("config", {}).get("url") == url:
            _gh("PATCH", f"{base}/{h['id']}", payload(url, secret, events))
            return "updated"
    _gh("POST", base, payload(url, secret, events))
    return "created"
