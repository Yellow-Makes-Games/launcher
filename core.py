"""Install, version-check and launch logic for the launcher. No UI here.

On disk each game lives at  <home>/<game id>/<version>/<exe>  with
<home>/<game id>/installed.json naming the version in use, so the installed
version is always known without inspecting the exe.
"""
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

GAMES_FILE = Path(__file__).with_name("games.json")


class LauncherError(Exception):
    pass


class NoSource(LauncherError):
    """The game has no update source configured in games.json."""


def home():
    override = os.environ.get("YMG_LAUNCHER_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YellowMakesGames"


def load_config():
    """games.json: {"launcher": <the launcher's own entry>, "games": [...]}."""
    return json.loads(GAMES_FILE.read_text(encoding="utf-8"))


def load_games():
    return load_config()["games"]


def app_version():
    """The launcher's own version; the release build writes version.txt."""
    try:
        return Path(__file__).with_name("version.txt").read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _open(url):
    if not url.lower().startswith(("https://", "http://")):
        raise LauncherError(f"Refusing non-http URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ymg-launcher"})
    try:
        return urllib.request.urlopen(req, timeout=20, context=_ssl_context())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise LauncherError("GitHub is limiting requests from this network; try again in a while") from e
        raise LauncherError(f"The server answered with error {e.code} for {url}") from e
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise LauncherError(f"The secure connection could not be verified ({reason.verify_message}); "
                                "antivirus or a work/school network may be intercepting it") from e
        if isinstance(reason, socket.gaierror):
            raise LauncherError("Could not find the server; check the internet connection") from e
        if isinstance(reason, (TimeoutError, ConnectionError)):
            raise LauncherError(f"Could not connect to the server ({reason}); "
                                "a firewall or proxy may be blocking the launcher") from e
        raise LauncherError(f"Network error: {reason}") from e
    except TimeoutError as e:
        raise LauncherError("The server took too long to answer") from e


def _ssl_context():
    """Windows' certificates plus certifi's, when the build includes it.

    Python only sees the root certificates Windows has already cached, so on
    some PCs GitHub fails to verify with the system store alone.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def exe_path(game, version):
    return home() / game["id"] / version / game["exe"]


def installed_version(game):
    """The installed version, or None if the game is not installed."""
    try:
        data = json.loads((home() / game["id"] / "installed.json").read_text(encoding="utf-8"))
        version = data["version"]
    except (OSError, ValueError, KeyError):
        return None
    return version if exe_path(game, version).exists() else None


def versions(game):
    """Ask the game's update source for [(version, download URL), ...], newest first.

    Source types (the "source" object in games.json):
      {"type": "manifest", "url": U}
          U returns JSON {"version": "v1.2.3", "url": "<zip or exe>"}; only
          that one version is offered.
      {"type": "github_release", "repo": "owner/name", "asset": "File.zip"}
          The GitHub Releases of a public repo that have that file attached;
          the version is the release's tag. "{version}" in the file name
          stands for that tag, e.g. "Game-{version}.zip".
    To support another kind of host, add a branch here.
    """
    src = game.get("source")
    if not src:
        raise NoSource(f"No update source set for {game['name']}")
    kind = src.get("type")
    if kind == "manifest":
        with _open(src["url"]) as r:
            data = json.load(r)
        return [(data["version"], data["url"])]
    if kind == "github_release":
        # Releases without the file are skipped: a release exists for a few
        # minutes before its build is attached, and that is not an error.
        with _open(f"https://api.github.com/repos/{src['repo']}/releases?per_page=100") as r:
            releases = json.load(r)
        found = []
        for rel in releases:
            if rel["draft"] or rel["prerelease"]:
                continue
            wanted = src["asset"].replace("{version}", rel["tag_name"])
            for asset in rel["assets"]:
                if asset["name"] == wanted:
                    found.append((rel["tag_name"], asset["browser_download_url"]))
        if not found:
            raise LauncherError(f"No release of {src['repo']} has a file named {src['asset']} yet")
        return found
    raise LauncherError(f"Unknown source type: {kind}")


def latest(game):
    """(latest version, download URL)."""
    return versions(game)[0]


def _key(version):
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version)
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def is_outdated(installed, newest):
    a, b = _key(installed), _key(newest)
    if a is None or b is None:
        return installed != newest  # not numeric: any difference means update
    return a < b


def _download(url, path, progress=None):
    with _open(url) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def install(game, version, url, progress=None):
    """Download `url` (a zip or a bare exe) and make it the installed version.

    progress(done_bytes, total_bytes_or_0) is called as the download runs.
    """
    # The version comes from the network and becomes a folder name.
    if not re.fullmatch(r"[\w.+-]+", version) or set(version) <= {"."}:
        raise LauncherError(f"Unusable version string: {version!r}")
    game_dir = home() / game["id"]
    dest = game_dir / version
    tmp = game_dir / (version + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    download = tmp / "download.tmp"
    _download(url, download, progress)

    if zipfile.is_zipfile(download):
        with zipfile.ZipFile(download) as z:
            z.extractall(tmp)
        download.unlink()
    else:
        download.rename(tmp / game["exe"])
    if not (tmp / game["exe"]).exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise LauncherError(f"Download did not contain {game['exe']}")

    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    (game_dir / "installed.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    for old in game_dir.iterdir():
        if old.is_dir() and old != dest:
            shutil.rmtree(old, ignore_errors=True)  # fails harmlessly if still running


def launch(game):
    version = installed_version(game)
    if not version:
        raise LauncherError(f"{game['name']} is not installed")
    exe = exe_path(game, version)
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


# --- The launcher updating itself (only when running as the built exe) ---

def launcher_update(entry):
    """(version, url) of a newer launcher exe, or None if there is none."""
    current = app_version()
    if current == "dev" or not getattr(sys, "frozen", False):
        return None  # running from source: nothing to replace
    version, url = latest(entry)
    return (version, url) if is_outdated(current, version) else None


def replace_self_and_restart(url, progress=None):
    """Download the new launcher exe over this one and start it.

    Windows will not overwrite a running exe but does allow renaming it, so
    the old one is moved aside and removed on the next start. The caller
    should close this process straight afterwards.
    """
    exe = Path(sys.executable)
    new, old = exe.with_suffix(".new"), exe.with_suffix(".old")
    _download(url, new, progress)
    with open(new, "rb") as f:
        if f.read(2) != b"MZ":
            new.unlink()
            raise LauncherError("Downloaded launcher is not a Windows program")
    old.unlink(missing_ok=True)
    exe.rename(old)
    new.rename(exe)
    # Without this the new one-file exe would reuse this one's unpacked files.
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)


def remove_old_launcher():
    if getattr(sys, "frozen", False):
        try:
            Path(sys.executable).with_suffix(".old").unlink(missing_ok=True)
        except OSError:
            pass  # previous launcher still closing; gone next start
