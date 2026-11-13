"""Build and run an app with Docker, health-check it, roll back on failure."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import proxy
from .config import App, Config
from .detect import detect


def log(msg: str) -> None:
    print(f"[shipit] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    safe = " ".join(cmd)
    log(f"$ {safe[:200]}")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def _out(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def _auth_url(app: App) -> str:
    """Inject a token into an https repo URL without writing it to disk or logs."""
    token = os.environ.get(app.token_env) if app.token_env else None
    u = urlparse(app.repo)
    if token and u.scheme in ("http", "https"):
        return urlunparse(u._replace(netloc=f"x-access-token:{token}@{u.hostname}" + (f":{u.port}" if u.port else "")))
    return app.repo


class Deployer:
    def __init__(self, app: App, state_dir: Path, cfg: Config | None = None):
        self.app = app
        self.cfg = cfg
        self.state_dir = state_dir
        self._port: int | None = None  # container port of the image being built
        self.dir = state_dir / app.name
        self.src = self.dir / "src"
        self.state_file = self.dir / "state.json"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- state -------------------------------------------------------
    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, **kw) -> None:
        s = self.state()
        s.update(kw)
        self.state_file.write_text(json.dumps(s, indent=2))

    @property
    def root(self) -> Path:
        """Folder with the app's code: the repo root, or `subdir` for monorepos."""
        return self.src / self.app.subdir if self.app.subdir else self.src

    @property
    def container_port(self) -> int:
        """Port the app listens on inside the container (generated Dockerfiles may differ from config)."""
        return self._port or self.state().get("container_port") or self.app.port

    # ---- steps -------------------------------------------------------
    def sync(self) -> str:
        a = self.app
        url = _auth_url(a)
        if (self.src / ".git").exists():
            run(["git", "remote", "set-url", "origin", url], cwd=self.src)
            run(["git", "fetch", "--depth", "1", "origin", a.branch], cwd=self.src)
            run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=self.src)
            run(["git", "clean", "-fdx"], cwd=self.src)
        else:
            run(["git", "clone", "--depth", "1", "-b", a.branch, url, str(self.src)])
        run(["git", "remote", "set-url", "origin", a.repo], cwd=self.src)  # drop token from .git/config
        return _out(["git", "rev-parse", "--short=12", "HEAD"], cwd=self.src)

    def build(self, sha: str) -> str:
        a = self.app
        image = f"shipit/{a.name}:{sha}"
        if not self.root.is_dir():
            raise ValueError(f"subdir {a.subdir!r} does not exist in the repo")
        dockerfile = self.root / (a.dockerfile or "Dockerfile")
        self._port = a.port  # a repo-provided Dockerfile listens where the config says
        generated = not dockerfile.exists()
        if generated:
            d = detect(self.root, a.port, a.start)
            self._port = d.port
            log(f"detected {d.lang}; generating Dockerfile")
            dockerfile = self.dir / "Dockerfile.generated"
            dockerfile.write_text(d.dockerfile)
        cmd = ["docker", "build", "-f", str(dockerfile), "-t", image]
        for k, v in a.build_args.items():
            cmd += ["--build-arg", f"{k}={v}"]
        run(cmd + [str(self.root)])
        if not generated:
            exposed = self._exposed_ports(image)
            if len(exposed) == 1:   # zero-config: follow the Dockerfile's EXPOSE
                self._port = exposed[0]
        return image

    @staticmethod
    def _exposed_ports(image: str) -> list[int]:
        try:
            data = json.loads(_out(["docker", "image", "inspect", "-f", "{{json .Config.ExposedPorts}}", image]))
        except (subprocess.CalledProcessError, ValueError):
            return []
        return sorted(int(k.split("/")[0]) for k in (data or {}))

    def _run_container(self, name: str, image: str, publish: str, restart: str = "unless-stopped") -> None:
        a = self.app
        run(["docker", "rm", "-f", name], check=False)
        cmd = ["docker", "run", "-d", "--name", name, "--restart", restart,
               "-p", publish, "--label", "shipit=1"]
        if a.env_file:
            cmd += ["--env-file", a.env_file]
        run(cmd + [image])

    def _pre_deploy(self, image: str) -> bool:
        a = self.app
        if not a.pre_deploy:
            return True
        cmd = ["docker", "run", "--rm"] + (["--env-file", a.env_file] if a.env_file else [])
        ok = run(cmd + [image, "sh", "-c", a.pre_deploy], check=False).returncode == 0
        if not ok:
            log("pre_deploy command failed; release aborted")
            self._record("pre-deploy-failed", image)
            self.notify(f"❌ {a.name}: pre_deploy failed for {image}. The live version is unchanged.")
        return ok

    def start(self, image: str) -> None:
        a = self.app
        self._run_container(a.name, image, f"{a.host_port}:{self.container_port}")

    def _running(self, name: str) -> bool:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                           capture_output=True, text=True)
        return r.stdout.strip() == "true"

    def healthy(self, name: str | None = None, host_port: int | None = None) -> bool:
        a = self.app
        name = name or a.name
        host_port = host_port or a.host_port
        deadline = time.time() + a.health_timeout
        while time.time() < deadline:
            if not self._running(name):
                return False
            if not a.health_path:
                time.sleep(5)  # no HTTP check: require it to survive a few seconds
                return self._running(name)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{host_port}{a.health_path}", timeout=3) as r:
                    if r.status < 500:
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return True
            except OSError:
                pass
            time.sleep(2)
        return False

    # ---- history / notify / prune -----------------------------------
    def _record(self, status: str, image: str) -> None:
        hist = self.state().get("history", [])
        hist.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "image": image, "status": status})
        self._save(history=hist[-20:])

    def notify(self, text: str) -> None:
        env = self.app.notify_env
        url = os.environ.get(env) if env else None
        if not url or urlparse(url).scheme not in ("http", "https"):
            return
        # "text" works for Slack/Mattermost, "content" for Discord.
        body = json.dumps({"text": text, "content": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as e:
            log(f"notify failed: {e}")

    def prune(self) -> None:
        s = self.state()
        keep = {i for i in (s.get("current"), s.get("previous")) if i}
        out = subprocess.run(["docker", "images", f"shipit/{self.app.name}", "--format", "{{.Repository}}:{{.Tag}}"],
                             capture_output=True, text=True).stdout.split()
        for image in out[max(self.app.keep_images, 0):]:  # docker lists newest first
            if image not in keep:
                run(["docker", "rmi", image], check=False)

    # ---- zero-downtime release through Caddy ------------------------
    def _all_states(self) -> dict[str, dict]:
        return {n: Deployer(a, self.state_dir).state() for n, a in self.cfg.apps.items()}

    def _release_proxy(self, image: str, prev: str | None) -> bool:
        a = self.app
        if self.cfg is None:
            raise ValueError("`domain` needs the full config; run via the shipit CLI")
        old = self.state()
        old_slot = old.get("slot")
        new_slot = "green" if old_slot == "blue" else "blue"
        port = proxy.slot_port(a.host_port, new_slot)
        name = f"{a.name}-{new_slot}"
        self._run_container(name, image, f"127.0.0.1:{port}:{self.container_port}")
        if not self.healthy(name, port):
            log(f"health check FAILED for {image}; live version untouched")
            run(["docker", "logs", "--tail", "40", name], check=False)
            run(["docker", "rm", "-f", name], check=False)
            self._record("failed", image)
            self.notify(f"❌ {a.name}: {image} failed its health check. The live version is unchanged.")
            return False
        self._save(slot=new_slot, port=port, current=image, previous=prev, container_port=self.container_port)
        try:
            proxy.sync(self.cfg, self._all_states())
        except OSError as e:
            log(f"Caddy update failed ({e}); keeping the old version")
            self._save(**{k: old.get(k) for k in ("slot", "port", "current", "previous")})
            run(["docker", "rm", "-f", name], check=False)
            self._record("proxy-failed", image)
            self.notify(f"❌ {a.name}: could not update Caddy ({e}). The live version is unchanged.")
            return False
        if old_slot:
            time.sleep(a.drain_seconds)  # let in-flight requests finish
            run(["docker", "rm", "-f", f"{a.name}-{old_slot}"], check=False)
        return True

    # ---- public ------------------------------------------------------
    def deploy(self) -> bool:
        a = self.app
        log(f"deploying {a.name} ({a.repo}@{a.branch})")
        try:
            sha = self.sync()
            image = self.build(sha)
        except Exception as e:
            self._record("build-failed", "-")
            self.notify(f"❌ {a.name}: build failed ({e})")
            raise
        if not self._pre_deploy(image):
            return False
        prev = self.state().get("current")
        if a.domain:
            ok = self._release_proxy(image, prev)
            if ok:
                self._record("deployed", image)
                self.notify(f"✅ {a.name} deployed: {image} (zero-downtime)")
                self.prune()
                log(f"OK {a.name} -> {image}")
            return ok

        # 1. Start the new version beside the live one on a random local port and test it.
        cand = f"{a.name}-next"
        self._run_container(cand, image, f"127.0.0.1::{self.container_port}", restart="no")
        try:
            port = int(_out(["docker", "port", cand, f"{self.container_port}/tcp"]).splitlines()[0].rsplit(":", 1)[1])
            ok = self.healthy(cand, port)
        except (subprocess.CalledProcessError, IndexError, ValueError):
            ok = False
        if not ok:
            log(f"health check FAILED for {image}; live version untouched")
            run(["docker", "logs", "--tail", "40", cand], check=False)
            run(["docker", "rm", "-f", cand], check=False)
            self._record("failed", image)
            self.notify(f"❌ {a.name}: {image} failed its health check. The live version is unchanged.")
            return False
        run(["docker", "rm", "-f", cand], check=False)

        # 2. Swap (a brief gap while the port is handed over), then verify again.
        self.start(image)
        if self.healthy():
            self._save(current=image, previous=prev, container_port=self.container_port)
            self._record("deployed", image)
            self.notify(f"✅ {a.name} deployed: {image}")
            self.prune()
            log(f"OK {a.name} -> {image}")
            return True
        log("health check failed after swap")
        run(["docker", "logs", "--tail", "40", a.name], check=False)
        if prev:
            log(f"rolling back to {prev}")
            self.start(prev)
        else:
            run(["docker", "rm", "-f", a.name], check=False)
        self._record("failed-after-swap", image)
        self.notify(f"❌ {a.name}: {image} failed after swap; rolled back to {prev or 'nothing'}")
        return False

    def rollback(self) -> bool:
        prev = self.state().get("previous")
        if not prev:
            log("no previous release recorded")
            return False
        cur = self.state().get("current")
        if self.app.domain:
            ok = self._release_proxy(prev, cur)
            if ok:
                self._record("rolled-back", prev)
                self.notify(f"↩️ {self.app.name} rolled back to {prev}")
            return ok
        self.start(prev)
        if self.healthy():
            self._save(current=prev, previous=cur)
            self._record("rolled-back", prev)
            self.notify(f"↩️ {self.app.name} rolled back to {prev}")
            log(f"rolled back to {prev}")
            return True
        return False

    def _live_name(self) -> str:
        slot = self.state().get("slot")
        return f"{self.app.name}-{slot}" if self.app.domain and slot else self.app.name

    def destroy(self) -> None:
        """Remove everything for this app: containers, images, state (used for preview teardown)."""
        a = self.app
        for suffix in ("", "-next", "-blue", "-green"):
            run(["docker", "rm", "-f", a.name + suffix], check=False)
        images = subprocess.run(["docker", "images", f"shipit/{a.name}", "-q"], capture_output=True, text=True).stdout.split()
        if images:
            run(["docker", "rmi", "-f", *images], check=False)
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self) -> str:
        s = self.state()
        lines = [f"app:      {self.app.name}",
                 f"running:  {'yes' if self._running(self._live_name()) else 'no'}",
                 f"current:  {s.get('current', '-')}",
                 f"previous: {s.get('previous', '-')}",
                 "history:"]
        lines += [f"  {h['time']}  {h['status']:<18} {h['image']}" for h in s.get("history", [])[-5:]]
        return "\n".join(lines)

    def logs(self, tail: int = 100, follow: bool = False) -> None:
        cmd = ["docker", "logs", "--tail", str(tail)] + (["-f"] if follow else []) + [self._live_name()]
        run(cmd, check=False)
