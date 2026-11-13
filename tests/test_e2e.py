"""End-to-end tests against a real Docker daemon.

Skipped unless SHIPIT_E2E=1 (they pull images and take a few minutes):

    SHIPIT_E2E=1 python -m unittest tests.test_e2e -v
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

from shipit.config import App
from shipit.deploy import Deployer

ENABLED = os.environ.get("SHIPIT_E2E") == "1" and shutil.which("docker") and shutil.which("git")

PY_SERVER = """import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"%s")
HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
"""

NODE_SERVER = """const http = require("http");
http.createServer((q, r) => r.end("%s")).listen(process.env.PORT, "0.0.0.0");
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=repo, check=True, capture_output=True)


def commit_files(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "change")


def fetch(port: int, timeout: float = 5) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
        return r.read().decode().strip()


@unittest.skipUnless(ENABLED, "set SHIPIT_E2E=1 and have docker + git to run")
class E2EBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.name = "e2e-" + uuid.uuid4().hex[:8]
        self.addCleanup(self.cleanup_docker)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")

    def cleanup_docker(self):
        ids = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"name={self.name}"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        images = subprocess.run(["docker", "images", f"shipit/{self.name}*", "-q"],
                                capture_output=True, text=True).stdout.split()
        if images:
            subprocess.run(["docker", "rmi", "-f", *images], capture_output=True)

    def deployer(self, **kw) -> Deployer:
        kw.setdefault("port", 8080)
        kw.setdefault("host_port", free_port())
        kw.setdefault("health_timeout", 90)
        app = App(name=self.name, repo=self.repo.resolve().as_uri(), **kw)
        return Deployer(app, self.tmp / "state")


class DetectedLanguages(E2EBase):
    def test_static_site(self):
        commit_files(self.repo, {"index.html": "static-v1"})
        d = self.deployer()
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "static-v1")

    def test_python_stdlib_app(self):
        commit_files(self.repo, {"app.py": PY_SERVER % "py-v1"})
        d = self.deployer()
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "py-v1")

    def test_node_app(self):
        commit_files(self.repo, {
            "package.json": '{"name":"t","version":"1.0.0","scripts":{"start":"node server.js"}}',
            "server.js": NODE_SERVER % "node-v1",
        })
        d = self.deployer()
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "node-v1")

    def test_custom_dockerfile_any_language(self):
        # No detection involved: a Dockerfile makes any language/runtime work.
        commit_files(self.repo, {
            "Dockerfile": "FROM busybox\nCOPY www /www\nCMD [\"httpd\", \"-f\", \"-p\", \"8080\", \"-h\", \"/www\"]\n",
            "www/index.html": "custom-v1",
        })
        d = self.deployer()
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "custom-v1")


class ReleaseLifecycle(E2EBase):
    def test_redeploy_rollback_and_bad_release(self):
        commit_files(self.repo, {"app.py": PY_SERVER % "v1"})
        d = self.deployer()
        port = d.app.host_port

        self.assertTrue(d.deploy())
        self.assertEqual(fetch(port), "v1")
        first = d.state()["current"]

        # new commit -> new version live
        commit_files(self.repo, {"app.py": PY_SERVER % "v2"})
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(port), "v2")
        self.assertEqual(d.state()["previous"], first)

        # broken commit -> deploy fails, v2 keeps serving, no leftover candidate
        commit_files(self.repo, {"app.py": "raise SystemExit(1)\n"})
        d.app.health_timeout = 20
        self.assertFalse(d.deploy())
        self.assertEqual(fetch(port), "v2")
        self.assertEqual(d.state()["history"][-1]["status"], "failed")
        leftovers = subprocess.run(["docker", "ps", "-a", "--filter", f"name={self.name}-next", "-q"],
                                   capture_output=True, text=True).stdout.strip()
        self.assertEqual(leftovers, "")

        # manual rollback to v1
        self.assertTrue(d.rollback())
        self.assertEqual(fetch(port), "v1")
        self.assertIn("rolled-back", d.status())

    def test_pre_deploy_failure_blocks_release(self):
        commit_files(self.repo, {"app.py": PY_SERVER % "v1"})
        d = self.deployer()
        self.assertTrue(d.deploy())
        commit_files(self.repo, {"app.py": PY_SERVER % "v2"})
        d.app.pre_deploy = "exit 3"
        self.assertFalse(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "v1")
        self.assertEqual(d.state()["history"][-1]["status"], "pre-deploy-failed")


