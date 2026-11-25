"""Install, version-check and launch logic for the launcher. No UI here.

On disk each game lives at  <home>/<game id>/<version>/<exe>  with
<home>/<game id>/installed.json naming the version in use, so the installed
version is always known without inspecting the exe.
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

GAMES_FILE = Path(__file__).with_name("games.json")


class LauncherError(Exception):
    pass


class NoSource(LauncherError):
    """The game has no update source configured in games.json."""


class NeedsSignIn(LauncherError):
    """GitHub hides the game's repo from people who are not signed in (or no longer are)."""


def home():
    override = os.environ.get("YMG_LAUNCHER_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YellowMakesGames"


def load_config():
    """games.json: {"launcher": <the launcher's own entry>, "games": [...],
    "tabs": [{"name": ..., "links": [{"label": ..., "url": ...}]}]}."""
    return json.loads(GAMES_FILE.read_text(encoding="utf-8"))


def load_games():
    return load_config()["games"]


def app_version():
    """The launcher's own version; the release build writes version.txt."""
    try:
        return Path(__file__).with_name("version.txt").read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _open(url, data=None, accept=None):
    if not url.lower().startswith(("https://", "http://")):
        raise LauncherError(f"Refusing non-http URL: {url}")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "ymg-launcher"})
    api = url.startswith("https://api.github.com/")
    if accept:
        req.add_header("Accept", accept)
    elif api and "/releases/assets/" in url:
        req.add_header("Accept", "application/octet-stream")  # the file, not its description
    token = github_token() if api else None
    if token:
        # Unredirected: GitHub sends downloads on to a storage server that
        # refuses requests carrying a second kind of authorization.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(req, timeout=20, context=_ssl_context())
    except urllib.error.HTTPError as e:
        if api and e.code == 401 and token:
            sign_out()
            raise NeedsSignIn("The GitHub sign-in has expired; sign in again") from e
        if api and e.code == 404:  # also GitHub's answer for a private repo you cannot see
            if token:
                raise LauncherError("GitHub found nothing your account can see there; ask for access") from e
            raise NeedsSignIn("Sign in with GitHub to see this game") from e
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
          The GitHub Releases of the repo that have that file attached; the
          version is the release's tag. "{version}" in the file name stands
          for that tag, e.g. "Game-{version}.zip". A private repo needs the
          user signed in with GitHub (see start_sign_in).
          With "manifest": "manifest-windows.json" as well, a release that has
          that file is installed from it instead (see install_manifest): only
          the files that changed are downloaded.
          With "prerelease": true, only the repo's prereleases are offered (a
          playtest channel, e.g. Jumpy Shooty's pt-v tags) and its normal
          releases are skipped; without it, the other way round.
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
        # Signed in, files come through the API, which (unlike the browser
        # link) also works for private repos.
        link = "url" if github_token() else "browser_download_url"
        found = []
        prerelease = bool(src.get("prerelease"))
        for rel in releases:
            if rel["draft"] or bool(rel["prerelease"]) != prerelease:
                continue
            files = {a["name"]: a[link] for a in rel["assets"]}
            _release_files[(src["repo"], rel["tag_name"])] = files
            wanted = src["asset"].replace("{version}", rel["tag_name"])
            if src.get("manifest") in files:
                found.append((rel["tag_name"], files[src["manifest"]]))
            elif wanted in files:
                found.append((rel["tag_name"], files[wanted]))
        if not found:
            raise LauncherError(f"No release of {src['repo']} has a file named {src['asset']} yet")
        return found
    raise LauncherError(f"Unknown source type: {kind}")


# (repo, tag) -> {file name: download URL}, from the last versions() call: where a
# manifest's files are, without asking GitHub again for each one.
_release_files = {}


def _release_file_url(repo, tag, name):
    files = _release_files.get((repo, tag))
    if files is None or name not in files:
        with _open(f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}") as r:
            rel = json.load(r)
        link = "url" if github_token() else "browser_download_url"
        files = _release_files[(repo, tag)] = {a["name"]: a[link] for a in rel["assets"]}
    if name not in files:
        raise LauncherError(f"Release {tag} of {repo} is missing {name}")
    return files[name]


def latest(game):
    """(latest version, download URL)."""
    return versions(game)[0]


