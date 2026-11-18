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

`{version}` stands for the release tag. The file can be a zip containing the exe, or the exe itself. The repo must be public. For other hosts use `{"type": "manifest", "url": "..."}`, where the URL returns `{"version": "v1.2.3", "url": "<zip or exe>"}`.

## Developing

```
python launcher.py        # run from source (shows "Launcher dev", never self-updates)
python -m unittest -v     # tests
```

Needs Python 3.11+ on Windows; no packages to install.