class ZeroConfigUp(E2EBase):
    def test_up_with_no_config_and_exposed_port(self):
        from shipit.cli import main
        # Dockerfile EXPOSEs 3000 and nothing in config says so: shipit must find it.
        commit_files(self.repo, {
            "Dockerfile": "FROM busybox\nCOPY www /www\nEXPOSE 3000\nCMD [\"httpd\", \"-f\", \"-p\", \"3000\", \"-h\", \"/www\"]\n",
            "www/index.html": "up-v1",
        })
        cfg = self.tmp / "shipit.toml"
        rc = main(["-c", str(cfg), "up", self.repo.resolve().as_uri(), "--name", self.name])
        self.assertEqual(rc, 0)
        text = cfg.read_text()
        port = int(text.split("host_port = ")[1].split()[0])
        self.assertEqual(fetch(port), "up-v1")
        self.assertIn('branch = "main"', text)           # branch auto-detected
        self.assertEqual(main(["-c", str(cfg), "up", self.repo.resolve().as_uri(), "--name", self.name]), 2)  # no duplicates
        self.assertEqual(main(["-c", str(cfg), "list"]), 0)


BUSYBOX = "FROM busybox\nCOPY www /www\nEXPOSE 8080\nCMD [\"httpd\", \"-f\", \"-p\", \"8080\", \"-h\", \"/www\"]\n"


class MonorepoE2E(E2EBase):
    def test_app_inside_a_subfolder(self):
        commit_files(self.repo, {
            "package.json": "this is a broken decoy at the repo root",
            "services/api/Dockerfile": BUSYBOX,
            "services/api/www/index.html": "mono-v1",
        })
        d = self.deployer(subdir="services/api")
        self.assertTrue(d.deploy())
        self.assertEqual(fetch(d.app.host_port), "mono-v1")


class PreviewE2E(E2EBase):
    def test_pull_request_preview_lifecycle(self):
        from shipit.config import Config
        from shipit.previews import Previews
        commit_files(self.repo, {"Dockerfile": BUSYBOX, "www/index.html": "main-v1"})
        git(self.repo, "switch", "-c", "feature-x")
        commit_files(self.repo, {"www/index.html": "feature-v2"})
        git(self.repo, "switch", "main")

        d = self.deployer(previews=True)
        self.assertTrue(d.deploy())                                  # production from main
        prod_port = d.app.host_port
        cfg = Config(self.tmp / "state", "127.0.0.1", 9000, "http://127.0.0.1:2019", {self.name: d.app})
        previews = Previews(cfg, d.app)

        self.assertTrue(previews.deploy(5, "feature-x"))             # PR opened
        port = previews._load()["5"]["host_port"]
        self.assertNotEqual(port, prod_port)
        self.assertEqual(fetch(port), "feature-v2")                  # preview shows the PR branch
        self.assertEqual(fetch(prod_port), "main-v1")                # production untouched

        commit_files_on(self.repo, "feature-x", {"www/index.html": "feature-v3"})
        self.assertTrue(previews.deploy(5, "feature-x"))             # new push to the PR
        self.assertEqual(previews._load()["5"]["host_port"], port)   # same port
        self.assertEqual(fetch(port), "feature-v3")

        previews.teardown(5)                                         # PR closed
        names = subprocess.run(["docker", "ps", "-a", "--filter", f"name={self.name}-pr-5", "-q"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(names, "")
        self.assertEqual(previews._load(), {})
        self.assertEqual(fetch(prod_port), "main-v1")


def commit_files_on(repo: Path, branch: str, files: dict[str, str]) -> None:
    git(repo, "switch", branch)
    commit_files(repo, files)
    git(repo, "switch", "main")


if __name__ == "__main__":
    unittest.main()
