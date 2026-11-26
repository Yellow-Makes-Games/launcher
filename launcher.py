"""Yellow Makes Games launcher: one page per game in games.json, picked from the tabs along the top.

Run with:  python launcher.py

Everything is drawn on one Tk canvas. Shading and rounded buttons are small
PNGs made at startup (see png()), because Tk blends PNG transparency over
whatever is under it but cannot draw transparent shapes itself.
"""
import base64
import math
import os
import queue
import struct
import threading
import webbrowser
import zlib
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import core

WIDTH, HEIGHT = 960, 540
LAUNCHER_CHECK_MS = 30 * 60 * 1000  # how often an open launcher looks for a newer version of itself
HERE = Path(__file__).parent
ICON = HERE / "icon.ico"

# Colours
INK = "#070b18"  # behind everything; the shading fades to this
PANEL, FIELD_HOVER, EDGE = "#101830", "#232f4f", "#33426b"  # pop-up menus and the sign-in box
TEXT, MUTED, FAINT = "#f3f5fb", "#a3aecb", "#6b7799"
ACCENT, ACCENT_HOVER, GOOD = "#ffd21f", "#ffe26a", "#5ee0a0"
FONT, DISPLAY = "Segoe UI", "Segoe UI Black"


def rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


# --- Images made in code ---

