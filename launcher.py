"""Yellow Makes Games launcher: one page per game in games.json, picked from a dropdown.

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

# Colours
PANEL, FIELD, FIELD_HOVER, EDGE = "#101830", "#232f4f", "#30406a", "#33426b"
TEXT, MUTED, FAINT = "#eef1f8", "#a3aecb", "#5f6b8c"
ACCENT, ACCENT_HOVER, INK, LINK = "#ffd21f", "#ffe26a", "#0b1020", "#8ab8ff"
OFF = "#192240"  # disabled buttons and fields
FONT = "Segoe UI"


def apply_theme(root):
    """Dark panel, flat buttons and one yellow accent, for every ttk widget."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PANEL, foreground=TEXT, font=(FONT, 10), borderwidth=0, focuscolor=PANEL)
    style.configure("TFrame", background=PANEL)
    style.configure("TLabel", background=PANEL, foreground=TEXT)
    style.configure("Status.TLabel", foreground=TEXT, font=(FONT, 12))
    style.configure("Link.TLabel", foreground=LINK, font=(FONT, 10))

    style.configure("TButton", background=FIELD, foreground=TEXT, padding=(16, 8), relief="flat",
                    font=(FONT, 10, "bold"))
    style.map("TButton", background=[("disabled", OFF), ("pressed", EDGE), ("active", FIELD_HOVER)],
              foreground=[("disabled", FAINT)])
    style.configure("Accent.TButton", background=ACCENT, foreground=INK)
    style.map("Accent.TButton", background=[("disabled", OFF), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
              foreground=[("disabled", FAINT)])
    style.configure("Quiet.TButton", background=PANEL, foreground=MUTED, padding=(10, 5), font=(FONT, 9))
    style.map("Quiet.TButton", background=[("active", FIELD)], foreground=[("active", TEXT)])

    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT, arrowcolor=TEXT,
                    bordercolor=EDGE, lightcolor=FIELD, darkcolor=FIELD, padding=(8, 6),
                    selectbackground=FIELD, selectforeground=TEXT)
    style.map("TCombobox", fieldbackground=[("disabled", OFF), ("readonly", FIELD)],
              foreground=[("disabled", FAINT)], arrowcolor=[("disabled", FAINT)],
              background=[("disabled", OFF), ("active", FIELD_HOVER)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                    bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)


class GameRow:
    def __init__(self, app, parent, game):
        self.app, self.game = app, game
        self.versions = []  # [(version, url), ...] newest first, once the check succeeds

        frame = ttk.Frame(parent, padding=(22, 20, 22, 8))
        frame.pack(fill="x")
        frame.columnconfigure(0, weight=1)
        self.status = ttk.Label(frame, text="", style="Status.TLabel", wraplength=380, justify="left")
        self.status.grid(row=1, column=0, sticky="w")
        self.folder_link = ttk.Label(frame, text="Open game folder  ›", style="Link.TLabel", cursor="hand2")
        self.folder_link.bind("<Button-1>", lambda e: self.open_folder())
        self.bar = ttk.Progressbar(frame, length=360)
        self.picker = ttk.Combobox(frame, state="disabled", width=15)  # which version to install
        self.picker.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self.picker.bind("<<ComboboxSelected>>", lambda e: self.render(self.status.cget("text")))
        self.update_btn = ttk.Button(frame, text="Install", width=16, command=self.start_install)
        self.update_btn.grid(row=0, column=2, rowspan=2, padx=8)
        self.play_btn = ttk.Button(frame, text="Play", width=9, command=self.play)
        self.play_btn.grid(row=0, column=3, rowspan=2)

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
        # The yellow button is the one thing to do next: Play, or Install if there is nothing to play.
        self.update_btn.config(text=action, state="normal" if can_act else "disabled",
                               style="TButton" if have else "Accent.TButton")
        self.play_btn.config(style="Accent.TButton" if have else "TButton")
        self.picker.config(state="readonly" if self.versions and not busy else "disabled")
        self.play_btn.config(state="normal" if have and not busy else "disabled")
        if have:
            self.folder_link.grid(row=3, column=0, sticky="w", pady=(8, 0))
        else:
            self.folder_link.grid_remove()

    def open_folder(self):
        version = core.installed_version(self.game)
        if version:
            os.startfile(core.exe_path(self.game, version).parent)  # opens Explorer

    def start_install(self):
        version, url = self.selected()
        self.render(f"Downloading {version}…", busy=True)
        self.bar.grid(row=2, column=0, sticky="w", pady=(8, 0))

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
        apply_theme(self)
        self.inbox = queue.Queue()  # worker threads must not touch tkinter
        self.drain()

        canvas = tk.Canvas(self, width=WIDTH, height=HEIGHT, highlightthickness=0, bg="#0b1020")
        canvas.pack()
        try:
            self.picture = tk.PhotoImage(file=str(BACKGROUND))  # keep a reference
            canvas.create_image(0, 0, anchor="nw", image=self.picture)
        except tk.TclError:
            pass  # missing picture: plain dark window
        canvas.create_text(36, 26, anchor="nw", text="YELLOW MAKES GAMES", fill=ACCENT, font=(FONT, 11, "bold"))
        self.heading = canvas.create_text(35, 44, anchor="nw", text="", fill="white", font=(FONT, 30, "bold"))
        self.canvas = canvas
        self.version_text = canvas.create_text(WIDTH - 36, 28, anchor="ne", fill=MUTED, font=(FONT, 10),
                                               text=f"Launcher {core.app_version()}")
        if launcher_entry:
            core.remove_old_launcher()
            self.background(lambda: core.launcher_update(launcher_entry), self.launcher_checked)
        # One page per game and per extra tab; the dropdown under the title picks
        # which one the box at the bottom shows.
        border = tk.Frame(canvas, bg=EDGE)  # 1px outline around the panel
        panel = ttk.Frame(border)
        panel.pack(fill="both", expand=True, padx=1, pady=1)
        canvas.create_window(36, HEIGHT - 36, anchor="sw", window=border, width=WIDTH - 72)
        self.pages = {}  # name in the dropdown -> its frame
        self.rows = []
        for game in games:
            page = ttk.Frame(panel)
            self.pages[game["name"]] = page
            self.rows.append(GameRow(self, page, game))
            footer = ttk.Frame(page, padding=(12, 4, 12, 10))
            footer.pack(fill="x")
            ttk.Button(footer, text="Remove all installs", style="Quiet.TButton",
                       command=self.remove_all).pack(side="right")
            ttk.Button(footer, text="Check for updates", style="Quiet.TButton",
                       command=lambda: [r.check() for r in self.rows]).pack(side="right")

        # Pages from games.json "tabs": a name plus buttons that open websites.
        self.link_buttons = []
        for tab in tabs:
            if tab["name"] in self.pages:  # a game of the same name: its links go under the game
                page = ttk.Frame(self.pages[tab["name"]], padding=(22, 0, 22, 12))
                page.pack(fill="x")
            else:
                page = ttk.Frame(panel, padding=(22, 18))
                self.pages[tab["name"]] = page
            for i, link in enumerate(tab["links"]):
                line = ttk.Frame(page)
                line.pack(fill="x", pady=5)
                button = ttk.Button(line, text=link["label"], width=22, style="TButton" if i else "Accent.TButton",
                                    command=lambda url=link["url"]: webbrowser.open(url))
                button.pack(side="left")
                ttk.Label(line, text=link["url"].split("://", 1)[-1], style="Status.TLabel").pack(side="left", padx=14)
                self.link_buttons.append(button)

        self.selector = ttk.Combobox(canvas, state="readonly", values=list(self.pages),
                                     width=22, font=(FONT, 11))
        self.selector.bind("<<ComboboxSelected>>", lambda e: self.show(self.selector.get()))
        canvas.create_window(38, 108, anchor="nw", window=self.selector)
        if self.pages:
            self.show(next(iter(self.pages)))

    def show(self, name):
        """Put the named page in the box and select it in the dropdown."""
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="x")
        self.selector.set(name)
        self.canvas.itemconfig(self.heading, text=name)
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
        self.self_btn = ttk.Button(self.canvas, text=f"Update launcher to {version}", style="Accent.TButton",
                                   command=lambda: self.update_launcher(version, url))
        self.canvas.create_window(WIDTH - 36, 54, anchor="ne", window=self.self_btn)

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
