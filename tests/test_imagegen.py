"""Image generation against a mock ComfyUI and a fake `lms` CLI."""
import asyncio

import pytest_asyncio
from aiohttp import web

import imagegen
from bot import ALREADY_QUEUED, DRAWING_NOTICE, QUEUED_NOTICE
from imagegen import NOTHING_TO_REFINE, ImageMaker, blocked
from tests.test_bot import BOT_CHANNEL, BOT_CHANNEL_2, FakeChannel, FakeMessage, make_bot

PNG = b"\x89PNG fake"


class MockComfy:
    """Speaks ComfyUI's /prompt + websocket protocol, sending a preview then the PNG."""

    def __init__(self, events, delay=0.0):
        self.events, self.delay, self.jobs, self.sockets = events, delay, [], {}
        self.fail = False

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets[request.query["clientId"]] = ws
        async for _ in ws:
            pass
        return ws

    async def prompt(self, request):
        body = await request.json()
        self.jobs.append(body["prompt"])
        self.events.append("comfy draw")
        asyncio.create_task(self.run(self.sockets[body["client_id"]], f"job{len(self.jobs)}"))
        return web.json_response({"prompt_id": f"job{len(self.jobs)}"})

    async def run(self, ws, job):
        await asyncio.sleep(0.01)
        await ws.send_json({"type": "executing", "data": {"node": "5", "prompt_id": job}})
        await ws.send_bytes(b"\0\0\0\1\0\0\0\1" + b"PREVIEW")
        await asyncio.sleep(self.delay)
        if self.fail:
            await ws.send_json({"type": "execution_error",
                                "data": {"prompt_id": job, "exception_message": "out of memory"}})
            return
        await ws.send_json({"type": "executing", "data": {"node": "7", "prompt_id": job}})
        await ws.send_bytes(b"\0\0\0\1\0\0\0\2" + PNG)
        await ws.send_json({"type": "executing", "data": {"node": None, "prompt_id": job}})

    async def stats(self, request):
        return web.json_response({})

    async def free(self, request):
        self.events.append("comfy free")
        return web.json_response({})

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/system_stats", self.stats)
        app.router.add_post("/prompt", self.prompt)
        app.router.add_post("/free", self.free)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@pytest_asyncio.fixture
async def events(monkeypatch):
    log = []

    async def fake_lms(self, *args):
        log.append("lms " + " ".join(args))
        return True

    monkeypatch.setattr(ImageMaker, "_lms", fake_lms)
    return log


@pytest_asyncio.fixture
async def comfy(events):
    server = MockComfy(events)
    server.url = await server.start()
    yield server
    await server.runner.cleanup()


def image_bot(api_url, comfy_url):
    bot = make_bot(api_url)
    bot.config.update(image_gen=True)
    bot.images = ImageMaker(bot.brain, comfy_url, {"anime": "model.safetensors", "realistic": "photo.safetensors"},
                            1024, 16384)
    return bot


async def draw(maker, request, user=1, refine=False):
    """Queue one picture, wait for the queue to empty, return (png, error)."""
    got = []

    async def deliver(png, error):
        got.append((png, error))

    maker.submit(request, BOT_CHANNEL, user, deliver, refine)
    await maker._worker
    return got[0]


async def finish(bot):
    await bot.images._worker


async def test_draw_takes_turns_on_the_gpu(mock_api, comfy, events):
    mock_api.reply = "a cat, watercolor"
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "model.safetensors"})
    assert await draw(maker, "画一只猫") == (PNG, None)
    assert events == ["lms unload gemma4-12b-bionic-v2", "comfy draw", "comfy free",
                      "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    job = comfy.jobs[0]
    assert job["2"]["inputs"]["text"] == "a cat, watercolor"
    assert job["1"]["inputs"]["ckpt_name"] == "model.safetensors"
    assert job["7"]["class_type"] == "SaveImageWebsocket"
    assert "画一只猫" in mock_api.requests[0]["messages"][0]["content"]
    assert not maker.drawing


async def test_gemma_comes_back_when_comfyui_is_offline(mock_api, events):
    maker = ImageMaker(make_bot(mock_api.base_url).brain, "http://127.0.0.1:9", {"anime": "m"})
    png, error = await draw(maker, "a cat")
    assert png is None and "offline" in error
    assert events == ["lms unload gemma4-12b-bionic-v2",
                      "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    assert not maker.drawing


async def test_refused_request_never_unloads_gemma(mock_api, comfy, events):
    mock_api.reply = "REFUSED"
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "m"})
    assert await draw(maker, "something bad") == (None, "Sorry, I won't draw that.")
    assert events == [] and comfy.jobs == []


