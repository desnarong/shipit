import hashlib
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


if __name__ == "__main__":
    unittest.main()
