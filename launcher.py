"""Yellow Makes Games launcher: one row per game in games.json.

Run with:  python launcher.py
"""
import os
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import core

WIDTH, HEIGHT = 960, 540
BACKGROUND = Path(__file__).with_name("background.png")  # 960x540, from Jumpy Shooty


class GameRow:
    def __init__(self, app, parent, game):
        self.app, self.game = app, game
        self.latest = None  # (version, url) once the check succeeds

        frame = ttk.Frame(parent, padding=16)
        frame.pack(fill="x")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=game["name"], font=("Segoe UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(frame, text="", font=("Segoe UI", 11))
        self.status.grid(row=1, column=0, sticky="w")
        self.folder_link = ttk.Label(frame, text="Open game folder", foreground="#0a58ca",
                                     cursor="hand2", font=("Segoe UI", 10, "underline"))
        self.folder_link.bind("<Button-1>", lambda e: self.open_folder())
        self.bar = ttk.Progressbar(frame, length=220)
        self.update_btn = ttk.Button(frame, text="Install", command=self.start_install)
        self.update_btn.grid(row=0, column=1, rowspan=2, padx=(12, 6))
        self.play_btn = ttk.Button(frame, text="Play", command=self.play)
        self.play_btn.grid(row=0, column=2, rowspan=2)
        ttk.Separator(parent).pack(fill="x")

        self.check()

    def check(self):
        self.render("Checking for updates…", busy=True)
        self.app.background(lambda: core.latest(self.game), self.checked)

    def checked(self, latest, error):
        self.latest = latest
        have = core.installed_version(self.game)
        if isinstance(error, core.NoSource):
            note = "no update source set"
        elif error:
            note = f"could not check for updates ({error})"
        elif not have:
            self.render(f"Not installed · latest is {latest[0]}")
            return
        elif core.is_outdated(have, latest[0]):
            self.render(f"Out of date · installed {have}, latest {latest[0]}")
            return
        else:
            note = "up to date"
        self.render(f"Installed {have} · {note}" if have else f"Not installed · {note}")

    def render(self, text, busy=False):
        have = core.installed_version(self.game)
        self.status.config(text=text)
        can_update = not busy and self.latest and (not have or core.is_outdated(have, self.latest[0]))
        self.update_btn.config(text="Update" if have else "Install",
                               state="normal" if can_update else "disabled")
        self.play_btn.config(state="normal" if have and not busy else "disabled")
        if have:
            self.folder_link.grid(row=3, column=0, sticky="w", pady=(4, 0))
        else:
            self.folder_link.grid_remove()

    def open_folder(self):
        version = core.installed_version(self.game)
        if version:
            os.startfile(core.exe_path(self.game, version).parent)  # opens Explorer

    def start_install(self):
        version, url = self.latest
        self.render(f"Downloading {version}…", busy=True)
        self.bar.grid(row=2, column=0, sticky="w", pady=(6, 0))

        def progress(done, total):
            self.app.post(self.bar.config, {"maximum": total or done, "value": done})

        self.app.background(lambda: core.install(self.game, version, url, progress), self.installed)

    def installed(self, _result, error):
        self.bar.grid_remove()
        if error:
            self.render(f"Update failed: {error}")
        else:
            self.checked(self.latest, None)

    def play(self):
        try:
            core.launch(self.game)
        except Exception as e:
            self.render(f"Could not start: {e}")


class App(tk.Tk):
    def __init__(self, games, launcher_entry=None):
        super().__init__()
        self.title("Yellow Makes Games")
        self.resizable(False, False)
        self.inbox = queue.Queue()  # worker threads must not touch tkinter
        self.drain()

        canvas = tk.Canvas(self, width=WIDTH, height=HEIGHT, highlightthickness=0, bg="#0b1020")
        canvas.pack()
        try:
            self.picture = tk.PhotoImage(file=str(BACKGROUND))  # keep a reference
            canvas.create_image(0, 0, anchor="nw", image=self.picture)
        except tk.TclError:
            pass  # missing picture: plain dark window
        canvas.create_text(36, 28, anchor="nw", text="Yellow Makes Games",
                           fill="white", font=("Segoe UI", 24, "bold"))
        self.canvas = canvas
        self.version_text = canvas.create_text(WIDTH - 36, 40, anchor="ne", fill="white", font=("Segoe UI", 11),
                                               text=f"Launcher {core.app_version()}")
        if launcher_entry:
            core.remove_old_launcher()
            self.background(lambda: core.launcher_update(launcher_entry), self.launcher_checked)
        panel = ttk.Frame(canvas)
        canvas.create_window(36, HEIGHT - 36, anchor="sw", window=panel, width=600)
        self.rows = [GameRow(self, panel, g) for g in games]
        ttk.Button(panel, text="Check again", command=lambda: [r.check() for r in self.rows]).pack(pady=10)

    def launcher_checked(self, update, error):
        if error or not update:
            return  # up to date, running from source, or offline: say nothing
        version, url = update
        self.self_btn = ttk.Button(self.canvas, text=f"Update launcher to {version}",
                                   command=lambda: self.update_launcher(version, url))
        self.canvas.create_window(WIDTH - 36, 68, anchor="ne", window=self.self_btn)

    def update_launcher(self, version, url):
        self.self_btn.config(state="disabled", text=f"Downloading launcher {version}…")
        self.background(lambda: core.replace_self_and_restart(url), self.launcher_replaced)

    def launcher_replaced(self, _result, error):
        if error:
            self.self_btn.config(state="normal", text=f"Launcher update failed: {error}")
        else:
            self.destroy()  # the new launcher is already starting

    def background(self, work, done):
        """Run work() off the UI thread, then done(result, error) on it."""
        def run():
            try:
                result, error = work(), None
            except Exception as e:  # shown to the user
                result, error = None, e
            self.post(done, result, error)
        threading.Thread(target=run, daemon=True).start()

    def post(self, fn, *args):
        """Thread-safe: have the UI thread call fn(*args)."""
        self.inbox.put((fn, args))

    def drain(self):
        while not self.inbox.empty():
            fn, args = self.inbox.get()
            fn(*args)
        self.after(50, self.drain)


if __name__ == "__main__":
    config = core.load_config()
    App(config["games"], config.get("launcher")).mainloop()
