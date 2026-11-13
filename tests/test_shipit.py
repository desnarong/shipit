import hashlib
import os
import time
import hmac
import tempfile
import unittest
from pathlib import Path

from shipit.config import load
from shipit.detect import detect
from shipit.webhook import verify


def proj(*files: str):
    d = tempfile.TemporaryDirectory()
    for f in files:
        (Path(d.name) / f).write_text("")
    return d


class DetectTests(unittest.TestCase):
    def check(self, files, lang):
        d = proj(*files)
        self.addCleanup(d.cleanup)
        r = detect(d.name)
        self.assertEqual(r.lang, lang)
        self.assertTrue(r.dockerfile.startswith("FROM"))

    def test_languages(self):
        self.check(["package.json"], "node")
        self.check(["go.mod"], "go")
        self.check(["pom.xml"], "java-maven")
        self.check(["build.gradle.kts"], "java-gradle")
        self.check(["composer.json"], "php")
        self.check(["App.csproj"], "dotnet")
        self.check(["mix.exs"], "elixir")
        self.check(["requirements.txt", "app.py"], "python")
        self.check(["index.html"], "static")

    def test_rust_uses_package_name(self):
        d = proj()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "Cargo.toml").write_text('[package]\nname = "hello"\n')
        self.assertIn("release/hello", detect(d.name).dockerfile)

    def test_python_needs_start(self):
        d = proj("requirements.txt")
        self.addCleanup(d.cleanup)
        with self.assertRaises(ValueError):
            detect(d.name)
        self.assertIn("gunicorn", detect(d.name, start="gunicorn x:y").dockerfile)

    def test_unknown(self):
        d = proj("README")
        self.addCleanup(d.cleanup)
        with self.assertRaises(ValueError):
            detect(d.name)


