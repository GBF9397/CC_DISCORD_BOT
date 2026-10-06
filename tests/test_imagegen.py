"""Image generation against a mock ComfyUI and a fake `lms` CLI."""
import asyncio

import pytest_asyncio
from aiohttp import web

from imagegen import DrawError, ImageMaker, blocked
from tests.test_bot import BOT_CHANNEL, BOT_CHANNEL_2, FakeChannel, FakeMessage, make_bot

PNG = b"\x89PNG fake"


class MockComfy:
    def __init__(self, events, delay=0.0):
        self.events, self.delay, self.jobs = events, delay, []

    async def prompt(self, request):
        self.jobs.append((await request.json())["prompt"])
        self.events.append("comfy draw")
        return web.json_response({"prompt_id": "job1"})

    async def history(self, request):
        await asyncio.sleep(self.delay)
        return web.json_response({"job1": {"outputs": {"7": {"images": [
            {"filename": "a.png", "subfolder": "", "type": "temp"}]}}}})

    async def view(self, request):
        assert request.query["filename"] == "a.png" and request.query["type"] == "temp"
        return web.Response(body=PNG)

    async def free(self, request):
        self.events.append("comfy free")
        return web.json_response({})

    async def start(self):
        app = web.Application()
        app.router.add_post("/prompt", self.prompt)
        app.router.add_get("/history/{id}", self.history)
        app.router.add_get("/view", self.view)
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


async def test_draw_takes_turns_on_the_gpu(mock_api, comfy, events):
    mock_api.reply = "a cat, watercolor"
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "model.safetensors"})
    assert await maker.draw("画一只猫", BOT_CHANNEL) == PNG
    assert events == ["lms unload gemma4-12b-bionic-v2", "comfy draw", "comfy free",
                      "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    job = comfy.jobs[0]
    assert job["2"]["inputs"]["text"] == "a cat, watercolor"
    assert job["1"]["inputs"]["ckpt_name"] == "model.safetensors"
    assert job["7"]["class_type"] == "PreviewImage"
    assert "画一只猫" in mock_api.requests[0]["messages"][0]["content"]
    assert not maker.drawing


async def test_gemma_comes_back_when_comfyui_is_offline(mock_api, events):
    maker = ImageMaker(make_bot(mock_api.base_url).brain, "http://127.0.0.1:9", {"anime": "m"})
    try:
        await maker.draw("a cat", BOT_CHANNEL)
        raise AssertionError("expected DrawError")
    except DrawError as e:
        assert "offline" in str(e)
    assert events == ["lms unload gemma4-12b-bionic-v2",
                      "lms load gemma4-12b-bionic-v2 --context-length 16384"]
    assert not maker.drawing


async def test_refused_request_never_unloads_gemma(mock_api, comfy, events):
    mock_api.reply = "REFUSED"
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "m"})
    try:
        await maker.draw("something bad", BOT_CHANNEL)
        raise AssertionError("expected DrawError")
    except DrawError:
        pass
    assert events == [] and comfy.jobs == []


def test_blocked_needs_both_sexual_and_minor_words():
    assert blocked("nude, schoolgirl, bedroom")
    assert blocked("explicit, 15 years old")
    assert not blocked("children playing in a park, sunny")
    assert not blocked("nude marble statue, museum")


async def test_bot_is_silent_while_drawing(mock_api, comfy, events):
    comfy.delay = 0.5
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    request = FakeMessage("!draw a cat", ch)
    drawing = asyncio.create_task(bot.on_message(request))
    await asyncio.sleep(0.2)
    insist = FakeMessage("answer me NOW", ch, author_id=2)
    await bot.on_message(insist)
    await bot.on_message(FakeMessage("!draw a dog", ch, author_id=3))
    await drawing
    assert insist.replies == []
    assert len(comfy.jobs) == 1
    assert request.replies[0].startswith("🎨")
    assert request.replies[1].filename == "image.png"
    assert request.replies[1].fp.read() == PNG
    after = FakeMessage("hi", ch)
    await bot.on_message(after)
    assert after.replies == ["echo: user1: hi"]


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
    assert early.replies == ["Nothing to refine yet in this channel. Draw one first with /draw or !draw."]
    assert events == []
    await bot.on_message(FakeMessage("!draw a cat", ch))
    mock_api.reply = "a cat, watercolor, night sky"
    msg = FakeMessage("!refine make it night", ch)
    await bot.on_message(msg)
    assert msg.replies[1].filename == "image.png"
    first, second = comfy.jobs
    assert second["5"]["inputs"]["seed"] == first["5"]["inputs"]["seed"]
    assert second["2"]["inputs"]["text"] == "a cat, watercolor, night sky"
    asked = mock_api.requests[-1]["messages"][0]["content"]
    assert "a cat, watercolor" in asked and "make it night" in asked
    other = FakeMessage("!refine make it night", FakeChannel(BOT_CHANNEL_2))
    await bot.on_message(other)
    assert other.replies[0].startswith("Nothing to refine")


async def test_style_switch_picks_the_checkpoint_per_channel(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    switch = FakeMessage("!style realistic", ch)
    await bot.on_message(switch)
    assert switch.replies == ["Drawing style switched to realistic."]
    bad = FakeMessage("!style watercolor", ch)
    await bot.on_message(bad)
    assert bad.replies[0].startswith("Unknown style")
    await bot.on_message(FakeMessage("!draw a cat", ch))
    await bot.on_message(FakeMessage("!draw a cat", FakeChannel(BOT_CHANNEL_2)))
    await bot.on_message(FakeMessage("!style anime", ch))
    await bot.on_message(FakeMessage("!refine add snow", ch))
    names = [job["1"]["inputs"]["ckpt_name"] for job in comfy.jobs]
    assert names == ["photo.safetensors", "model.safetensors", "photo.safetensors"]  # refine keeps its model
