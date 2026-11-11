"""Webhook server: GitHub (HMAC) and GitLab (token) push events trigger deploys."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .deploy import Deployer, log
from .easy import get_secret

MAX_BODY = 5 * 1024 * 1024


def verify(secret: str, headers, body: bytes) -> bool:
    sig = headers.get("X-Hub-Signature-256")
    if sig:  # GitHub / Gitea / Forgejo
        want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)
    tok = headers.get("X-Gitlab-Token")
    if tok:  # GitLab
        return hmac.compare_digest(tok, secret)
    return False


def make_server(cfg: Config) -> ThreadingHTTPServer:
    jobs: queue.Queue[str] = queue.Queue()
    pending: set[str] = set()
    lock = threading.Lock()

    def worker():
        while True:
            name = jobs.get()
            with lock:
                pending.discard(name)
            try:
                Deployer(cfg.apps[name], cfg.state_dir, cfg).deploy()
            except Exception as e:  # keep the worker alive
                log(f"deploy of {name} crashed: {e}")

    threading.Thread(target=worker, daemon=True).start()

    def enqueue(name: str) -> bool:
        with lock:
            if name in pending:
                return False  # a build is already queued; it will pick up the latest commit
            pending.add(name)
        jobs.put(name)
        return True

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, msg: str):
            body = json.dumps({"message": msg}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(200, "ok") if self.path == "/healthz" else self._reply(404, "not found")

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "hook" or parts[1] not in cfg.apps:
                return self._reply(404, "not found")
            app = cfg.apps[parts[1]]
            secret = os.environ.get(app.secret_env or "SHIPIT_SECRET", "") or get_secret(cfg.state_dir)
            if not secret:
                return self._reply(500, "webhook secret not configured")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._reply(413, "payload too large")
            body = self.rfile.read(n)
            if not verify(secret, self.headers, body):
                return self._reply(401, "bad signature")
            try:
                ref = json.loads(body or b"{}").get("ref", "")
            except ValueError:
                return self._reply(400, "bad json")
            if ref and ref != f"refs/heads/{app.branch}":
                return self._reply(200, f"ignored ref {ref}")
            queued = enqueue(app.name)
            self._reply(202, "queued" if queued else "already queued")

        def log_message(self, fmt, *args):
            log("http " + fmt % args)

    return ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), Handler)