class ConfigTests(unittest.TestCase):
    def test_load_and_validate(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.toml"
            f.write_text('[[app]]\nname="web"\nrepo="https://x/y.git"\n')
            self.assertIn("web", load(f).apps)
            f.write_text('[[app]]\nname="../evil"\nrepo="x"\n')
            with self.assertRaises(ValueError):
                load(f)


class WebhookTests(unittest.TestCase):
    def test_github_hmac(self):
        body = b'{"ref":"refs/heads/main"}'
        good = "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify("s3", {"X-Hub-Signature-256": good}, body))
        self.assertFalse(verify("s3", {"X-Hub-Signature-256": "sha256=00"}, body))

    def test_gitlab_token(self):
        self.assertTrue(verify("s3", {"X-Gitlab-Token": "s3"}, b""))
        self.assertFalse(verify("s3", {"X-Gitlab-Token": "no"}, b""))
        self.assertFalse(verify("s3", {}, b""))


class DeployFlowTests(unittest.TestCase):
    def make(self, healthy_results):
        from unittest import mock
        from shipit.config import App
        from shipit.deploy import Deployer
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Deployer(App(name="web", repo="x", notify_env="HOOK"), Path(tmp.name))
        d._save(current="shipit/web:old")
        results = iter(healthy_results)
        self.started = []
        patches = [
            mock.patch.object(d, "sync", return_value="abc"),
            mock.patch.object(d, "build", return_value="shipit/web:abc"),
            mock.patch.object(d, "_run_container"),
            mock.patch.object(d, "healthy", side_effect=lambda *a, **k: next(results)),
            mock.patch.object(d, "start", side_effect=self.started.append),
            mock.patch.object(d, "prune"),
            mock.patch.object(d, "notify"),
            mock.patch("shipit.deploy.run"),
            mock.patch("shipit.deploy._out", return_value="127.0.0.1:49153"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return d

    def test_failed_candidate_leaves_live_untouched(self):
        d = self.make([False])
        self.assertFalse(d.deploy())
        self.assertEqual(self.started, [])           # live container never swapped
        self.assertEqual(d.state()["current"], "shipit/web:old")
        self.assertEqual(d.state()["history"][-1]["status"], "failed")

    def test_success_swaps_and_records(self):
        d = self.make([True, True])
        self.assertTrue(d.deploy())
        self.assertEqual(self.started, ["shipit/web:abc"])
        s = d.state()
        self.assertEqual((s["current"], s["previous"]), ("shipit/web:abc", "shipit/web:old"))

    def test_failure_after_swap_rolls_back(self):
        d = self.make([True, False])
        self.assertFalse(d.deploy())
        self.assertEqual(self.started, ["shipit/web:abc", "shipit/web:old"])

    def test_history_capped(self):
        d = self.make([])
        for i in range(30):
            d._record("deployed", f"img{i}")
        self.assertEqual(len(d.state()["history"]), 20)

    def test_notify_payload(self):
        import json
        import os
        from unittest import mock
        from shipit.config import App
        from shipit.deploy import Deployer
        with tempfile.TemporaryDirectory() as t,              mock.patch.dict(os.environ, {"HOOK": "https://example.com/h"}),              mock.patch("urllib.request.urlopen") as uo:
            Deployer(App(name="web", repo="x", notify_env="HOOK"), Path(t)).notify("hi")
            req = uo.call_args[0][0]
            self.assertEqual(json.loads(req.data), {"text": "hi", "content": "hi"})
        with tempfile.TemporaryDirectory() as t,              mock.patch.dict(os.environ, {"HOOK": "file:///etc/passwd"}),              mock.patch("urllib.request.urlopen") as uo:
            Deployer(App(name="web", repo="x", notify_env="HOOK"), Path(t)).notify("hi")
            uo.assert_not_called()


class ProxyTests(unittest.TestCase):
    def test_build_config(self):
        from shipit.proxy import build_config
        c = build_config([("b.example.com", 8081), ("a.example.com", 8080)])
        routes = c["apps"]["http"]["servers"]["shipit"]["routes"]
        self.assertEqual([r["match"][0]["host"][0] for r in routes], ["a.example.com", "b.example.com"])
        self.assertEqual(routes[0]["handle"][0]["upstreams"][0]["dial"], "127.0.0.1:8080")

    def test_invalid_domain_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.toml"
            f.write_text('[[app]]\nname="web"\nrepo="x"\ndomain="a b;rm"\n')
            with self.assertRaises(ValueError):
                load(f)

    def make(self, healthy, caddy_ok=True):
        from unittest import mock
        from shipit.config import App, Config
        from shipit.deploy import Deployer
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(name="web", repo="x", domain="w.example.com", host_port=9000, drain_seconds=0)
        cfg = Config(Path(tmp.name), "0.0.0.0", 9000, "http://127.0.0.1:2019", {"web": app})
        d = Deployer(app, Path(tmp.name), cfg)
        self.synced = []
        self.runs = []
        def fake_sync(c, states):
            if not caddy_ok:
                raise OSError("down")
            self.synced.append(dict(states["web"]))
        patches = [
            mock.patch.object(d, "_run_container"),
            mock.patch.object(d, "healthy", return_value=healthy),
            mock.patch.object(d, "notify"),
            mock.patch("shipit.deploy.proxy.sync", side_effect=fake_sync),
            mock.patch("shipit.deploy.run", side_effect=lambda cmd, **k: self.runs.append(cmd)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return d

    def test_blue_green_flip(self):
        d = self.make(True)
        self.assertTrue(d._release_proxy("img1", None))
        self.assertEqual((d.state()["slot"], d.state()["port"]), ("blue", 9000))
        self.assertTrue(d._release_proxy("img2", "img1"))
        self.assertEqual((d.state()["slot"], d.state()["port"]), ("green", 9001))
        self.assertEqual(self.synced[-1]["port"], 9001)
        self.assertIn(["docker", "rm", "-f", "web-blue"], self.runs)   # old slot removed after cutover

    def test_unhealthy_never_reaches_caddy(self):
        d = self.make(False)
        self.assertFalse(d._release_proxy("img1", None))
        self.assertEqual(self.synced, [])
        self.assertNotIn("slot", d.state())

    def test_caddy_failure_keeps_old_version(self):
        d = self.make(True, caddy_ok=False)
        d._save(slot="blue", port=9000, current="img0")
        self.assertFalse(d._release_proxy("img1", "img0"))
        self.assertEqual((d.state()["slot"], d.state()["current"]), ("blue", "img0"))


class PythonInferTests(unittest.TestCase):
    def project(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, text in files.items():
            f = Path(d.name) / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return d.name

    def test_fastapi(self):
        r = detect(self.project({"requirements.txt": "fastapi\n", "main.py": "app = FastAPI()\n"}), port=9000)
        self.assertIn("uvicorn main:app --host 0.0.0.0 --port 9000", r.dockerfile)
        self.assertIn("pip install --no-cache-dir uvicorn", r.dockerfile)

    def test_flask_custom_var(self):
        r = detect(self.project({"requirements.txt": "flask\n", "app.py": "server = Flask(__name__)\n"}))
        self.assertIn("gunicorn app:server", r.dockerfile)

    def test_django(self):
        r = detect(self.project({"requirements.txt": "django\n", "manage.py": "", "mysite/wsgi.py": ""}))
        self.assertIn("gunicorn mysite.wsgi:application", r.dockerfile)

    def test_explicit_start_wins(self):
        r = detect(self.project({"requirements.txt": "flask\n", "app.py": "app = Flask(__name__)\n"}), start="python run.py")
        self.assertIn("python run.py", r.dockerfile)
        self.assertNotIn("gunicorn", r.dockerfile)


class HooksAndDoctorTests(unittest.TestCase):
    def test_failed_pre_deploy_aborts_before_anything_starts(self):
        from types import SimpleNamespace
        from unittest import mock
        from shipit.config import App
        from shipit.deploy import Deployer
        with tempfile.TemporaryDirectory() as t:
            d = Deployer(App(name="web", repo="x", pre_deploy="exit 1"), Path(t))
            with mock.patch.object(d, "sync", return_value="abc"), \
                 mock.patch.object(d, "build", return_value="shipit/web:abc"), \
                 mock.patch.object(d, "_run_container") as rc, \
                 mock.patch.object(d, "notify"), \
                 mock.patch("shipit.deploy.run", return_value=SimpleNamespace(returncode=1)):
                self.assertFalse(d.deploy())
                rc.assert_not_called()
            self.assertEqual(d.state()["history"][-1]["status"], "pre-deploy-failed")

    def test_doctor_flags_duplicate_ports_and_missing_secret(self):
        import os
        from unittest import mock
        from shipit import doctor
        from shipit.config import App, Config
        cfg = Config(Path("."), "0.0.0.0", 9000, "http://127.0.0.1:2019",
                     {"a": App(name="a", repo="x", host_port=8000), "b": App(name="b", repo="x", host_port=8000)})
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("shipit.doctor._docker_ok", return_value=(True, "ok")):
            os.environ.pop("SHIPIT_SECRET", None)
            res = {name: ok for name, ok, _ in doctor.checks(cfg)}
        self.assertFalse(res["b: port 8000"])
        self.assertTrue(res["a: webhook secret $SHIPIT_SECRET"])  # auto-generated secret is fine


class EasyTests(unittest.TestCase):
    def test_app_name_from_repo(self):
        from shipit.easy import app_name_from_repo as n
        self.assertEqual(n("https://github.com/me/My_App.git"), "my_app")
        self.assertEqual(n("git@github.com:me/cool-site.git"), "cool-site")
        self.assertEqual(n("https://github.com/me/Weird Name!/"), "weird-name")
        self.assertEqual(n("file:///tmp/repo"), "repo")

    def test_free_port_skips_taken_and_pairs(self):
        from unittest import mock
        from shipit.easy import free_port
        with mock.patch("shipit.easy._port_in_use", return_value=False):
            self.assertEqual(free_port(set(), 8080), 8080)
            self.assertEqual(free_port({8080}, 8080), 8082)   # 8080 taken -> next pair
            self.assertEqual(free_port({8081}, 8080), 8082)           # blue/green needs port+1 too

    def test_secret_is_stable_and_private(self):
        from shipit.easy import get_secret
        with tempfile.TemporaryDirectory() as t:
            a = get_secret(Path(t) / "state")
            self.assertEqual(a, get_secret(Path(t) / "state"))
            self.assertGreaterEqual(len(a), 32)

    def test_append_app_roundtrip(self):
        from shipit.easy import append_app
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "shipit.toml"
            append_app(f, {"name": "a", "repo": 'https://x/y "q".git', "branch": "main", "port": None, "host_port": 8080})
            append_app(f, {"name": "b", "repo": "r", "host_port": 8082, "domain": "b.example.com"})
            cfg = load(f)
            self.assertEqual(set(cfg.apps), {"a", "b"})
            self.assertEqual(cfg.apps["a"].repo, 'https://x/y "q".git')
            self.assertEqual(cfg.apps["b"].domain, "b.example.com")

    def test_default_branch_parses_symref(self):
        from unittest import mock
        from types import SimpleNamespace
        from shipit.easy import default_branch
        out = "ref: refs/heads/trunk\tHEAD\nabc123\tHEAD\n"
        with mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=out)):
            self.assertEqual(default_branch("x"), "trunk")
        with mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="")):
            self.assertEqual(default_branch("x"), "main")


class HooksTests(unittest.TestCase):
    def test_parse_github_repo(self):
        from shipit.hooks import parse_github_repo as p
        self.assertEqual(p("https://github.com/me/app.git"), ("me", "app"))
        self.assertEqual(p("git@github.com:me/app.git"), ("me", "app"))
        self.assertEqual(p("https://x-access-token:abc@github.com/me/app"), ("me", "app"))
        self.assertIsNone(p("https://gitlab.com/me/app.git"))

    def test_hook_url(self):
        from shipit.hooks import hook_url
        self.assertEqual(hook_url("https://d.example.com/", "web"), "https://d.example.com/hook/web")
        self.assertEqual(hook_url("https://d.example.com/hook/web", "web"), "https://d.example.com/hook/web")

    def test_register_creates_then_updates(self):
        import json
        from unittest import mock
        from shipit import hooks
        calls = []
        existing = []

        def fake_gh(method, path, body=None):
            calls.append((method, path, body))
            return json.dumps(existing) if method == "GET" else "{}"

        with mock.patch("shipit.hooks._gh", side_effect=fake_gh):
            self.assertEqual(hooks.register("https://github.com/me/app.git", "http://h/hook/app", "s3"), "created")
            self.assertEqual(calls[-1][:2], ("POST", "repos/me/app/hooks"))
            self.assertEqual(calls[-1][2]["config"]["secret"], "s3")
            self.assertEqual(calls[-1][2]["events"], ["push"])
            existing.append({"id": 7, "config": {"url": "http://h/hook/app"}})
            self.assertEqual(hooks.register("https://github.com/me/app.git", "http://h/hook/app", "s3"), "updated")
            self.assertEqual(calls[-1][:2], ("PATCH", "repos/me/app/hooks/7"))

    def test_register_rejects_non_github(self):
        from shipit import hooks
        with self.assertRaises(ValueError):
            hooks.register("https://gitlab.com/me/app.git", "http://h/hook/app", "s")

    def test_secret_never_on_command_line(self):
        from unittest import mock
        from types import SimpleNamespace
        from shipit import hooks
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="[]", stderr="")) as r:
            hooks._gh("POST", "repos/a/b/hooks", {"config": {"secret": "TOPSECRET"}})
            self.assertNotIn("TOPSECRET", " ".join(r.call_args[0][0]))
            self.assertIn("TOPSECRET", r.call_args.kwargs["input"])


