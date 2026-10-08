"""Monitor readings and the /status command."""
import subprocess
from types import SimpleNamespace

import monitor
from tests.test_bot import make_bot


def test_gpu_parses_nvidia_smi(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="42, 8192, 16384\n"))
    assert monitor.gpu() == (42.0, 8.0, 16.0)


def test_gpu_missing_is_none(monkeypatch):
    def run(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", run)
    assert monitor.gpu() is None


def test_lines_show_use_and_free():
    out = monitor.lines({"gpu": (42.0, 9.5, 16.0), "cpu": 23.4, "ram": (14.0, 32.0)})
    assert out == ["GPU  ▓▓▓▓░░░░░░  42%", "VRAM ▓▓▓▓▓▓░░░░  59%  9.5 / 16.0 GB  (free 6.5 GB)",
                   "CPU  ▓▓░░░░░░░░  23%", "RAM  ▓▓▓▓░░░░░░  44%  14.0 / 32.0 GB  (free 18.0 GB)"]
    assert monitor.lines({"gpu": None, "cpu": 5, "ram": (1, 2)})[0] == "GPU  n/a"


def test_window_on_unless_turned_off(monkeypatch):
    from core import load_config
    monkeypatch.delenv("MONITOR_WINDOW", raising=False)
    assert load_config()["monitor_window"]
    monkeypatch.setenv("MONITOR_WINDOW", "off")
    assert not load_config()["monitor_window"]


async def test_status_works_while_drawing(mock_api, monkeypatch):
    monkeypatch.setattr(monitor, "read", lambda interval=None: {"gpu": (99, 15, 16), "cpu": 50, "ram": (20, 32)})
    bot = make_bot(mock_api.base_url)
    bot.drawing = lambda: True
    sent = []

    async def send_message(text, ephemeral=False):
        sent.append((text, ephemeral))
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=send_message))
    await bot.tree.get_command("status").callback(interaction)
    assert sent and "GPU  ▓▓▓▓▓▓▓▓▓▓  99%" in sent[0][0] and "free 1.0 GB" in sent[0][0] and sent[0][1]
