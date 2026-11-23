# Yellow Makes Games launcher

A small Windows app that installs, updates and starts our games, and updates itself.

Download `YMGLauncher.exe` from the [latest release](../../releases/latest) and run it.

## How it works

- Games are listed in `games.json`. Each has an update source; the launcher asks it for the newest version and compares that with what is installed.
- Games install to `%LOCALAPPDATA%\YellowMakesGames\<game>\<version>\`, with `installed.json` naming the version in use.
- Every push to `main` is tagged with the next version and released as `YMGLauncher.exe` (see `.github/workflows/release.yml`). A running launcher that finds a newer release offers to update itself.

## Adding a game

Add an entry to `games.json`:

```json
{
  "id": "my-game",
  "name": "My Game",
  "exe": "MyGame.exe",
  "source": {"type": "github_release", "repo": "Yellow-Makes-Games/My-Game", "asset": "MyGame-windows-{version}.zip"}
}
```

`{version}` stands for the release tag. The file can be a zip containing the exe, or the exe itself. For other hosts use `{"type": "manifest", "url": "..."}`, where the URL returns `{"version": "v1.2.3", "url": "<zip or exe>"}`.

### Browser games

A game that is a website goes under `"tabs"`, as a page of links:

```json
{"name": "Civalia", "links": [{"label": "Play Civalia", "url": "https://civalia.yellowmakes.games", "window": true}]}
```

With `"window": true` the site opens in a window of its own (Edge's app mode: no tabs or address bar, its own taskbar entry); without it, or on a PC without Edge, it opens in the default browser. A tab with the same name as a game adds its links under that game's buttons. Any page can show its own picture with `"background": "file.png"` (960×540, shipped next to the exe).

### Games in private repos

Players click **Sign in with GitHub** (top right) and enter the code shown at github.com/login/device. The sign-in belongs to the org's **YMG Launcher** GitHub App (read-only access to repository contents), so the launcher can only read repos that are:

1. ones the app is installed on (org settings → GitHub Apps → YMG Launcher → Configure → add the repo), **and**
2. ones the player's GitHub account can see.

The sign-in is kept in `%LOCALAPPDATA%\YellowMakesGames\github-sign-in.bin`, encrypted so only that Windows user can read it.

## Developing

```
python launcher.py        # run from source (shows "Launcher dev", never self-updates)
python -m unittest -v     # tests
```

Needs Python 3.11+ on Windows; no packages to install.
