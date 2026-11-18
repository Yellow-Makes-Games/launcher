"""Yellow Makes Games launcher: one row per game in games.json.

Run with:  python launcher.py
"""
import os
import queue
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import core

WIDTH, HEIGHT = 960, 540
ICON = Path(__file__).with_name("icon.ico")
BACKGROUND = Path(__file__).with_name("background.png")  # 960x540, from Jumpy Shooty


class GameRow:
    def __init__(self, app, parent, game):
        self.app, self.game = app, game
        self.versions = []  # [(version, url), ...] newest first, once the check succeeds

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
        self.picker = ttk.Combobox(frame, state="disabled", width=16)  # which version to install
        self.picker.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self.picker.bind("<<ComboboxSelected>>", lambda e: self.render(self.status.cget("text")))
        self.update_btn = ttk.Button(frame, text="Install", width=18, command=self.start_install)
        self.update_btn.grid(row=0, column=2, rowspan=2, padx=6)
        self.play_btn = ttk.Button(frame, text="Play", command=self.play)
        self.play_btn.grid(row=0, column=3, rowspan=2)
        ttk.Separator(parent).pack(fill="x")

        self.check()

    def check(self):
        self.render("Checking for updates…", busy=True)
        self.app.background(lambda: core.versions(self.game), self.checked)

    def checked(self, versions, error):
        self.versions = versions or []
        labels = [v if i else f"Latest ({v})" for i, (v, _) in enumerate(self.versions)]
        self.picker.config(values=labels)
        self.picker.set(labels[0] if labels else "")
        self.describe(error)

    def describe(self, error=None):
        have = core.installed_version(self.game)
        newest = self.versions[0][0] if self.versions else None
        if isinstance(error, core.NoSource):
            note = "no update source set"
        elif error:
            note = f"could not check for updates ({error})"
        elif not have:
            self.render(f"Not installed · latest is {newest}")
            return
        elif core.is_outdated(have, newest):
            self.render(f"Out of date · installed {have}, latest {newest}")
            return
        else:
            note = "up to date"
        self.render(f"Installed {have} · {note}" if have else f"Not installed · {note}")

    def selected(self):
        """The (version, url) chosen in the picker, or None before the check succeeds."""
        index = self.picker.current()
        return self.versions[index] if 0 <= index < len(self.versions) else None

    def render(self, text, busy=False):
        have = core.installed_version(self.game)
        choice = self.selected()
        self.status.config(text=text)
        if not choice or choice == self.versions[0]:
            action = "Update" if have else "Install"
        elif not have:
            action = f"Install {choice[0]}"
        elif core.is_outdated(have, choice[0]):
            action = f"Update to {choice[0]}"
        else:
            action = f"Switch to {choice[0]}"  # going back to an older version
        can_act = not busy and choice and choice[0] != have
        self.update_btn.config(text=action, state="normal" if can_act else "disabled")
        self.picker.config(state="readonly" if self.versions and not busy else "disabled")
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
        version, url = self.selected()
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
            self.describe()

    def play(self):
        try:
            core.launch(self.game)
        except Exception as e:
            self.render(f"Could not start: {e}")


class App(tk.Tk):
    def __init__(self, games, launcher_entry=None, tabs=()):
        super().__init__()
        self.title("Yellow Makes Games")
        try:
            self.iconbitmap(str(ICON))  # title bar and taskbar
        except tk.TclError:
            pass
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
        # One page per game and per extra tab; the dropdown under the title picks
        # which one the box at the bottom shows.
        panel = ttk.Frame(canvas)
        canvas.create_window(36, HEIGHT - 36, anchor="sw", window=panel, width=720)
        self.pages = {}  # name in the dropdown -> its frame
        self.rows = []
        for game in games:
            page = ttk.Frame(panel)
            self.pages[game["name"]] = page
            self.rows.append(GameRow(self, page, game))
            footer = ttk.Frame(page)
            footer.pack(pady=10)
            ttk.Button(footer, text="Check again",
                       command=lambda: [r.check() for r in self.rows]).pack(side="left", padx=4)
            ttk.Button(footer, text="Remove all installs", command=self.remove_all).pack(side="left", padx=4)

        # Extra pages from games.json "tabs": a name plus buttons that open websites.
        self.link_buttons = []
        for tab in tabs:
            page = ttk.Frame(panel, padding=16)
            self.pages[tab["name"]] = page
            for link in tab["links"]:
                line = ttk.Frame(page)
                line.pack(fill="x", pady=4)
                button = ttk.Button(line, text=link["label"], width=22,
                                    command=lambda url=link["url"]: webbrowser.open(url))
                button.pack(side="left")
                ttk.Label(line, text=link["url"].split("://", 1)[-1], font=("Segoe UI", 10)).pack(side="left", padx=10)
                self.link_buttons.append(button)

        self.selector = ttk.Combobox(canvas, state="readonly", values=list(self.pages),
                                     width=20, font=("Segoe UI", 12))
        self.selector.bind("<<ComboboxSelected>>", lambda e: self.show(self.selector.get()))
        canvas.create_window(38, 84, anchor="nw", window=self.selector)
        if self.pages:
            self.show(next(iter(self.pages)))

    def show(self, name):
        """Put the named page in the box and select it in the dropdown."""
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="x")
        self.selector.set(name)
        self.selector.selection_clear()

    def remove_all(self):
        installed = [r for r in self.rows if core.installed_version(r.game)]
        if not installed:
            messagebox.showinfo("Remove all installs", "No games are installed.", parent=self)
            return
        names = ", ".join(r.game["name"] for r in installed)
        if not messagebox.askyesno("Remove all installs", parent=self, icon="warning", default="no",
                                   message=f"Delete the installed files for {names}?",
                                   detail="You can install them again from the launcher."):
            return
        for row in installed:
            try:
                core.uninstall(row.game)
                row.describe()
            except core.LauncherError as e:
                row.render(str(e))

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
    try:  # own taskbar entry and icon when run from source, instead of Python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YellowMakesGames.Launcher")
    except (AttributeError, OSError):
        pass
    config = core.load_config()
    App(config["games"], config.get("launcher"), config.get("tabs", ())).mainloop()
