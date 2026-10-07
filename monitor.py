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


def lines(stats):
    """The readings as short lines: use and what's free."""
    out = []
    if stats["gpu"]:
        load, used, total = stats["gpu"]
        out.append(f"GPU  {load:.0f}%")
        out.append(f"VRAM {used:.1f} / {total:.1f} GB  (free {total - used:.1f} GB)")
    else:
        out.append("GPU  n/a")
    out.append(f"CPU  {stats['cpu']:.0f}%")
    used, total = stats["ram"]
    out.append(f"RAM  {used:.1f} / {total:.1f} GB  (free {total - used:.1f} GB)")
    return out


def _window(bot_pid):
    import tkinter as tk  # here, so the bot and tests run without Tk

    root = tk.Tk()
    root.title("Gemma bot monitor")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    text = tk.Label(root, font=("Consolas", 11), justify="left", padx=12, pady=8)
    text.pack()
    read()  # first CPU reading primes the counter

    def tick():
        if not psutil.pid_exists(bot_pid):  # the bot stopped or crashed
            root.destroy()
            return
        text.config(text="\n".join(lines(read())))
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
