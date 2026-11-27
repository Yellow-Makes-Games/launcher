"""End-to-end check against a local web server:  python -m unittest -v"""
import functools
import gc
import hashlib
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

# A page of website links, as games.json "tabs" can add.
LINKS = [{"name": "Bastion", "links": [
    {"label": "Open Bastion", "url": "https://bastion.yellowmakes.games"},
    {"label": "Open Bastion staging", "url": "https://bastion-staging.yellowmakes.games"}]}]


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
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.settle)  # runs after each test's app.destroy
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.game = {"id": "demo", "name": "Demo", "exe": "Demo.exe",
                     "source": {"type": "manifest", "url": f"{self.base}/latest.json"}}

    def settle(self):
        """Let the window's background work finish, then free the window here.

        Otherwise a worker thread can drop the last reference to a closed
        window, and Tk aborts the whole process ("Tcl_AsyncDelete: async
        handler deleted by the wrong thread").
        """
        for t in threading.enumerate():
            if t not in (threading.main_thread(), threading.current_thread(), self.server_thread):
                t.join(10)
        gc.collect()

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

    def publish_manifest(self, version, files):
        """A release as a manifest: files is {path: bytes}; each is served under its sha256."""
        entries = []
        for path, body in files.items():
            digest = hashlib.sha256(body).hexdigest()
            (self.site / digest).write_bytes(body)
            entries.append({"path": path, "size": len(body), "sha256": digest, "url": f"{self.base}/{digest}"})
        (self.site / f"manifest-{version}.json").write_text(json.dumps({"format": 1, "version": version, "files": entries}))
        (self.site / "latest.json").write_text(json.dumps({"version": version, "url": f"{self.base}/manifest-{version}.json"}))
        return entries

    def install_latest(self):
        """Installs the newest release; returns the URLs downloaded on the way."""
        with mock.patch("core._download", wraps=core._download) as download:
            core.install(self.game, *core.latest(self.game))
        return [c.args[0] for c in download.call_args_list]

    def installed(self, path):
        return (core.exe_path(self.game, core.installed_version(self.game)).parent / path).read_bytes()

    def test_manifest_downloads_only_what_changed(self):
        self.publish("v0.1.0")  # an install from a zip, as before manifests
        core.install(self.game, *core.latest(self.game))
        music = os.urandom(200_000)
        v2 = {"Demo.exe": b"build v0.1.0", "Demo.pck": b"code 2", "packs/music.pck": music,
              "packs/maps.pck": b"maps 2", "packs/thumbs.pck": b"thumbs"}
        entries = self.publish_manifest("v0.2.0", v2)
        got = self.install_latest()
        url = {e["path"]: e["url"] for e in entries}
        self.assertNotIn(url["Demo.exe"], got)  # the zip install's exe had the same bytes
        self.assertEqual(len(got), 1 + 4)       # the manifest, and the four new files
        self.assertEqual(core.installed_version(self.game), "v0.2.0")
        for path, body in v2.items():
            self.assertEqual(self.installed(path), body)
        self.assertIn('"v0.2.0"', self.installed("manifest.json").decode())
        self.assertFalse(core.exe_path(self.game, "v0.1.0").parent.exists())  # old version removed

        v3 = {**v2, "Demo.pck": b"code 3", "packs/maps.pck": b"maps 3"}
        del v3["packs/thumbs.pck"]
        entries = self.publish_manifest("v0.3.0", v3)
        url = {e["path"]: e["url"] for e in entries}
        seen = []
        with mock.patch("core._download", wraps=core._download) as download:
            core.install(self.game, *core.latest(self.game), lambda d, t: seen.append((d, t)))
        got = [c.args[0] for c in download.call_args_list]
        self.assertEqual(sorted(got[1:]), sorted([url["Demo.pck"], url["packs/maps.pck"]]))
        self.assertEqual(seen[-1], (12, 12))  # progress counts the two small downloads
        for path, body in v3.items():
            self.assertEqual(self.installed(path), body)
        folder = core.exe_path(self.game, "v0.3.0").parent
        self.assertFalse((folder / "packs/thumbs.pck").exists())  # the release dropped it
        self.assertEqual(sorted(p.name for p in folder.rglob("*.part")), [])

    def test_manifest_fixes_a_damaged_file(self):
        body = os.urandom(50_000)
        entries = self.publish_manifest("v1.0.0", {"Demo.exe": b"exe", "packs/a.pck": body})
        self.install_latest()
        folder = core.exe_path(self.game, "v1.0.0").parent
        with open(folder / "packs/a.pck", "r+b") as f:
            f.write(b"\0" * 8)  # the same size, other bytes
        self.publish_manifest("v1.0.1", {"Demo.exe": b"exe", "packs/a.pck": body})
        got = self.install_latest()
        self.assertIn(entries[1]["url"], got)
        self.assertEqual(self.installed("packs/a.pck"), body)

    def test_manifest_refuses_bad_files_and_keeps_the_install(self):
        self.publish_manifest("v1.0.0", {"Demo.exe": b"exe 1"})
        self.install_latest()
        entries = self.publish_manifest("v2.0.0", {"Demo.exe": b"exe 2"})
        manifest = {"format": 1, "version": "v2.0.0", "files": [{**entries[0], "sha256": "0" * 64}]}
        (self.site / "bad.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(core.LauncherError, "damaged"):
            core.install(self.game, "v2.0.0", f"{self.base}/bad.json")
        for path in ("../evil.exe", "/evil.exe", "C:/evil.exe", "a\\..\\b", "manifest.json"):
            manifest["files"] = [{**entries[0], "path": path}]
            (self.site / "bad.json").write_text(json.dumps(manifest))
            with self.assertRaises(core.LauncherError, msg=path):
                core.install(self.game, "v2.0.0", f"{self.base}/bad.json")
        (self.site / "bad.json").write_text(json.dumps({**manifest, "format": 2}))
        with self.assertRaisesRegex(core.LauncherError, "newer launcher"):
            core.install(self.game, "v2.0.0", f"{self.base}/bad.json")
        self.assertEqual(core.installed_version(self.game), "v1.0.0")
        self.assertEqual(self.installed("Demo.exe"), b"exe 1")
        self.assertEqual([p.name for p in core.home().joinpath("demo").iterdir() if p.name.endswith(".part")], [])

    def test_github_release_prefers_the_manifest(self):
        def rel(tag, files):
            return {"tag_name": tag, "draft": False, "prerelease": False,
                    "assets": [{"name": f, "browser_download_url": f"https://x/{tag}/{f}"} for f in files]}
        releases = [rel("v0.3.0", ["manifest-windows.json", "Demo-v0.3.0.zip", "ab-maps.pck"]),
                    rel("v0.2.0", ["Demo-v0.2.0.zip"])]
        game = {**self.game, "source": {"type": "github_release", "repo": "o/r", "asset": "Demo-{version}.zip",
                                        "manifest": "manifest-windows.json"}}
        with mock.patch("core._open", return_value=io.BytesIO(json.dumps(releases).encode())):
            self.assertEqual(core.versions(game), [("v0.3.0", "https://x/v0.3.0/manifest-windows.json"),
                                                   ("v0.2.0", "https://x/v0.2.0/Demo-v0.2.0.zip")])
        # A file of a release in that listing needs no more asking; one of an older release does.
        with mock.patch("core._open") as opened:
            self.assertEqual(core._manifest_file_url(game, {"path": "packs/maps.pck", "release": "v0.3.0",
                                                            "asset": "ab-maps.pck"}), "https://x/v0.3.0/ab-maps.pck")
            opened.assert_not_called()
            opened.return_value = io.BytesIO(json.dumps(rel("v0.1.0", ["cd-music.pck"])).encode())
            self.assertEqual(core._manifest_file_url(game, {"path": "packs/music.pck", "release": "v0.1.0",
                                                            "asset": "cd-music.pck"}), "https://x/v0.1.0/cd-music.pck")
            self.assertIn("/repos/o/r/releases/tags/v0.1.0", opened.call_args[0][0])
            opened.return_value = io.BytesIO(json.dumps(releases[0]).encode())  # (asked again: still not there)
            with self.assertRaisesRegex(core.LauncherError, "missing"):
                core._manifest_file_url(game, {"path": "x", "release": "v0.3.0", "asset": "missing.pck"})

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

    def test_github_release_lists_every_version_with_the_file(self):
        def rel(tag, files):
            return {"tag_name": tag, "draft": False, "prerelease": False,
                    "assets": [{"name": f, "browser_download_url": f"https://x/{f}"} for f in files]}
        releases = [rel("v0.3.0", []), rel("v0.2.0", ["Demo-v0.2.0.zip"]), rel("v0.1.0", ["Demo-v0.1.0.zip"])]
        game = {**self.game, "source": {"type": "github_release", "repo": "o/r", "asset": "Demo-{version}.zip"}}
        with mock.patch("core._open", return_value=io.BytesIO(json.dumps(releases).encode())):
            self.assertEqual(core.versions(game), [("v0.2.0", "https://x/Demo-v0.2.0.zip"),
                                                   ("v0.1.0", "https://x/Demo-v0.1.0.zip")])

    def test_prerelease_source_is_a_channel_of_its_own(self):
        def rel(tag, prerelease):
            return {"tag_name": tag, "draft": False, "prerelease": prerelease,
                    "assets": [{"name": f"Demo-{tag}.zip", "browser_download_url": f"https://x/{tag}.zip"}]}
        releases = [rel("v0.2.0", False), rel("pt-v0.1.1", True), rel("v0.1.0", False), rel("pt-v0.1.0", True)]
        public = {**self.game, "source": {"type": "github_release", "repo": "o/r", "asset": "Demo-{version}.zip"}}
        playtest = {**self.game, "source": {**public["source"], "prerelease": True}}
        with mock.patch("core._open", side_effect=lambda *a, **k: io.BytesIO(json.dumps(releases).encode())):
            self.assertEqual([v for v, _ in core.versions(public)], ["v0.2.0", "v0.1.0"])
            self.assertEqual([v for v, _ in core.versions(playtest)], ["pt-v0.1.1", "pt-v0.1.0"])
        self.assertTrue(core.is_outdated("pt-v0.1.9", "pt-v0.1.10"))
        self.assertFalse(core.is_outdated("pt-v0.1.10", "pt-v0.1.9"))

    def test_games_json_has_the_playtest_beside_the_game(self):
        games = {g["id"]: g for g in core.load_games()}
        self.assertTrue(games["jumpy-shooty-playtest"]["source"]["prerelease"])
        self.assertFalse(games["jumpy-shooty"]["source"].get("prerelease", False))
        self.assertEqual(len({g["name"] for g in games.values()}), len(games))  # one tab each

    def wait_for(self, app, row, text):
        deadline = time.time() + 10
        while text not in row.status:
            self.assertLess(time.time(), deadline, row.status)
            app.update()
            time.sleep(0.02)

    def test_window_can_go_back_to_an_older_version(self):
        import launcher
        self.publish("v0.1.0")
        self.publish("v0.2.0")
        offered = [("v0.2.0", f"{self.base}/Demo-v0.2.0.zip"), ("v0.1.0", f"{self.base}/Demo-v0.1.0.zip")]
        with mock.patch("core.versions", return_value=offered):
            app = launcher.App([self.game])
            self.addCleanup(app.destroy)
            row = app.rows[0]

            self.wait_for(app, row, "Not installed · latest is v0.2.0")
            self.assertEqual(app.primary.cget("text"), "INSTALL")
            self.assertEqual(app.version_btn.cget("text"), "Latest · v0.2.0   ▾")
            app.version_menu()
            self.assertEqual(len(app.menu.winfo_children()[0].winfo_children()), 2)  # one line per version
            app.close_menu()
            app.primary.invoke()
            self.wait_for(app, row, "Installed v0.2.0 · up to date")
            self.assertEqual(app.primary.cget("text"), "▶   PLAY")
            self.assertFalse(app.secondary.visible)  # nothing else to install

            row.choose(1)
            self.assertEqual(app.secondary.cget("text"), "Switch to v0.1.0")
            app.secondary.invoke()
            self.wait_for(app, row, "Out of date · installed v0.1.0, latest v0.2.0")
            self.assertEqual(core.exe_path(self.game, "v0.1.0").read_bytes(), b"build v0.1.0")
            self.assertFalse(core.exe_path(self.game, "v0.2.0").exists())
            self.assertEqual(row.badge[0], "UPDATE AVAILABLE")

            row.choose(0)
            self.assertEqual(app.secondary.cget("text"), "Update")

            with mock.patch("launcher.messagebox.askyesno", return_value=False):
                app.remove_all()
            self.assertEqual(core.installed_version(self.game), "v0.1.0")  # answered No: untouched
            with mock.patch("launcher.messagebox.askyesno", return_value=True) as asked:
                app.remove_all()
            self.assertIn("Demo", asked.call_args.kwargs["message"])
            self.assertIsNone(core.installed_version(self.game))
            self.assertFalse((core.home() / "demo").exists())
            self.assertEqual(row.status, "Not installed · latest is v0.2.0")
            self.assertEqual(app.primary.cget("text"), "INSTALL")
            with mock.patch("launcher.messagebox.showinfo") as info, \
                    mock.patch("launcher.messagebox.askyesno") as asked:
                app.remove_all()
            self.assertTrue(info.called and not asked.called)  # nothing left to remove

    def test_five_clicks_on_the_version_open_the_minigame(self):
        import launcher
        with mock.patch("core.versions", return_value=[]):
            app = launcher.App([self.game])
        self.addCleanup(app.destroy)
        for _ in range(4):
            app.version_clicked()
        self.assertIsNone(app.minigame)  # four is not enough
        app.version_clicked()
        game = app.minigame
        self.assertTrue(game.winfo_exists())
        app.version_clicked(); [app.version_clicked() for _ in range(4)]
        self.assertIs(app.minigame, game)  # brought back, not a second window

        game.held.add("right")
        for _ in range(30):
            game.step()
        self.assertGreater(game.x, 240)  # moved right
        game.lives = 0
        game.score = 7
        game.step()
        self.assertTrue(game.over)
        self.assertEqual((core.home() / "minigame-best.txt").read_text(), "7")
        game.restart()
        self.assertEqual((game.score, game.lives, game.over, game.best), (0, 3, False, 7))

    def test_tabs_switch_between_game_and_bastion(self):
        import launcher
        self.publish("v0.1.0")
        app = launcher.App([self.game], tabs=LINKS)
        self.addCleanup(app.destroy)
        self.assertEqual(list(app.tabs), ["Demo", "Bastion"])
        self.assertEqual(app.current, "Demo")
        self.assertEqual(app.canvas.itemcget(app.heading, "text"), "Demo")
        self.assertFalse(any(b.visible for b in app.link_buttons))

        app.show("Bastion")
        self.assertEqual(app.canvas.itemcget(app.heading, "text"), "Bastion")
        self.assertEqual(app.canvas.itemcget(app.watermark, "text"), "BASTION")  # no artwork: generated
        self.assertTrue(all(b.visible for b in app.link_buttons))
        self.assertFalse(app.primary.visible)
        with mock.patch("launcher.webbrowser.open") as opened:
            for button in app.link_buttons:
                button.invoke()
        self.assertEqual([c.args[0] for c in opened.call_args_list],
                         ["https://bastion.yellowmakes.games", "https://bastion-staging.yellowmakes.games"])

    def test_browser_game_opens_in_its_own_window(self):
        import launcher
        tabs = [{"name": "Civ", "links": [{"label": "Play Civ", "url": "https://civ.example", "window": True}]}]
        app = launcher.App([], tabs=tabs)
        self.addCleanup(app.destroy)
        button, = app.link_buttons
        self.assertEqual(button.cget("text"), "▶   Play Civ")
        self.assertIn("own window", app.canvas.itemcget(app.status, "text"))
        with mock.patch("core._edge", return_value=r"C:\Edge\msedge.exe"), \
                mock.patch("core.subprocess.Popen") as popen, mock.patch("launcher.webbrowser.open") as browser:
            button.invoke()
        self.assertEqual(popen.call_args[0][0][:2], [r"C:\Edge\msedge.exe", "--app=https://civ.example"])
        self.assertFalse(browser.called)
        with mock.patch("core._edge", return_value=None), mock.patch("launcher.webbrowser.open") as browser:
            button.invoke()  # no Edge: an ordinary browser tab
        self.assertEqual(browser.call_args[0][0], "https://civ.example")

    def test_tab_named_like_a_game_shares_its_page(self):
        import launcher
        self.publish("v0.1.0")
        app = launcher.App([{**self.game, "name": "Bastion"}], tabs=LINKS)
        self.addCleanup(app.destroy)
        self.assertEqual(list(app.tabs), ["Bastion"])
        self.assertEqual(len(app.rows), 1)
        self.assertEqual(len(app.link_buttons), 2)
        self.assertTrue(all(b.visible for b in app.link_buttons))  # under the game's own buttons
        self.assertTrue(app.primary.visible)

    def test_game_artwork_is_used_when_set(self):
        import launcher
        app = launcher.App([{**self.game, "source": None, "background": "background.png"}])
        self.addCleanup(app.destroy)
        self.assertEqual(app.canvas.itemcget(app.watermark, "text"), "")
        self.assertEqual(app.art_for("Demo")[0].width(), launcher.WIDTH)

    def test_sign_in_is_kept_encrypted_and_sign_out_forgets_it(self):
        self.assertIsNone(core.github_token())
        core._write_token({"token": "gh-secret", "login": "tim"})
        self.assertNotIn(b"gh-secret", core._token_file().read_bytes())
        self.assertEqual((core.github_token(), core.github_login()), ("gh-secret", "tim"))
        core.sign_out()
        self.assertIsNone(core.github_token())
        self.assertIsNone(core.github_login())

    def test_sign_in_waits_for_the_code_then_keeps_the_token(self):
        replies = iter([{"error": "authorization_pending"}, {"error": "slow_down", "interval": 0},
                        {"access_token": "t1", "refresh_token": "r1", "expires_in": 28800}])
        posts = []

        def post(url, **fields):
            posts.append((url, fields))
            return next(replies)

        cancelled = mock.Mock(wait=mock.Mock(return_value=False))
        with mock.patch("core._github_post", post), \
                mock.patch("core._open", return_value=io.BytesIO(b'{"login": "tim"}')) as opened:
            login = core.finish_sign_in({"device_code": "d", "interval": 0, "expires_in": 60}, cancelled)
        self.assertEqual(login, "tim")
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0][1]["device_code"], "d")
        self.assertEqual(opened.call_args[0][0], "https://api.github.com/user")
        self.assertEqual((core.github_token(), core.github_login()), ("t1", "tim"))

        cancelled.wait.return_value = True
        with self.assertRaisesRegex(core.LauncherError, "cancelled"):
            core.finish_sign_in({"device_code": "d"}, cancelled)
        with mock.patch("core._github_post", return_value={"error": "access_denied"}):
            cancelled.wait.return_value = False
            with self.assertRaisesRegex(core.LauncherError, "declined"):
                core.finish_sign_in({"device_code": "d"}, cancelled)

    def test_expired_token_is_refreshed_or_dropped(self):
        core._write_token({"token": "old", "refresh_token": "r1", "expires_at": time.time() - 1, "login": "tim"})
        with mock.patch("core._github_post", return_value={"access_token": "new", "refresh_token": "r2",
                                                           "expires_in": 28800}) as post:
            self.assertEqual(core.github_token(), "new")
        self.assertEqual(post.call_args.kwargs["refresh_token"], "r1")
        self.assertEqual((core.github_token(), core.github_login()), ("new", "tim"))  # no second refresh

        core._write_token({"token": "old", "refresh_token": "r2", "expires_at": time.time() - 1, "login": "tim"})
        with mock.patch("core._github_post", side_effect=core.LauncherError("offline")):
            self.assertIsNone(core.github_token())
        self.assertEqual(core.github_login(), "tim")  # kept for when the network is back
        with mock.patch("core._github_post", return_value={"error": "bad_refresh_token"}):
            self.assertIsNone(core.github_token())
        self.assertIsNone(core.github_login())

    def test_token_goes_only_to_the_github_api_and_not_past_redirects(self):
        core._write_token({"token": "tok", "login": "tim"})
        with mock.patch("core.urllib.request.urlopen") as urlopen:
            core._open("https://api.github.com/repos/o/r/releases/assets/7")
            core._open(f"{self.base}/latest.json")
        api, other = (c.args[0] for c in urlopen.call_args_list)
        self.assertEqual(api.unredirected_hdrs.get("Authorization"), "Bearer tok")
        self.assertNotIn("Authorization", api.headers)
        self.assertEqual(api.get_header("Accept"), "application/octet-stream")
        self.assertFalse(other.has_header("Authorization"))

    def test_private_repo_asks_to_sign_in(self):
        import urllib.error
        game = {**self.game, "source": {"type": "github_release", "repo": "o/secret", "asset": "Demo.zip"}}
        not_found = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("core.urllib.request.urlopen", side_effect=not_found):
            with self.assertRaises(core.NeedsSignIn):
                core.versions(game)
            core._write_token({"token": "tok", "login": "tim"})
            with self.assertRaisesRegex(core.LauncherError, "ask for access"):
                core.versions(game)
        # Signed in, downloads use the API link, which works for private repos.
        releases = [{"tag_name": "v1", "draft": False, "prerelease": False,
                     "assets": [{"name": "Demo.zip", "url": "https://api.github.com/repos/o/secret/releases/assets/1",
                                 "browser_download_url": "https://github.com/o/secret/releases/download/v1/Demo.zip"}]}]
        with mock.patch("core.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(releases).encode())):
            self.assertEqual(core.latest(game), ("v1", "https://api.github.com/repos/o/secret/releases/assets/1"))

    def test_window_sign_in_and_out(self):
        import launcher
        app = launcher.App([{**self.game, "source": None}])
        self.addCleanup(app.destroy)
        self.assertEqual(app.account_btn.cget("text"), "Sign in with GitHub")
        device = {"user_code": "AB12-CD34", "verification_uri": "https://github.com/login/device", "device_code": "d"}

        def finish(_device, _cancelled):
            core._write_token({"token": "tok", "login": "tim"})
            return "tim"

        with mock.patch("core.start_sign_in", return_value=device), \
                mock.patch("core.finish_sign_in", finish), mock.patch("launcher.webbrowser.open") as opened:
            app.account_btn.invoke()
            deadline = time.time() + 10
            while "tim" not in app.account_btn.cget("text"):
                self.assertLess(time.time(), deadline)
                app.update()
                time.sleep(0.02)
        self.assertEqual(opened.call_args[0][0], "https://github.com/login/device")
        self.assertEqual(app.clipboard_get(), "AB12-CD34")
        self.assertIsNone(app.sign_in_box)
        app.account_btn.invoke()  # signed in: a menu with Sign out
        self.assertIsNotNone(app.menu)
        app.close_menu()
        app.sign_out()
        self.assertEqual(app.account_btn.cget("text"), "Sign in with GitHub")
        self.assertIsNone(core.github_token())

    def test_network_errors_are_readable(self):
        dead = {**self.game, "source": {"type": "manifest", "url": "http://no-such-host.invalid/latest.json"}}
        with self.assertRaisesRegex(core.LauncherError, "Could not find the server"):
            core.latest(dead)
        missing = {**self.game, "source": {"type": "manifest", "url": f"{self.base}/nope.json"}}
        with self.assertRaisesRegex(core.LauncherError, "error 404"):
            core.latest(missing)

    def test_window(self):
        import launcher
        self.publish("v0.2.0")
        app = launcher.App([self.game, {**self.game, "id": "unset", "name": "Unset", "source": None}])
        self.addCleanup(app.destroy)
        row, unset = app.rows

        self.wait_for(app, row, "Not installed · latest is v0.2.0")
        self.assertEqual(app.primary.cget("text"), "INSTALL")
        self.assertEqual(app.primary.cget("state"), "normal")
        self.wait_for(app, unset, "Not installed · no update source set")
        app.show("Unset")
        self.assertEqual(app.primary.cget("state"), "disabled")
        self.assertFalse(app.version_btn.visible)
        app.show("Demo")

        app.primary.invoke()
        self.assertTrue(row.busy)
        self.assertEqual(app.canvas.itemcget(app.fill, "state"), "normal")  # download progress
        self.wait_for(app, row, "Installed v0.2.0 · up to date")
        self.assertEqual(app.canvas.itemcget(app.fill, "state"), "hidden")
        self.assertEqual(row.badge[0], "READY TO PLAY")
        with mock.patch("launcher.os.startfile") as startfile:
            row.open_folder()
        self.assertEqual(startfile.call_args[0][0], core.exe_path(self.game, "v0.2.0").parent)
        with mock.patch("core.subprocess.Popen") as popen:
            app.primary.invoke()  # Play
        self.assertEqual(popen.call_args[0][0], [str(core.exe_path(self.game, "v0.2.0"))])

        self.publish("v0.3.0")
        row.check()
        self.wait_for(app, row, "Out of date · installed v0.2.0, latest v0.3.0")
        self.assertEqual(app.secondary.cget("text"), "Update")
        app.secondary.invoke()
        self.wait_for(app, row, "Installed v0.3.0 · up to date")

if __name__ == "__main__":
    unittest.main()
