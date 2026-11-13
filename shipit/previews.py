"""Pull-request preview deploys.

For apps with `previews = true`, a GitHub `pull_request` webhook deploys that PR's branch as a separate
throw-away app (`<name>-pr-<n>`) on its own port, redeploys it on every push to the PR, and removes it
when the PR is closed.

Safety: only same-repo PRs are deployed (a fork's code would run on your server), previews never use the
production env_file / pre_deploy hook, and the branch name is validated before it reaches git.
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from .config import App, Config
from .deploy import Deployer, log
from .easy import free_port

PREVIEW_PORT_START = 20000
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
DEPLOY_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


def preview_name(app: App, number: int) -> str:
    return f"{app.name}-pr-{number}"


def parse_pr_event(payload: dict) -> dict | None:
    """Reduce a GitHub pull_request payload to what we need, or None if it is not usable."""
    pr = payload.get("pull_request")
    if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
        return None
    head, base = pr.get("head") or {}, pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = (base.get("repo") or {}).get("full_name")
    return {
        "action": payload.get("action", ""),
        "number": pr["number"],
        "ref": head.get("ref", ""),
        "same_repo": bool(head_repo) and head_repo == base_repo,
        "draft": bool(pr.get("draft")),
    }


class Previews:
    def __init__(self, cfg: Config, app: App):
        self.cfg, self.app = cfg, app
        self.file = cfg.state_dir / app.name / "previews.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, reg: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(reg, indent=2))

    def _taken_ports(self) -> set[int]:
        taken = set()
        for a in self.cfg.apps.values():
            taken |= {a.host_port, a.host_port + 1}
            for entry in Previews(self.cfg, a)._load().values():
                taken.add(entry["host_port"])
        return taken

    def _app_for(self, number: int, ref: str, port: int) -> App:
        a = self.app
        return dataclasses.replace(
            a, name=preview_name(a, number), branch=ref, host_port=port, domain=None, previews=False,
            pre_deploy=None, env_file=a.preview_env_file,
        )

    def deploy(self, number: int, ref: str) -> bool:
        if not SAFE_REF.match(ref):
            log(f"preview: refusing unsafe branch name {ref!r}")
            return False
        reg = self._load()
        entry = reg.get(str(number))
        port = entry["host_port"] if entry else free_port(self._taken_ports(), PREVIEW_PORT_START)
        reg[str(number)] = {"host_port": port, "ref": ref}
        self._save(reg)  # reserve the port before the (slow) build
        ok = Deployer(self._app_for(number, ref, port), self.cfg.state_dir, self.cfg).deploy()
        if ok:
            log(f"preview for PR #{number} is live: http://localhost:{port}")
        return ok

    def teardown(self, number: int) -> None:
        reg = self._load()
        entry = reg.pop(str(number), None)
        port = entry["host_port"] if entry else self.app.host_port
        Deployer(self._app_for(number, (entry or {}).get("ref", "main"), port), self.cfg.state_dir, self.cfg).destroy()
        self._save(reg)
        log(f"preview for PR #{number} removed")