def _key(version):
    m = re.fullmatch(r"(?:[A-Za-z]+-)?v?(\d+(?:\.\d+)*)", version)  # (a channel prefix: "pt-v0.1.3")
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
    """Download `url` (a zip, a bare exe, or a manifest: see install_manifest) and
    make it the installed version.

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
    try:
        _download(url, download, progress)
        manifest = None if zipfile.is_zipfile(download) else _read_manifest(download)
        if manifest is not None:
            download.unlink()
            install_manifest(game, manifest, tmp, progress)
        elif zipfile.is_zipfile(download):
            with zipfile.ZipFile(download) as z:
                z.extractall(tmp)
            download.unlink()
        else:
            download.rename(tmp / game["exe"])
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if not (tmp / game["exe"]).exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise LauncherError(f"Download did not contain {game['exe']}")

    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    (game_dir / "installed.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    for old in game_dir.iterdir():
        if old.is_dir() and old != dest:
            shutil.rmtree(old, ignore_errors=True)  # fails harmlessly if still running


# --- Installing from a manifest: only the files that changed ---
#
# A manifest (manifest-windows.json, attached to a release) lists every file of the
# game's folder:
#   {"format": 1, "version": "v1.2.3", "files": [
#       {"path": "packs/maps.pck", "size": 123, "sha256": "...",
#        "release": "v1.2.0", "asset": "0123abcd-maps.pck"}, ...]}
# Each file is in the release named, under that asset name (a file a release did not
# change stays where an earlier one put it), or at "url" for other hosts. A file the
# installed version already has, by its sha256, is hard-linked from there (no second
# copy on disk); only the rest are downloaded.

MANIFEST_FORMAT = 1


def _read_manifest(path):
    """The manifest in the file at `path`, or None if it is not one (a zip, an exe)."""
    if path.stat().st_size > 16 * 1024 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    if data.get("format") != MANIFEST_FORMAT:
        raise LauncherError("This version needs a newer launcher; update the launcher first")
    return data


def _safe_path(path):
    """A path inside the game's folder: relative, no "..", no drive."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    parts = path.split("/")
    return parts[0] != "" and ".." not in parts and "" not in parts and path != "manifest.json"