async def test_comfyui_error_is_reported_and_gemma_comes_back(mock_api, comfy, events):
    comfy.fail = True
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "m"})
    assert await draw(maker, "a cat") == (None, "Sorry, the drawing failed.")
    assert events[-2:] == ["comfy free", "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    assert not maker.drawing


def test_blocked_needs_both_sexual_and_minor_words():
    assert blocked("nude, schoolgirl, bedroom")
    assert blocked("explicit, 15 years old")
    assert not blocked("children playing in a park, sunny")
    assert not blocked("nude marble statue, museum")


async def test_queue_one_per_member_and_silent_to_chat(mock_api, comfy, events):
    comfy.delay = 0.3
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    first = FakeMessage("!draw a cat", ch, author_id=1)
    await bot.on_message(first)
    assert first.replies == [DRAWING_NOTICE]
    second = FakeMessage("!draw a dog", ch, author_id=2)
    await bot.on_message(second)
    assert second.replies == [QUEUED_NOTICE.format(ahead=1)]
    again = FakeMessage("!draw a fox", ch, author_id=2)
    await bot.on_message(again)
    assert again.replies == [ALREADY_QUEUED]
    chat = FakeMessage("answer me NOW", ch, author_id=3)
    await bot.on_message(chat)
    await finish(bot)
    assert chat.replies == []
    assert len(comfy.jobs) == 2
    assert first.replies[1].filename == "image.png" and first.replies[1].fp.read() == PNG
    assert second.replies[1].filename == "image.png"
    # One trip off the GPU for both pictures.
    assert events == ["lms unload gemma4-12b-bionic-v2", "comfy draw", "comfy draw", "comfy free",
                      "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    after = FakeMessage("hi", ch)
    await bot.on_message(after)
    assert after.replies == ["echo: user1: hi"]


async def test_member_can_queue_again_once_their_picture_is_done(mock_api, comfy, events):
    comfy.delay = 0.2
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    await bot.on_message(FakeMessage("!draw a cat", ch, author_id=1))
    await bot.on_message(FakeMessage("!draw a dog", ch, author_id=2))
    while bot.images.waiting != {2}:  # member 1's picture is posted, member 2's still drawing
        await asyncio.sleep(0.01)
    late = FakeMessage("!draw a bird", ch, author_id=1)
    await bot.on_message(late)
    assert late.replies == [QUEUED_NOTICE.format(ahead=1)]
    await finish(bot)
    assert len(comfy.jobs) == 3
    assert late.replies[1].filename == "image.png"


async def test_draw_off_by_default(mock_api):
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("!draw a cat", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: !draw a cat"]
    assert bot.tree.get_command("draw") is None


async def test_refine_changes_the_last_prompt_and_keeps_the_seed(mock_api, comfy, events):
    mock_api.reply = "a cat, watercolor"
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    early = FakeMessage("!refine make it night", ch)
    await bot.on_message(early)
    assert early.replies == [NOTHING_TO_REFINE]
    assert events == []
    await bot.on_message(FakeMessage("!draw a cat", ch))
    await finish(bot)
    mock_api.reply = "a cat, watercolor, night sky"
    msg = FakeMessage("!refine make it night", ch)
    await bot.on_message(msg)
    await finish(bot)
    assert msg.replies[1].filename == "image.png"
    first, second = comfy.jobs
    assert second["5"]["inputs"]["seed"] == first["5"]["inputs"]["seed"]
    assert second["2"]["inputs"]["text"] == "a cat, watercolor, night sky"
    asked = mock_api.requests[-1]["messages"][0]["content"]
    assert "a cat, watercolor" in asked and "make it night" in asked
    other = FakeMessage("!refine make it night", FakeChannel(BOT_CHANNEL_2))
    await bot.on_message(other)
    assert other.replies == [NOTHING_TO_REFINE]


async def test_style_switch_picks_the_checkpoint_per_channel(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    switch = FakeMessage("!style realistic", ch)
    await bot.on_message(switch)
    assert switch.replies == ["Drawing style switched to realistic."]
    bad = FakeMessage("!style watercolor", ch)
    await bot.on_message(bad)
    assert bad.replies[0].startswith("Unknown style")
    for message in (FakeMessage("!draw a cat", ch), FakeMessage("!draw a cat", FakeChannel(BOT_CHANNEL_2)),
                    FakeMessage("!style anime", ch), FakeMessage("!refine add snow", ch)):
        await bot.on_message(message)
        if bot.drawing():
            await finish(bot)
    names = [job["1"]["inputs"]["ckpt_name"] for job in comfy.jobs]
    assert names == ["photo.safetensors", "model.safetensors", "photo.safetensors"]  # refine keeps its model


async def test_starts_comfyui_in_the_background_when_it_is_off(mock_api, comfy, events, monkeypatch):
    started = []
    monkeypatch.setattr(imagegen.subprocess, "Popen", lambda args, **kw: started.append((args, kw["cwd"])))
    up = iter([False, False, True])
    real = ImageMaker._comfy_running

    async def running(self):
        return next(up, True) and await real(self)

    monkeypatch.setattr(ImageMaker, "_comfy_running", running)
    monkeypatch.setattr(imagegen.asyncio, "sleep", fast_sleep)
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "m"}, comfy_dir="C:/Comfy")
    assert await draw(maker, "a cat") == (PNG, None)
    assert len(started) == 1
    args, cwd = started[0]
    assert cwd == "C:/Comfy" and args[-1] == "--disable-auto-launch" and "main.py" in args[2]
    await draw(maker, "a dog")
    assert len(started) == 1  # already running: not started again


REAL_SLEEP = asyncio.sleep


async def fast_sleep(seconds):
    await REAL_SLEEP(0)


async def test_each_request_keeps_its_own_style_in_order(mock_api, comfy, events):
    comfy.delay = 0.1
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    requests = ["!draw 小猫", "!draw realistic 中世纪城堡", "!draw anime 花", "!draw 热血主角", "!draw realistic 跑车"]
    for user, text in enumerate(requests, start=1):
        await bot.on_message(FakeMessage(text, ch, author_id=user))
    await bot.on_message(FakeMessage("!style realistic", ch, author_id=9))  # ignored while busy
    await finish(bot)
    names = [job["1"]["inputs"]["ckpt_name"] for job in comfy.jobs]
    assert names == ["model.safetensors", "photo.safetensors", "model.safetensors",
                     "model.safetensors", "photo.safetensors"]
    asked = [r["messages"][0]["content"] for r in mock_api.requests]
    assert ["小猫" in a for a in asked] == [True, False, False, False, False]
    assert "realistic" not in asked[1] and "中世纪城堡" in asked[1]


async def test_gemma_is_told_how_members_can_draw(mock_api, comfy):
    bot = image_bot(mock_api.base_url, comfy.url)
    await bot.on_message(FakeMessage("你可以生成图吗", FakeChannel(BOT_CHANNEL)))
    assert "!draw" in mock_api.requests[-1]["messages"][0]["content"]
    plain = make_bot(mock_api.base_url)
    await plain.on_message(FakeMessage("你可以生成图吗", FakeChannel(BOT_CHANNEL)))
    assert "!draw" not in mock_api.requests[-1]["messages"][0]["content"]