def png(master, width, rows):
    """A PhotoImage from rows of RGBA bytes."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + bytes(row) for row in rows)
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return tk.PhotoImage(master=master, data=base64.b64encode(data))


def fade(master, width, height, alpha, across):
    """INK at a varying opacity: alpha(t) for t from 0 to 1 across (x) or down (y) the image."""
    r, g, b = rgb(INK)
    if across:
        row = b"".join(bytes((r, g, b, round(255 * alpha(x / width)))) for x in range(width))
        return png(master, width, [row] * height)
    return png(master, width, [bytes((r, g, b, round(255 * alpha(y / height)))) * width for y in range(height)])


def rounded(master, width, height, radius, fill, border=None):
    """A rounded rectangle with smooth corners. fill and border are (colour, opacity)."""
    (fr, fg, fb), fa = rgb(fill[0]), fill[1]
    (br, bg, bb), ba = rgb(border[0]) if border else (0, 0, 0), border[1] if border else 0
    rows = []
    for y in range(height):
        py = y + 0.5
        cy = min(max(py, radius), height - radius)
        row = bytearray()
        for x in range(width):
            px = x + 0.5
            cx = min(max(px, radius), width - radius)
            d = math.hypot(px - cx, py - cy)
            outer = min(max(radius - d + 0.5, 0.0), 1.0)
            inner = min(max(radius - 1 - d + 0.5, 0.0), 1.0) if border else outer
            fill_a, edge_a = fa * inner, ba * (outer - inner)
            a = fill_a + edge_a
            if a <= 0:
                row += b"\0\0\0\0"
                continue
            row += bytes((round((fr * fill_a + br * edge_a) / a), round((fg * fill_a + bg * edge_a) / a),
                          round((fb * fill_a + bb * edge_a) / a), round(255 * a)))
        rows.append(row)
    return png(master, width, rows)


def generated_art(master):
    """Backdrop for a game without artwork: dark blue with soft coloured glows."""
    w, h, scale = 320, 180, 3
    glows = [(0.78, 0.22, 0.34, (255, 150, 40), 0.24), (1.0, 1.0, 0.50, (50, 100, 255), 0.42),
             (0.35, -0.15, 0.45, (120, 70, 230), 0.22)]
    top, bottom = (16, 23, 52), (5, 7, 15)
    rows = []
    for y in range(h):
        t = y / h
        base = [top[i] + (bottom[i] - top[i]) * t for i in range(3)]
        row = bytearray()
        for x in range(w):
            c = list(base)
            for gx, gy, size, colour, strength in glows:
                dx, dy = x / w - gx, (y / h - gy) * h / w
                a = strength * math.exp(-(dx * dx + dy * dy) / (size * size) * 3)
                for i in range(3):
                    c[i] += (colour[i] - c[i]) * a
            row += bytes((round(c[0]), round(c[1]), round(c[2]), 255))
        rows.append(row)
    return png(master, w, rows).zoom(scale)


# --- Pieces of the window ---

class Button:
    """A rounded button drawn on the canvas.

    kind "accent" is the yellow one (the next thing to do), "ghost" is see-through.
    Same calls as a Tk button where the code needs them: config, cget, invoke.
    """

    def __init__(self, app, text, command, kind="ghost", height=40, width=None, font=(FONT, 10, "bold"),
                 radius=10):
        self.app, self.canvas, self.command = app, app.canvas, command
        self.kind, self.height, self.fixed_width, self.radius = kind, height, width, radius
        self.font = tkfont.Font(root=app, family=font[0], size=font[1], weight=font[2] if len(font) > 2 else "normal")
        self.state, self.hover, self.visible = "normal", False, False
        self.x = self.y = 0
        self.bg = self.canvas.create_image(0, 0, anchor="nw", state="hidden")
        self.label = self.canvas.create_text(0, 0, text=text, font=self.font, state="hidden")
        self.canvas.tag_bind(self.bg, "<Enter>", lambda e: self.set_hover(True))
        self.canvas.tag_bind(self.bg, "<Leave>", lambda e: self.set_hover(False))
        self.canvas.tag_bind(self.bg, "<ButtonRelease-1>", lambda e: self.invoke())

    @property
    def width(self):
        return self.fixed_width or self.font.measure(self.cget("text")) + 2 * 22

    def config(self, **options):
        if "text" in options:
            self.canvas.itemconfigure(self.label, text=options.pop("text"))
        self.__dict__.update(options)  # state, kind, command
        if self.visible:
            self.place(self.x, self.y)

    def cget(self, option):
        return self.canvas.itemcget(self.label, "text") if option == "text" else getattr(self, option)

    def invoke(self):
        if self.state == "normal" and self.visible and self.command:
            self.command()

    def place(self, x, y):
        """Show the button with its top-left corner at x, y; returns its right edge."""
        self.x, self.y, self.visible = x, y, True
        w, h = self.width, self.height
        if self.state != "normal":
            look, ink = (("#ffffff", 0.05), None), FAINT
        elif self.kind == "accent":
            look, ink = ((ACCENT_HOVER if self.hover else ACCENT, 1.0), None), INK
        else:
            look, ink = (("#ffffff", 0.16 if self.hover else 0.08), ("#ffffff", 0.45 if self.hover else 0.22)), TEXT
        fill, border = look
        image = self.app.image("rounded", w, h, self.radius, *fill, *(border or ()))
        self.canvas.itemconfigure(self.bg, image=image, state="normal")
        self.canvas.coords(self.bg, x, y)
        # "disabled" lets the mouse fall through the text to the picture under it.
        self.canvas.itemconfigure(self.label, state="disabled", fill=ink, disabledfill=ink)
        self.canvas.coords(self.label, x + w / 2, y + h / 2)
        self.canvas.tag_raise(self.bg)
        self.canvas.tag_raise(self.label)
        return x + w

    def hide(self):
        self.visible = False
        self.canvas.itemconfigure(self.bg, state="hidden")
        self.canvas.itemconfigure(self.label, state="hidden")

    def set_hover(self, on):
        self.hover = on
        self.canvas.config(cursor="hand2" if on and self.state == "normal" else "")
        if self.visible:
            self.place(self.x, self.y)


class Menu(tk.Toplevel):
    """A small dark pop-up list. items: [(label, command or None to grey it out)]."""

    def __init__(self, app, x, y, items):
        super().__init__(app, bg=EDGE)
        self.overrideredirect(True)
        body = tk.Frame(self, bg=PANEL, padx=4, pady=4)
        body.pack(padx=1, pady=1)
        for label, command in items:
            row = tk.Label(body, text=label, anchor="w", bg=PANEL, fg=TEXT if command else FAINT,
                           font=(FONT, 10), padx=14, pady=6, width=24, cursor="hand2" if command else "")
            row.pack(fill="x")
            if command:
                row.bind("<Enter>", lambda e, r=row: r.config(bg=FIELD_HOVER))
                row.bind("<Leave>", lambda e, r=row: r.config(bg=PANEL))
                row.bind("<ButtonRelease-1>", lambda e, c=command: (app.close_menu(), c()))
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda e: app.close_menu())
        self.focus_force()


class SignInBox(tk.Toplevel):
    """Shows the code to enter on GitHub while the launcher waits for it."""

    def __init__(self, app, device, cancelled):
        super().__init__(app, bg=PANEL)
        self.title("Sign in with GitHub")
        self.resizable(False, False)
        self.transient(app)
        self.cancelled = cancelled
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        code, url = device["user_code"], device["verification_uri"]
        self.canvas = tk.Canvas(self, width=440, height=230, bg=PANEL, highlightthickness=0)
        self.canvas.pack()
        self.images = app.images  # the buttons reuse the main window's pictures
        c = self.canvas
        c.create_text(32, 30, anchor="nw", text="SIGN IN WITH GITHUB", fill=ACCENT, font=(FONT, 9, "bold"))
        c.create_text(32, 54, anchor="nw", text="Enter this code on GitHub:", fill=TEXT, font=(FONT, 11))
        c.create_text(30, 80, anchor="nw", text=code, fill=TEXT, font=(DISPLAY, 30))
        c.create_text(32, 136, anchor="nw", fill=MUTED, font=(FONT, 9),
                      text=f"It is already copied; paste it at {url.split('://', 1)[-1]}")
        right = Button(self, "Open GitHub", lambda: webbrowser.open(url), kind="accent").place(32, 166)
        Button(self, "Cancel", self.cancel).place(right + 10, 166)
        self.clipboard_clear()
        self.clipboard_append(code)
        webbrowser.open(url)

    def image(self, *key):
        return self.master.image(*key)

    def cancel(self):
        self.cancelled.set()  # the background wait stops within a few seconds
        self.destroy()


class GameRow:
    """One game's state: what is installed, what can be installed, and what is happening.

    The window draws whichever game is showing; this class only keeps the facts
    and tells the window when they change.
    """

    def __init__(self, app, game):
        self.app, self.game = app, game
        self.versions = []  # [(version, url), ...] newest first, once the check succeeds
        self.choice = 0  # index into versions: the one to install
        self.busy = False
        self.status = ""
        self.badge = ("", MUTED)  # short label above the title, and its colour
        self.progress = None  # (done bytes, total bytes) while downloading
        self.check()

    def changed(self):
        self.app.refresh(self)

    def check(self):
        self.busy, self.status, self.badge = True, "Checking for updates…", ("CHECKING", MUTED)
        self.changed()
        self.app.background(lambda: core.versions(self.game), self.checked)

    def checked(self, versions, error):
        self.versions, self.choice = versions or [], 0
        self.describe(error)

    def describe(self, error=None):
        self.busy = False
        have = core.installed_version(self.game)
        newest = self.versions[0][0] if self.versions else None
        if isinstance(error, core.NoSource):
            note, self.badge = "no update source set", ("INSTALLED" if have else "NOT AVAILABLE", MUTED)
        elif isinstance(error, core.NeedsSignIn):
            note, self.badge = "sign in with GitHub (top right) to get this game", ("SIGN IN NEEDED", ACCENT)
        elif error:
            note, self.badge = f"could not check for updates ({error})", ("OFFLINE", MUTED)
        elif not have:
            self.status, self.badge = f"Not installed · latest is {newest}", ("NOT INSTALLED", MUTED)
            return self.changed()
        elif core.is_outdated(have, newest):
            self.status, self.badge = f"Out of date · installed {have}, latest {newest}", ("UPDATE AVAILABLE", ACCENT)
            return self.changed()
        else:
            note, self.badge = "up to date", ("READY TO PLAY", GOOD)
        self.status = f"Installed {have} · {note}" if have else f"Not installed · {note}"
        self.changed()

    def selected(self):
        """The (version, url) chosen to install, or None before the check succeeds."""
        return self.versions[self.choice] if 0 <= self.choice < len(self.versions) else None

    def choose(self, index):
        self.choice = index
        self.changed()

    def action(self):
        """(label, possible) for the install button, for the chosen version."""
        have, choice = core.installed_version(self.game), self.selected()
        if not choice or choice == self.versions[0]:
            label = "Update" if have else "Install"
        elif not have:
            label = f"Install {choice[0]}"
        elif core.is_outdated(have, choice[0]):
            label = f"Update to {choice[0]}"
        else:
            label = f"Switch to {choice[0]}"  # going back to an older version
        return label, bool(not self.busy and choice and choice[0] != have)

    def open_folder(self):
        version = core.installed_version(self.game)
        if version:
            os.startfile(core.exe_path(self.game, version).parent)  # opens Explorer

    def start_install(self):
        version, url = self.selected()
        self.busy, self.status, self.badge = True, f"Downloading {version}…", ("DOWNLOADING", ACCENT)
        self.progress = (0, 0)
        self.changed()

        def progress(done, total):
            self.app.post(self.set_progress, done, total)

        self.app.background(lambda: core.install(self.game, version, url, progress), self.installed)

    def set_progress(self, done, total):
        if self.progress is not None:
            self.progress = (done, total)
            self.changed()

    def installed(self, _result, error):
        self.progress = None
        if error:
            self.busy, self.status, self.badge = False, f"Update failed: {error}", ("UPDATE FAILED", ACCENT)
            self.changed()
        else:
            self.describe()

    def play(self):
        try:
            core.launch(self.game)
        except Exception as e:
            self.status = f"Could not start: {e}"
            self.changed()


class App(tk.Tk):
    def __init__(self, games, launcher_entry=None, tabs=()):
        super().__init__()
        self.title("Yellow Makes Games")
        try:
            self.iconbitmap(str(ICON))  # title bar and taskbar
        except tk.TclError:
            pass
        self.resizable(False, False)
        self.configure(bg=INK)
        self.dark_title_bar()
        self.inbox = queue.Queue()  # worker threads must not touch tkinter
        self.images = {}  # made once, reused; Tk forgets images nothing refers to
        self.menu = None
        self.sign_in_box = None
        self.drain()

        c = self.canvas = tk.Canvas(self, width=WIDTH, height=HEIGHT, highlightthickness=0, bg=INK)
        c.pack()
        c.bind("<Button-1>", lambda e: self.close_menu(), add="+")
        self.bind("<Configure>", lambda e: self.close_menu())

        # Artwork, then shading that darkens the left, top and bottom so text reads over any picture.
        self.art = c.create_image(0, 0, anchor="nw")
        self.watermark = c.create_text(WIDTH + 24, HEIGHT + 34, anchor="se", text="", fill="#16204a",
                                       font=(DISPLAY, 150))
        c.create_image(0, 0, anchor="nw", image=self.keep("left", fade(self, 720, HEIGHT, lambda t: 0.93 * (1 - t) ** 1.7, True)))
        c.create_image(0, HEIGHT - 300, anchor="nw",
                       image=self.keep("bottom", fade(self, WIDTH, 300, lambda t: 0.97 * t ** 1.6, False)))
        c.create_image(0, 0, anchor="nw", image=self.keep("top", fade(self, WIDTH, 120, lambda t: 0.8 * (1 - t) ** 2, False)))

        # Top bar: one tab per page, then the launcher's own version, update and account.
        self.pages = {}  # name -> {"row": GameRow or None, "links": [Button], "art": path or None}
        for game in games:
            self.pages[game["name"]] = {"row": None, "links": [], "art": game.get("background"), "game": game}
        for tab in tabs:  # a tab named like a game adds its links to the game's page
            page = self.pages.setdefault(tab["name"], {"row": None, "links": [], "art": tab.get("background")})
            # Under a game's own buttons links are small; on a page of their own they are the big buttons.
            big = "game" not in page
            sites = "  ·  ".join(link["url"].split("://", 1)[-1].rstrip("/") for link in tab["links"])
            page["note"] = f"Plays in your browser  ·  {sites}"
            if all(link.get("window") for link in tab["links"]):
                page["note"] = f"Opens in its own window  ·  {sites}"
            for link in tab["links"]:
                # "window": true opens the site as its own app window; otherwise a browser tab.
                text = f"▶   {link['label']}" if link.get("window") else f"{link['label']}  ↗"
                page["links"].append(Button(self, text, lambda link=link: self.open_link(link),
                                            height=54 if big else 36, font=(FONT, 13 if big else 9, "bold")))
        self.link_buttons = [b for page in self.pages.values() for b in page["links"]]

        self.tabs = {}
        x = 48  # lined up with the title
        for name in self.pages:
            item = c.create_text(x, 30, anchor="w", text=name.upper(), fill=MUTED, font=(FONT, 10, "bold"))
            c.tag_bind(item, "<Enter>", lambda e, n=name: self.hover_tab(n, True))
            c.tag_bind(item, "<Leave>", lambda e, n=name: self.hover_tab(n, False))
            c.tag_bind(item, "<ButtonRelease-1>", lambda e, n=name: self.show(n))
            self.tabs[name] = item
            x = c.bbox(item)[2] + 30
        self.tab_line = c.create_rectangle(0, 0, 0, 0, fill=ACCENT, width=0)

        self.version_text = c.create_text(0, 30, anchor="e", fill=FAINT, font=(FONT, 9),
                                          text=f"Launcher {core.app_version()}")
        self.account_btn = Button(self, "", self.account_clicked, height=32, font=(FONT, 9, "bold"), radius=8)
        self.launcher_btn = None
        self.place_top_right()

        # The game: badge, title, one line of status, the buttons, download progress, links.
        self.badge_dot = c.create_oval(0, 0, 0, 0, width=0)
        self.badge = c.create_text(64, 222, anchor="w", text="", font=(FONT, 9, "bold"))
        self.title_shadow = c.create_text(47, 242, anchor="nw", text="", fill="#02040a", font=(DISPLAY, 42))
        self.heading = c.create_text(46, 240, anchor="nw", text="", fill=TEXT, font=(DISPLAY, 42))
        self.status = c.create_text(48, 318, anchor="nw", text="", fill=MUTED, font=(FONT, 11), width=600)
        self.primary = Button(self, "", None, kind="accent", height=54, width=200, font=(FONT, 13, "bold"))
        self.secondary = Button(self, "", None, height=54, font=(FONT, 11, "bold"))
        self.version_btn = Button(self, "", self.version_menu, height=54, font=(FONT, 10, "bold"))
        self.more_btn = Button(self, "•••", self.more_menu, height=54, width=54, font=(FONT, 11, "bold"))
        self.track = c.create_rectangle(0, 0, 0, 0, fill="#1c2645", width=0, state="hidden")
        self.fill = c.create_rectangle(0, 0, 0, 0, fill=ACCENT, width=0, state="hidden")
        self.progress_text = c.create_text(48, 448, anchor="nw", text="", fill=MUTED, font=(FONT, 9))

        self.current = None
        self.rows = []
        for page in self.pages.values():
            if "game" in page:
                page["row"] = GameRow(self, page["game"])
                self.rows.append(page["row"])
        if self.pages:
            self.show(next(iter(self.pages)))
        self.show_account()

        self.launcher_entry = launcher_entry
        self.launcher_busy = False  # checking for, or downloading, a launcher update
        if launcher_entry:
            core.remove_old_launcher()
            self.check_launcher()

    # Images

    def keep(self, key, image):
        self.images[key] = image
        return image

    def image(self, kind, *args):
        """A cached image: ("rounded", w, h, radius, colour, opacity[, border colour, opacity])."""
        key = (kind, *args)
        if key not in self.images:
            w, h, radius, colour, opacity, *border = args
            self.images[key] = rounded(self, w, h, radius, (colour, opacity), tuple(border) or None)
        return self.images[key]

    def art_for(self, name):
        path = self.pages[name]["art"]
        key = ("art", path or "generated")
        if key not in self.images:
            try:
                self.images[key] = tk.PhotoImage(master=self, file=str(HERE / path)) if path else generated_art(self)
            except tk.TclError:
                self.images[key] = generated_art(self)  # missing picture
        return self.images[key], not path

    def dark_title_bar(self):
        try:  # Windows 11: dark title bar to match the window
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        except (AttributeError, OSError):
            pass

    # Pages

    def show(self, name):
        self.close_menu()
        self.current = name
        art, generated = self.art_for(name)
        self.canvas.itemconfigure(self.art, image=art)
        self.canvas.itemconfigure(self.watermark, text=name.upper() if generated else "")
        for tab, item in self.tabs.items():
            self.canvas.itemconfigure(item, fill=TEXT if tab == name else MUTED)
        x0, _, x1, _ = self.canvas.bbox(self.tabs[name])
        self.canvas.coords(self.tab_line, x0 + 1, 44, x1 - 1, 47)
        self.canvas.itemconfigure(self.heading, text=name)
        self.canvas.itemconfigure(self.title_shadow, text=name)
        for button in self.link_buttons:
            button.hide()
        self.draw()

    def open_link(self, link):
        if link.get("window"):
            try:
                if core.open_game_window(link["url"]):
                    return
            except (core.LauncherError, OSError):
                pass  # no usable Edge: a normal browser tab instead
        webbrowser.open(link["url"])

    def hover_tab(self, name, on):
        self.canvas.config(cursor="hand2" if on else "")
        if name != self.current:
            self.canvas.itemconfigure(self.tabs[name], fill=TEXT if on else MUTED)

    def refresh(self, row):
        """A game's state changed; redraw if it is the one showing."""
        if self.current and self.pages[self.current]["row"] is row:
            self.draw()

    def draw(self):
        c, page = self.canvas, self.pages[self.current]
        row = page["row"]
        text, colour = row.badge if row else ("BROWSER GAME", GOOD)
        c.itemconfigure(self.badge, text=text, fill=colour)
        c.itemconfigure(self.badge_dot, fill=colour)
        c.coords(self.badge_dot, 49, 218, 57, 226)
        c.itemconfigure(self.status, text=row.status if row else page["note"])
        for button in (self.primary, self.secondary, self.version_btn, self.more_btn):
            button.hide()

        x, y, gap = 48, 358, 12
        links = page["links"]
        if not row:  # a page of links only: they are the big buttons
            for i, button in enumerate(links):
                button.config(kind="accent" if i == 0 else "ghost")
                x = button.place(x, y) + gap
            return

        have = core.installed_version(row.game)
        label, possible = row.action()
        if have:
            self.primary.config(text="▶   PLAY", command=row.play, state="disabled" if row.busy else "normal")
            x = self.primary.place(x, y) + gap
            if row.selected() and row.selected()[0] != have:
                self.secondary.config(text=label, command=row.start_install,
                                      state="normal" if possible else "disabled")
                x = self.secondary.place(x, y) + gap
        else:
            self.primary.config(text=label.upper(), command=row.start_install,
                                state="normal" if possible else "disabled")
            x = self.primary.place(x, y) + gap
        if row.versions:
            chosen = row.selected()[0]
            self.version_btn.config(text=f"{'Latest · ' if row.choice == 0 else ''}{chosen}   ▾",
                                    state="disabled" if row.busy else "normal")
            x = self.version_btn.place(x, y) + gap
        self.more_btn.place(x, y)

        if row.progress is not None:
            done, total = row.progress
            share = done / total if total else 0
            c.coords(self.track, 48, 432, 48 + 420, 436)
            c.coords(self.fill, 48, 432, 48 + round(420 * share), 436)
            c.itemconfigure(self.track, state="normal")
            c.itemconfigure(self.fill, state="normal")
            mb = 1024 * 1024
            c.itemconfigure(self.progress_text, text=f"{share:.0%}  ·  {done / mb:.0f} of {total / mb:.0f} MB"
                            if total else f"{done / mb:.0f} MB")
        else:
            c.itemconfigure(self.track, state="hidden")
            c.itemconfigure(self.fill, state="hidden")
            c.itemconfigure(self.progress_text, text="")

        x = 48
        for button in links:
            x = button.place(x, HEIGHT - 72) + 10

    # Menus

    def open_menu(self, button, items):
        self.close_menu()
        self.menu = Menu(self, self.canvas.winfo_rootx() + button.x,
                         self.canvas.winfo_rooty() + button.y + button.height + 6, items)

    def close_menu(self):
        if self.menu is not None:
            if self.menu.winfo_exists():
                self.menu.destroy()
            self.menu = None

    def version_menu(self):
        row = self.pages[self.current]["row"]
        have = core.installed_version(row.game)
        items = []
        for i, (version, _url) in enumerate(row.versions[:15]):
            tags = [t for t, on in (("latest", i == 0), ("installed", version == have)) if on]
            label = f"{'✓  ' if i == row.choice else '     '}{version}" + (f"   ({', '.join(tags)})" if tags else "")
            items.append((label, lambda i=i: row.choose(i)))
        self.open_menu(self.version_btn, items)

    def more_menu(self):
        row = self.pages[self.current]["row"]
        have = core.installed_version(row.game)
        self.open_menu(self.more_btn, [
            ("Open game folder", row.open_folder if have else None),
            ("Check for updates", None if row.busy else (lambda: [r.check() for r in self.rows])),
            ("Check for launcher update", self.check_launcher_now if self.launcher_entry else None),
            ("Remove all installs…", self.remove_all),
        ])

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
                row.status = str(e)
                row.changed()

    # Top right: the launcher's own version and update, and the GitHub account

    def place_top_right(self):
        x = self.account_btn.place(WIDTH - 32 - self.account_btn.width, 14) - self.account_btn.width - 16
        if self.launcher_btn:
            x = self.launcher_btn.place(x - self.launcher_btn.width, 14) - self.launcher_btn.width - 16
        self.canvas.coords(self.version_text, x, 30)

    def show_account(self):
        login = core.github_login()
        self.account_btn.config(state="normal", text=f"●  {login}" if login else "Sign in with GitHub")
        self.place_top_right()

    def account_clicked(self):
        login = core.github_login()
        if login:
            self.open_menu(self.account_btn, [(f"Signed in to GitHub as {login}", None), ("Sign out", self.sign_out)])
            return
        self.account_btn.config(state="disabled", text="Contacting GitHub…")
        self.place_top_right()
        self.background(core.start_sign_in, self.got_sign_in_code)

    def sign_out(self):
        core.sign_out()
        self.show_account()
        for row in self.rows:
            row.check()

    def got_sign_in_code(self, device, error):
        if error:
            self.show_account()
            messagebox.showerror("Sign in with GitHub", str(error), parent=self)
            return
        cancelled = threading.Event()
        self.sign_in_box = SignInBox(self, device, cancelled)
        self.background(lambda: core.finish_sign_in(device, cancelled),
                        lambda login, err: self.signed_in(login, err, cancelled))

    def signed_in(self, _login, error, cancelled):
        if self.sign_in_box and self.sign_in_box.winfo_exists():
            self.sign_in_box.destroy()
        self.sign_in_box = None
        self.show_account()
        if error and not cancelled.is_set():
            messagebox.showerror("Sign in with GitHub", str(error), parent=self)
        elif not error:
            for row in self.rows:
                row.check()

    def check_launcher(self, manual=False):
        """Look for a newer launcher now, and again every LAUNCHER_CHECK_MS while it stays open."""
        if not manual:
            self.after(LAUNCHER_CHECK_MS, self.check_launcher)
        if self.launcher_busy:
            return
        self.launcher_busy = True
        self.background(lambda: core.launcher_update(self.launcher_entry),
                        lambda update, error: self.launcher_checked(update, error, manual))

    def check_launcher_now(self):
        self.check_launcher(manual=True)

    def launcher_checked(self, update, error, manual=False):
        self.launcher_busy = False
        if error or not update:
            if manual:  # asked for: say what happened; otherwise stay quiet
                if error:
                    messagebox.showerror("Launcher update", f"Could not check for a launcher update ({error}).",
                                         parent=self)
                else:
                    messagebox.showinfo("Launcher update",
                                        f"The launcher is up to date ({core.app_version()}).", parent=self)
            return
        version, url = update
        if self.launcher_btn:
            self.launcher_btn.hide()  # an older update was found before; offer the newest
        self.launcher_btn = Button(self, f"Update launcher to {version}", lambda: self.update_launcher(version, url),
                                   kind="accent", height=32, font=(FONT, 9, "bold"), radius=8)
        self.place_top_right()

    def update_launcher(self, version, url):
        self.launcher_busy = True
        self.launcher_btn.config(state="disabled", text=f"Downloading launcher {version}…")
        self.place_top_right()
        self.background(lambda: core.replace_self_and_restart(url), self.launcher_replaced)

    def launcher_replaced(self, _result, error):
        if error:
            self.launcher_busy = False
            self.launcher_btn.config(state="normal", text="Launcher update failed; try again")
            self.place_top_right()
            messagebox.showerror("Update launcher", str(error), parent=self)
        else:
            self.destroy()  # the new launcher is already starting

    # Threads

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