def _check_entry(e):
    if not (isinstance(e, dict) and _safe_path(e.get("path"))
            and isinstance(e.get("size"), int) and e["size"] >= 0
            and isinstance(e.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", e["sha256"])):
        raise LauncherError(f"The release's file list is damaged ({e!r:.80})")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:  # (another drive, or one without hard links)
        shutil.copy2(source, target)


def _manifest_file_url(game, e):
    src = game.get("source") or {}
    if src.get("type") == "github_release" and e.get("release") and e.get("asset"):
        return _release_file_url(src["repo"], e["release"], e["asset"])
    if e.get("url"):
        return e["url"]
    raise LauncherError(f"The release's file list does not say where {e['path']} is")


def install_manifest(game, manifest, folder, progress=None):
    """Fill `folder` with every file `manifest` lists: from the installed version when
    it has the same bytes, else downloaded (and checked against its sha256).

    progress(done_bytes, total_bytes) counts the downloads only.
    """
    files = manifest["files"]
    for e in files:
        _check_entry(e)
    old = installed_version(game)
    old_dir = exe_path(game, old).parent if old else None
    # Files of the installed version that may be reused: where its manifest put each
    # sha256, and (an install from a zip has no manifest) the same path. Each is hashed
    # before it is reused, so a damaged file is downloaded again, not carried on.
    claimed = {}
    if old_dir:
        try:
            before = json.loads((old_dir / "manifest.json").read_text(encoding="utf-8")).get("files", [])
        except (OSError, ValueError, AttributeError):
            before = []
        for e in before:
            if isinstance(e, dict) and _safe_path(e.get("path")):
                claimed.setdefault(e.get("sha256"), old_dir / e["path"])
    hashes = {}

    def reusable(e):
        for p in (claimed.get(e["sha256"]), old_dir / e["path"] if old_dir else None):
            if p is not None and p.is_file() and p.stat().st_size == e["size"]:
                if p not in hashes:
                    hashes[p] = _sha256(p)
                if hashes[p] == e["sha256"]:
                    return p
        return None

    todo = []
    for e in files:
        target = folder / e["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        source = reusable(e)
        if source:
            _link_or_copy(source, target)
        else:
            todo.append(e)

    total = sum(e["size"] for e in todo)
    done = 0
    for e in todo:
        part = folder / (e["path"] + ".part")
        report = (lambda d, _t, base=done: progress(base + d, total)) if progress else None
        _download(_manifest_file_url(game, e), part, report)
        if part.stat().st_size != e["size"] or _sha256(part) != e["sha256"]:
            raise LauncherError(f"{e['path']} arrived damaged; try again")
        part.rename(folder / e["path"])
        done += e["size"]
    if progress:
        progress(total, total)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return {"downloaded": len(todo), "reused": len(files) - len(todo), "bytes": total}


def uninstall(game):
    """Delete every installed version of the game. Fails if it is running."""
    game_dir = home() / game["id"]
    if game_dir.exists():
        try:
            shutil.rmtree(game_dir)
        except OSError as e:
            raise LauncherError(f"Could not remove {game['name']}; close the game and try again") from e


def launch(game):
    version = installed_version(game)
    if not version:
        raise LauncherError(f"{game['name']} is not installed")
    exe = exe_path(game, version)
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


# --- Browser games in their own window ---

def _edge():
    """Path to Microsoft Edge (part of Windows 10 and 11), or None."""
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                path = winreg.QueryValue(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")
            except OSError:
                continue
            if path and Path(path).exists():
                return path
    except ImportError:
        pass
    for base in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        path = Path(os.environ.get(base, "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if os.environ.get(base) and path.exists():
            return str(path)
    return None


def open_game_window(url):
    """Open a browser game in a window of its own: no tabs or address bar, its own taskbar entry.

    Uses Edge's app mode; returns False if Edge is missing, so the caller can
    fall back to the default browser.
    """
    if not url.lower().startswith("https://"):
        raise LauncherError(f"Refusing non-https URL: {url}")
    edge = _edge()
    if not edge:
        return False
    subprocess.Popen([edge, f"--app={url}", "--window-size=1280,800"])
    return True


# --- Signing in with GitHub, to see games in private repos ---
#
# GitHub's device flow: the launcher gets a short code, the user enters it at
# github.com/login/device, and the launcher receives a token. The token is
# for the "YMG Launcher" GitHub App, which can only read the repos it is
# installed on, and of those only the ones the user can see anyway.

GITHUB_CLIENT_ID = "Iv23liADSm9hKIB95XKY"  # identifies the app; not a secret
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_token_lock = threading.Lock()  # one refresh at a time; a refresh token works once


def _github_post(url, **fields):
    data = urllib.parse.urlencode({"client_id": GITHUB_CLIENT_ID, **fields}).encode()
    with _open(url, data=data, accept="application/json") as r:
        return json.load(r)


def start_sign_in():
    """Ask GitHub for a code: {"user_code", "verification_uri", "device_code", ...}.

    Show the user user_code and verification_uri, then call finish_sign_in.
    """
    return _github_post("https://github.com/login/device/code")


def finish_sign_in(device, cancelled=None):
    """Wait for the user to enter the code on GitHub, keep the token, return their login.

    cancelled: a threading.Event; set it to stop waiting.
    """
    cancelled = cancelled or threading.Event()
    interval = device.get("interval", 5)
    deadline = time.monotonic() + device.get("expires_in", 900)
    while time.monotonic() < deadline:
        if cancelled.wait(interval):
            raise LauncherError("Sign-in cancelled")
        reply = _github_post(_ACCESS_TOKEN_URL, device_code=device["device_code"],
                             grant_type="urn:ietf:params:oauth:grant-type:device_code")
        error = reply.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = reply.get("interval", interval + 5)
            continue
        if error == "access_denied":
            raise LauncherError("Sign-in was declined on GitHub")
        if error:
            raise LauncherError(f"GitHub sign-in failed: {reply.get('error_description', error)}")
        _write_token(_token_record(reply))
        try:
            with _open("https://api.github.com/user") as r:
                login = json.load(r)["login"]
        except LauncherError:
            sign_out()
            raise
        _write_token({**_read_token(), "login": login})
        return login
    raise LauncherError("The sign-in code expired; try again")


def _token_record(reply, login=None):
    record = {"token": reply["access_token"], "login": login}
    if reply.get("refresh_token"):  # an expiring token (GitHub's default: 8 hours)
        record["refresh_token"] = reply["refresh_token"]
        record["expires_at"] = time.time() + int(reply.get("expires_in", 8 * 3600))
    return record


def github_token():
    """The signed-in user's GitHub token, refreshed if it has run out, or None."""
    with _token_lock:
        record = _read_token()
        if not record:
            return None
        if record.get("expires_at", float("inf")) - 60 > time.time():
            return record["token"]
        if not record.get("refresh_token"):
            sign_out()
            return None
        try:
            reply = _github_post(_ACCESS_TOKEN_URL, grant_type="refresh_token",
                                 refresh_token=record["refresh_token"])
        except LauncherError:
            return None  # offline: carry on signed out, try again next time
        if "access_token" not in reply:
            sign_out()  # refresh refused: the user has to sign in again
            return None
        _write_token(_token_record(reply, record.get("login")))
        return reply["access_token"]


def github_login():
    """The signed-in GitHub username, or None."""
    record = _read_token()
    return record.get("login") if record else None


def sign_out():
    try:
        _token_file().unlink(missing_ok=True)
    except OSError:
        pass


def _token_file():
    return home() / "github-sign-in.bin"


def _read_token():
    try:
        return json.loads(_dpapi(_token_file().read_bytes(), protect=False))
    except (OSError, ValueError):
        return None


def _write_token(record):
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(json.dumps(record).encode(), protect=True))


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    """Encrypt (or decrypt) bytes so only this Windows user on this PC can read them."""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):  # 0x1: no prompts
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the GitHub sign-in")
    try:
        return ctypes.string_at(blob_out.data, blob_out.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(blob_out.data, ctypes.c_void_p))


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
