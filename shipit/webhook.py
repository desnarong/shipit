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
from .easy import webhook_secret
from .previews import DEPLOY_ACTIONS, Previews, parse_pr_event

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


def make_server(source) -> ThreadingHTTPServer:
    """`source` is a Config or a LiveConfig (anything with .get())."""
    get = source.get if hasattr(source, "get") else (lambda: source)
    cfg = get()
    jobs: queue.Queue[str] = queue.Queue()
    pending: dict[str, object] = {}   # job key -> function to run; a newer request replaces a queued one
    lock = threading.Lock()

    def worker():
        while True:
            key = jobs.get()
            with lock:
                fn = pending.pop(key)
            try:
                fn()
            except Exception as e:  # keep the worker alive
                log(f"job {key} crashed: {e}")

    threading.Thread(target=worker, daemon=True).start()

    def enqueue(key: str, fn) -> bool:
        with lock:
            already = key in pending
            pending[key] = fn
        if already:
            return False  # already queued; it will pick up the newest request
        jobs.put(key)
        return True

    def deploy_app(name: str):
        def run_it():
            cfg = get()
            Deployer(cfg.apps[name], cfg.state_dir, cfg).deploy()
        return run_it

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
            cfg = get()
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "hook" or parts[1] not in cfg.apps:
                return self._reply(404, "not found")
            app = cfg.apps[parts[1]]
            secret = webhook_secret(app, cfg.state_dir)
            if not secret:
                return self._reply(500, "webhook secret not configured")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._reply(413, "payload too large")
            body = self.rfile.read(n)
            if not verify(secret, self.headers, body):
                return self._reply(401, "bad signature")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._reply(400, "bad json")
            event = self.headers.get("X-GitHub-Event", "push")
            if event == "ping":
                return self._reply(200, "pong")
            if event == "pull_request":
                return self._pull_request(cfg, app, payload)
            ref = payload.get("ref", "")
            if ref and ref != f"refs/heads/{app.branch}":
                return self._reply(200, f"ignored ref {ref}")
            queued = enqueue(app.name, deploy_app(app.name))
            self._reply(202, "queued" if queued else "already queued")

        def _pull_request(self, cfg, app, payload):
            if not app.previews:
                return self._reply(200, "previews are not enabled for this app")
            pr = parse_pr_event(payload)
            if pr is None:
                return self._reply(400, "bad pull_request payload")
            if not pr["same_repo"]:
                return self._reply(200, "ignored: pull request from a fork")
            n, key = pr["number"], f"{app.name}#pr{pr['number']}"
            if pr["action"] == "closed":
                queued = enqueue(key, lambda: Previews(get(), get().apps[app.name]).teardown(n))
                return self._reply(202, "teardown queued" if queued else "already queued")
            if pr["action"] in DEPLOY_ACTIONS and not pr["draft"]:
                queued = enqueue(key, lambda: Previews(get(), get().apps[app.name]).deploy(n, pr["ref"]))
                return self._reply(202, "preview queued" if queued else "already queued")
            return self._reply(200, f"ignored action {pr['action']}")

        def log_message(self, fmt, *args):
            log("http " + fmt % args)

    return ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), Handler)
