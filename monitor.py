"""PC load readings (graphics card, CPU, RAM) for the monitor window and /status."""
import logging
import os
import subprocess
import sys

import psutil

log = logging.getLogger("bot")

GB = 1024 ** 3
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flash


def gpu():
    """(load %, VRAM used GB, VRAM total GB) of the first NVIDIA card, or None without one."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
        load, used, total = (float(x) for x in out.splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return load, used / 1024, total / 1024  # nvidia-smi reports MiB


def read(cpu_interval=None):
    """Current readings. cpu_interval=None measures since the previous call."""
    ram = psutil.virtual_memory()
    return {"gpu": gpu(), "cpu": psutil.cpu_percent(interval=cpu_interval),
            "ram": (ram.used / GB, ram.total / GB)}


def rows(stats):
    """(name, % used, used/total text) per reading."""
    out = []
    if stats["gpu"]:
        load, used, total = stats["gpu"]
        out.append(("GPU", load, ""))
        out.append(("VRAM", 100 * used / total, f"{used:.1f} / {total:.1f} GB  (free {total - used:.1f} GB)"))
    out.append(("CPU", stats["cpu"], ""))
    used, total = stats["ram"]
    out.append(("RAM", 100 * used / total, f"{used:.1f} / {total:.1f} GB  (free {total - used:.1f} GB)"))
    return out


def lines(stats):
    """The readings as short lines with a text bar, for Discord."""
    out = [] if stats["gpu"] else ["GPU  n/a"]
    for name, percent, detail in rows(stats):
        filled = round(percent / 10)
        out.append(f"{name:<4} {'▓' * filled}{'░' * (10 - filled)} {percent:3.0f}%  {detail}".rstrip())
    return out


def color(percent):
    return "#3cb371" if percent < 60 else "#f0a030" if percent < 85 else "#e04040"


def _window(bot_pid):
    import tkinter as tk  # here, so the bot and tests run without Tk

    BAR_W, BAR_H = 220, 16
    root = tk.Tk()
    root.title("Gemma bot monitor")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    font = ("Consolas", 11)
    widgets = []  # (name label, bar canvas, text label) per row, made on first tick
    read()  # first CPU reading primes the counter

    def tick():
        if not psutil.pid_exists(bot_pid):  # the bot stopped or crashed
            root.destroy()
            return
        current = rows(read())
        while len(widgets) < len(current):
            r = len(widgets)
            name = tk.Label(root, font=font, anchor="w", width=5)
            bar = tk.Canvas(root, width=BAR_W, height=BAR_H, bg="#d0d0d0", highlightthickness=0)
            text = tk.Label(root, font=font, anchor="w")
            name.grid(row=r, column=0, padx=(10, 4), pady=3)
            bar.grid(row=r, column=1, pady=3)
            text.grid(row=r, column=2, padx=(6, 10), pady=3, sticky="w")
            widgets.append((name, bar, text))
        for (name, percent, detail), (name_w, bar, text) in zip(current, widgets):
            name_w.config(text=name)
            bar.delete("all")
            bar.create_rectangle(0, 0, BAR_W * min(percent, 100) / 100, BAR_H, fill=color(percent), width=0)
            text.config(text=f"{percent:3.0f}%  {detail}".rstrip())
        root.after(1000, tick)

    tick()
    root.mainloop()


def open_window():
    """Show the monitor window in its own python.exe, so a hidden bot start doesn't hide it.
    Stop-Process -Name python ends it with the bot; otherwise it closes once the bot is gone."""
    try:
        subprocess.Popen([sys.executable, __file__, str(os.getpid())], creationflags=NO_WINDOW)
    except OSError as e:
        log.warning("Monitor window failed to open: %s", e)


if __name__ == "__main__":
    _window(int(sys.argv[1]))
