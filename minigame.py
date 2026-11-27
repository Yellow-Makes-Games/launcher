"""Star Catcher: the launcher's hidden minigame (click the launcher version, top right, five times).

Steer the yellow paddle with the arrow keys or A/D (or the mouse). Catch the
stars, dodge the blocks. Three blocks and the round is over. It speeds up as
the score climbs. Space starts again, Esc closes. The best score is kept in
the launcher's folder.
"""
import math
import random
import tkinter as tk

import core

W, H = 480, 400
INK, TEXT, MUTED, ACCENT, BAD = "#070b18", "#f3f5fb", "#a3aecb", "#ffd21f", "#6c7cff"
FONT = "Segoe UI"
TICK = 16  # ms per frame


def best_file():
    return core.home() / "minigame-best.txt"


def load_best():
    try:
        return int(best_file().read_text())
    except (OSError, ValueError):
        return 0


def save_best(score):
    try:
        best_file().parent.mkdir(parents=True, exist_ok=True)
        best_file().write_text(str(score))
    except OSError:
        pass  # only a high score


class StarCatcher(tk.Toplevel):
    def __init__(self, master, seed=None):
        super().__init__(master)
        self.title("Star Catcher")
        self.resizable(False, False)
        self.configure(bg=INK)
        self.rng = random.Random(seed)
        self.c = tk.Canvas(self, width=W, height=H, bg=INK, highlightthickness=0)
        self.c.pack()
        self.held = set()
        for key in ("<KeyPress>", "<KeyRelease>"):
            self.bind(key, self.key)
        self.c.bind("<Motion>", lambda e: setattr(self, "mouse_x", e.x))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<space>", lambda e: self.restart() if self.over else None)
        self.best = load_best()
        self.after_id = None
        self.restart()
        self.focus_force()

    def restart(self):
        self.c.delete("all")
        # A few still stars behind everything.
        for _ in range(40):
            x, y = self.rng.uniform(0, W), self.rng.uniform(0, H)
            self.c.create_oval(x, y, x + 2, y + 2, fill="#26305a", width=0)
        self.x, self.mouse_x = W / 2, None
        self.score, self.lives, self.over = 0, 3, False
        self.falling = []  # [kind, canvas item, x, y, speed]; kind "star" or "block"
        self.spawn_in = 0
        self.paddle = self.c.create_rectangle(0, 0, 0, 0, fill=ACCENT, width=0)
        self.hud = self.c.create_text(12, 10, anchor="nw", fill=TEXT, font=(FONT, 11, "bold"))
        self.message = self.c.create_text(W / 2, H / 2, fill=TEXT, font=(FONT, 16, "bold"), justify="center",
                                          text="")
        self.step()

    def key(self, e):
        k = e.keysym.lower()
        if e.type == tk.EventType.KeyPress:
            self.held.add(k)
            self.mouse_x = None  # keys take over from the mouse
        else:
            self.held.discard(k)

    def step(self):
        if not self.winfo_exists():
            return
        speed = 1 + self.score / 40  # everything speeds up as the score climbs
        move = (("right" in self.held or "d" in self.held) - ("left" in self.held or "a" in self.held)) * 7
        if self.mouse_x is not None:
            move = max(-9, min(9, self.mouse_x - self.x))
        self.x = max(36, min(W - 36, self.x + move))
        self.c.coords(self.paddle, self.x - 36, H - 26, self.x + 36, H - 16)

        self.spawn_in -= 1
        if self.spawn_in <= 0:
            self.spawn(speed)
            self.spawn_in = max(12, int(38 / speed))

        for f in list(self.falling):
            kind, item, x, y, v = f
            y += v
            f[3] = y
            self.c.move(item, 0, v)
            if H - 30 <= y <= H - 12 and abs(x - self.x) < 44:  # touched the paddle
                self.c.delete(item)
                self.falling.remove(f)
                if kind == "star":
                    self.score += 1
                else:
                    self.lives -= 1
                    self.c.itemconfigure(self.paddle, fill=BAD)
                    self.after(120, lambda: self.winfo_exists() and self.c.itemconfigure(self.paddle, fill=ACCENT))
            elif y > H + 20:
                self.c.delete(item)
                self.falling.remove(f)

        self.c.itemconfigure(self.hud, text=f"★ {self.score}     {'♥ ' * self.lives}    best {self.best}")
        if self.lives <= 0:
            return self.game_over()
        self.after_id = self.after(TICK, self.step)

    def spawn(self, speed):
        x = self.rng.uniform(20, W - 20)
        if self.rng.random() < 0.6:
            item = self.c.create_polygon(star(x, 0, 11), fill=ACCENT, width=0)
            self.falling.append(["star", item, x, 0, self.rng.uniform(2.2, 3.2) * speed])
        else:
            item = self.c.create_rectangle(x - 12, -12, x + 12, 12, fill=BAD, width=0)
            self.falling.append(["block", item, x, 0, self.rng.uniform(2.6, 3.8) * speed])

    def game_over(self):
        self.over = True
        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            save_best(self.score)
        self.c.itemconfigure(self.message, text=f"{'New best!' if new_best else 'Game over'}\n★ {self.score}\n\n"
                                                "Space to play again  ·  Esc to close")
        self.c.tag_raise(self.message)


def star(cx, cy, r):
    """The points of a five-pointed star centred on cx, cy."""
    points = []
    for i in range(10):
        a = math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        points += [cx + rr * math.cos(a), cy - rr * math.sin(a)]
    return points