class LiveConfigTests(unittest.TestCase):
    def test_reload_and_keep_last_good(self):
        from shipit.config import LiveConfig
        from shipit.easy import append_app
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "shipit.toml"
            append_app(f, {"name": "a", "repo": "r"})
            live = LiveConfig(f)
            self.assertEqual(set(live.get().apps), {"a"})
            append_app(f, {"name": "b", "repo": "r"})
            os.utime(f, ns=(1, time.time_ns()))           # make sure mtime differs
            self.assertEqual(set(live.get().apps), {"a", "b"})
            f.write_text("this is [not valid toml")
            os.utime(f, ns=(1, time.time_ns() + 10**9))
            self.assertEqual(set(live.get().apps), {"a", "b"})   # bad edit keeps last good config

    def test_state_dir_relative_to_config_file(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "shipit.toml"
            f.write_text('[server]\nstate_dir = "data"\n[[app]]\nname="a"\nrepo="r"\n')
            self.assertEqual(load(f).state_dir, (Path(t) / "data").resolve())


class DoctorWithoutAppsTests(unittest.TestCase):
    def test_doctor_works_without_config_file_or_apps(self):
        import contextlib
        import io
        from unittest import mock
        from shipit.cli import main
        with tempfile.TemporaryDirectory() as t, mock.patch("shipit.doctor._docker_ok", return_value=(True, "ok")):
            missing = str(Path(t) / "nope.toml")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["-c", missing, "doctor"])
            self.assertIn("no apps configured yet", buf.getvalue())
            self.assertIn("docker", buf.getvalue())
            self.assertIn(rc, (0, 1))          # 1 only if git/python are missing on this machine, never a crash
            empty_cfg = Path(t) / "empty.toml"
            empty_cfg.write_text("[server]\nport = 9000\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIn(main(["-c", str(empty_cfg), "doctor"]), (0, 1))

    def test_other_commands_still_require_an_app(self):
        from shipit.cli import main
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "empty.toml"
            f.write_text("[server]\nport = 9000\n")
            self.assertEqual(main(["-c", str(f), "list"]), 2)


class MonorepoTests(unittest.TestCase):
    def test_subdir_rejects_escape(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.toml"
            for bad in ("../x", "/abs", "a/../../b"):
                f.write_text(f'[[app]]\nname="a"\nrepo="r"\nsubdir="{bad}"\n')
                with self.assertRaises(ValueError, msg=bad):
                    load(f)
            f.write_text('[[app]]\nname="a"\nrepo="r"\nsubdir="services/api"\n')
            self.assertEqual(load(f).apps["a"].subdir, "services/api")

    def test_build_uses_subdir_for_detection_and_context(self):
        from unittest import mock
        from shipit.config import App
        from shipit.deploy import Deployer
        with tempfile.TemporaryDirectory() as t:
            d = Deployer(App(name="web", repo="x", subdir="services/api"), Path(t))
            (d.src / "services" / "api").mkdir(parents=True)
            (d.src / "package.json").write_text("{}")                      # a root-level decoy
            (d.src / "services" / "api" / "go.mod").write_text("module x\n")
            ran = []
            with mock.patch("shipit.deploy.run", side_effect=lambda cmd, **k: ran.append(cmd)), \
                 mock.patch.object(d, "_exposed_ports", return_value=[]):
                d.build("abc")
            self.assertEqual(ran[-1][-1], str(d.src / "services" / "api"))       # build context
            self.assertIn("golang", (d.dir / "Dockerfile.generated").read_text())  # detected the subdir, not the decoy

    def test_missing_subdir_is_a_clear_error(self):
        from shipit.config import App
        from shipit.deploy import Deployer
        with tempfile.TemporaryDirectory() as t:
            d = Deployer(App(name="web", repo="x", subdir="nope"), Path(t))
            d.src.mkdir(parents=True)
            with self.assertRaises(ValueError):
                d.build("abc")


class PreviewTests(unittest.TestCase):
    def pr(self, action="opened", number=7, ref="feature-x", head="me/app", base="me/app", draft=False):
        return {"action": action, "pull_request": {"number": number, "draft": draft,
                "head": {"ref": ref, "repo": {"full_name": head}}, "base": {"repo": {"full_name": base}}}}

    def test_parse_pr_event(self):
        from shipit.previews import parse_pr_event
        self.assertTrue(parse_pr_event(self.pr())["same_repo"])
        self.assertFalse(parse_pr_event(self.pr(head="evil/app"))["same_repo"])
        self.assertIsNone(parse_pr_event({"action": "opened"}))

    def make(self):
        from shipit.config import App, Config
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(name="web", repo="x", host_port=8080, domain="web.example.com", pre_deploy="migrate",
                  env_file="/prod.env", preview_env_file="/preview.env", previews=True)
        return Config(Path(tmp.name), "127.0.0.1", 9000, "http://127.0.0.1:2019", {"web": app}), app

    def test_preview_app_is_isolated_from_production(self):
        from unittest import mock
        from shipit.previews import Previews
        cfg, app = self.make()
        with mock.patch("shipit.previews.Deployer") as D, mock.patch("shipit.previews.free_port", return_value=20000):
            D.return_value.deploy.return_value = True
            self.assertTrue(Previews(cfg, app).deploy(7, "feature-x"))
            pa = D.call_args[0][0]
        self.assertEqual(pa.name, "web-pr-7")
        self.assertEqual((pa.branch, pa.host_port), ("feature-x", 20000))
        self.assertIsNone(pa.domain)
        self.assertIsNone(pa.pre_deploy)                 # never run production migrations for a preview
        self.assertEqual(pa.env_file, "/preview.env")    # never the production env file
        self.assertFalse(pa.previews)

    def test_same_pr_keeps_its_port_and_teardown_frees_it(self):
        from unittest import mock
        from shipit.previews import Previews
        cfg, app = self.make()
        ports = iter([20000, 20002])
        with mock.patch("shipit.previews.Deployer") as D, mock.patch("shipit.previews.free_port", side_effect=lambda *a: next(ports)):
            D.return_value.deploy.return_value = True
            p = Previews(cfg, app)
            p.deploy(7, "a")
            p.deploy(7, "a")                             # redeploy on push: same port
            self.assertEqual(D.call_args[0][0].host_port, 20000)
            p.deploy(8, "b")
            self.assertEqual(D.call_args[0][0].host_port, 20002)
            p.teardown(7)
            D.return_value.destroy.assert_called_once()
            self.assertNotIn("7", p._load())
            self.assertIn("8", p._load())

    def test_unsafe_branch_name_refused(self):
        from unittest import mock
        from shipit.previews import Previews
        cfg, app = self.make()
        with mock.patch("shipit.previews.Deployer") as D:
            self.assertFalse(Previews(cfg, app).deploy(1, "--upload-pack=evil"))
            D.assert_not_called()

    def test_webhook_routes_pull_requests(self):
        import hmac as _hmac
        import json
        import socket
        import threading
        import urllib.request
        from unittest import mock
        from shipit.config import App, Config
        from shipit.webhook import make_server
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = Config(Path(tmp.name), "127.0.0.1", port, "http://127.0.0.1:2019",
                     {"on": App(name="on", repo="x", previews=True), "off": App(name="off", repo="x")})
        done = threading.Event()
        calls = []

        class FakePreviews:
            def __init__(self, c, a):
                pass

            def deploy(self, n, ref):
                calls.append(("deploy", n, ref))
                done.set()

            def teardown(self, n):
                calls.append(("teardown", n))
                done.set()

        secret = "s3cret"
        os.environ["SHIPIT_SECRET"] = secret
        self.addCleanup(os.environ.pop, "SHIPIT_SECRET", None)
        with mock.patch("shipit.webhook.Previews", FakePreviews):
            srv = make_server(cfg)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)

            def post(app, payload, event="pull_request"):
                body = json.dumps(payload).encode()
                sig = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                req = urllib.request.Request(f"http://127.0.0.1:{port}/hook/{app}", data=body, method="POST",
                                             headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": event})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status

            self.assertEqual(post("on", {}, "ping"), 200)
            self.assertEqual(post("off", self.pr()), 200)                         # previews disabled
            self.assertEqual(post("on", self.pr(head="evil/app")), 200)           # fork: ignored
            self.assertEqual(post("on", self.pr(draft=True)), 200)                # drafts ignored
            self.assertEqual(post("on", self.pr(action="labeled")), 200)          # irrelevant action
            self.assertEqual(calls, [])
            self.assertEqual(post("on", self.pr()), 202)
            self.assertTrue(done.wait(5))
            done.clear()
            self.assertEqual(calls[-1], ("deploy", 7, "feature-x"))
            self.assertEqual(post("on", self.pr(action="closed")), 202)
            self.assertTrue(done.wait(5))
            self.assertEqual(calls[-1], ("teardown", 7))


if __name__ == "__main__":
    unittest.main()
