"""End-to-end check against a local web server:  python -m unittest -v"""
import functools
import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import core


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name, "site")
        self.site.mkdir()
        os.environ["YMG_LAUNCHER_HOME"] = str(Path(self.tmp.name, "home"))
        self.addCleanup(os.environ.pop, "YMG_LAUNCHER_HOME")

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.site))
        handler.log_message = lambda *a: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.game = {"id": "demo", "name": "Demo", "exe": "Demo.exe",
                     "source": {"type": "manifest", "url": f"{self.base}/latest.json"}}

    def publish(self, version, as_zip=True):
        body = f"build {version}".encode()
        if as_zip:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Demo.exe", body)
            name, body = f"Demo-{version}.zip", buf.getvalue()
        else:
            name = f"Demo-{version}.exe"
        (self.site / name).write_bytes(body)
        (self.site / "latest.json").write_text(json.dumps({"version": version, "url": f"{self.base}/{name}"}))

    def test_install_then_update(self):
        self.assertIsNone(core.installed_version(self.game))
        self.publish("v0.1.9")
        version, url = core.latest(self.game)
        seen = []
        core.install(self.game, version, url, lambda done, total: seen.append((done, total)))
        self.assertEqual(core.installed_version(self.game), "v0.1.9")
        self.assertEqual(core.exe_path(self.game, "v0.1.9").read_bytes(), b"build v0.1.9")
        self.assertTrue(seen and seen[-1][0] == seen[-1][1])
        self.assertFalse(core.is_outdated("v0.1.9", core.latest(self.game)[0]))

        self.publish("v0.1.10", as_zip=False)  # bare exe, and 10 > 9 numerically
        version, url = core.latest(self.game)
        self.assertTrue(core.is_outdated(core.installed_version(self.game), version))
        core.install(self.game, version, url)
        self.assertEqual(core.installed_version(self.game), "v0.1.10")
        self.assertEqual(core.exe_path(self.game, "v0.1.10").read_bytes(), b"build v0.1.10")
        self.assertFalse(core.exe_path(self.game, "v0.1.9").exists())  # old version removed

        with mock.patch("core.subprocess.Popen") as popen:
            core.launch(self.game)
        self.assertEqual(popen.call_args[0][0], [str(core.exe_path(self.game, "v0.1.10"))])

    def test_bad_downloads_leave_install_alone(self):
        self.publish("v1.0.0")
        core.install(self.game, *core.latest(self.game))
        (self.site / "latest.json").write_text(json.dumps({"version": "..", "url": f"{self.base}/Demo-v1.0.0.zip"}))
        with self.assertRaises(core.LauncherError):
            core.install(self.game, *core.latest(self.game))
        (self.site / "empty.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        with self.assertRaises(core.LauncherError):
            core.install(self.game, "v2.0.0", f"{self.base}/empty.zip")
        with self.assertRaises(core.LauncherError):
            core.install(self.game, "v2.0.0", "file:///C:/Windows/notepad.exe")
        self.assertEqual(core.installed_version(self.game), "v1.0.0")

    def test_no_source(self):
        with self.assertRaises(core.NoSource):
            core.latest({**self.game, "source": None})

    def test_github_release_picks_newest_with_the_file(self):
        def rel(tag, files, **extra):
            return {"tag_name": tag, "draft": False, "prerelease": False,
                    "assets": [{"name": f, "browser_download_url": f"https://x/{tag}/{f}"} for f in files], **extra}
        releases = [rel("v0.3.0", []), rel("v0.2.9", ["Demo.zip"], prerelease=True),
                    rel("v0.2.0", ["other.txt", "Demo.zip"]), rel("v0.1.0", ["Demo.zip"])]
        game = {**self.game, "source": {"type": "github_release", "repo": "o/r", "asset": "Demo.zip"}}
        with mock.patch("core._open", return_value=io.BytesIO(json.dumps(releases).encode())) as opened:
            self.assertEqual(core.latest(game), ("v0.2.0", "https://x/v0.2.0/Demo.zip"))
        self.assertIn("/repos/o/r/releases", opened.call_args[0][0])
        game["source"]["asset"] = "Demo-{version}.zip"
        named = [rel("v0.5.0", ["Demo-v0.4.0.zip"]), rel("v0.4.0", ["Demo-v0.4.0.zip"])]
        with mock.patch("core._open", return_value=io.BytesIO(json.dumps(named).encode())):
            self.assertEqual(core.latest(game), ("v0.4.0", "https://x/v0.4.0/Demo-v0.4.0.zip"))
        with mock.patch("core._open", return_value=io.BytesIO(b"[]")):
            with self.assertRaises(core.LauncherError):
                core.latest(game)

    def test_window(self):
        import launcher
        self.publish("v0.2.0")
        app = launcher.App([self.game, {**self.game, "id": "unset", "name": "Unset", "source": None}])
        self.addCleanup(app.destroy)
        row, unset = app.rows

        def wait_for(text):
            deadline = time.time() + 10
            while text not in row.status.cget("text"):
                self.assertLess(time.time(), deadline, row.status.cget("text"))
                app.update()
                time.sleep(0.02)

        wait_for("Not installed · latest is v0.2.0")
        self.assertEqual(str(row.play_btn.cget("state")), "disabled")
        self.assertEqual(unset.status.cget("text"), "Not installed · no update source set")
        self.assertEqual(str(unset.update_btn.cget("state")), "disabled")

        row.update_btn.invoke()
        wait_for("Installed v0.2.0 · up to date")
        self.assertEqual(str(row.play_btn.cget("state")), "normal")
        self.assertEqual(str(row.update_btn.cget("state")), "disabled")

        self.publish("v0.3.0")
        row.check()
        wait_for("Out of date · installed v0.2.0, latest v0.3.0")
        self.assertEqual(row.update_btn.cget("text"), "Update")
        row.update_btn.invoke()
        wait_for("Installed v0.3.0 · up to date")


if __name__ == "__main__":
    unittest.main()
