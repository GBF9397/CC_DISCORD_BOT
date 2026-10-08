"""Image generation against a mock ComfyUI and a fake `lms` CLI."""
import asyncio
from collections import deque

import pytest_asyncio
from aiohttp import web

import imagegen
from bot import ALREADY_QUEUED, DRAWING_NOTICE, NO_PICTURE, NOTHING_TO_RECALL, QUEUED_NOTICE, RECALLED
from imagegen import NOTHING_TO_REFINE, ImageMaker, Job, blocked
from tests.test_bot import BOT_CHANNEL, BOT_CHANNEL_2, CHANNEL, FakeAttachment, FakeChannel, FakeMessage, make_bot

PNG = b"\x89PNG fake"


class MockComfy:
    """Speaks ComfyUI's /prompt + websocket protocol, sending a preview then the PNG."""

    def __init__(self, events, delay=0.0):
        self.events, self.delay, self.jobs, self.sockets = events, delay, [], {}
        self.fail = False
        self.has_edit_node = True

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
        for step in range(1, 5):
            await ws.send_json({"type": "progress", "data": {"value": step, "max": 4, "prompt_id": job}})
        await ws.send_json({"type": "executing", "data": {"node": "7", "prompt_id": job}})
        await ws.send_bytes(b"\0\0\0\1\0\0\0\2" + PNG)
        await ws.send_json({"type": "executing", "data": {"node": None, "prompt_id": job}})

    async def stats(self, request):
        return web.json_response({})

    async def object_info(self, request):
        node = request.match_info["node"]
        return web.json_response({node: {}} if self.has_edit_node else {})

    async def free(self, request):
        self.events.append("comfy free")
        return web.json_response({})

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/system_stats", self.stats)
        app.router.add_post("/prompt", self.prompt)
        app.router.add_post("/free", self.free)
        app.router.add_get("/object_info/{node}", self.object_info)
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
    assert first.replies[0].split("\n")[0] == DRAWING_NOTICE  # then the time estimate
    second = FakeMessage("!draw a dog", ch, author_id=2)
    await bot.on_message(second)
    assert second.replies[0].split("\n")[0] == QUEUED_NOTICE.format(ahead=1)
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
    assert late.replies[0].split("\n")[0] == QUEUED_NOTICE.format(ahead=1)
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
    assert "a cat, watercolor" in asked[0]["text"] and "make it night" in asked[0]["text"]
    assert asked[1]["image_url"]["url"].startswith("data:image/png;base64,")  # Gemma sees the last picture
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
    assert names == ["photo.safetensors", "model.safetensors", "model.safetensors"]  # the channel switched since


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
    request = asked[1].split("[Request]")[1]
    assert "realistic" not in request and "中世纪城堡" in request
    assert "photo model" in asked[1] and "photo model" not in asked[0]  # Gemma knows which model draws it


async def test_gemma_is_told_how_members_can_draw(mock_api, comfy):
    bot = image_bot(mock_api.base_url, comfy.url)
    await bot.on_message(FakeMessage("你可以生成图吗", FakeChannel(BOT_CHANNEL)))
    assert "!draw" in mock_api.requests[-1]["messages"][0]["content"]
    plain = make_bot(mock_api.base_url)
    await plain.on_message(FakeMessage("你可以生成图吗", FakeChannel(BOT_CHANNEL)))
    assert "!draw" not in mock_api.requests[-1]["messages"][0]["content"]


async def test_gemma_looks_up_a_named_character_before_writing_the_prompt(mock_api, comfy, events, monkeypatch):
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "m"})
    replies = iter(["SEARCH: Frieren Sousou no Frieren", "1girl, elf, white hair, twin tails, white robe"])
    asked, searched = [], []

    async def fake_prompt(instruction, images=()):
        asked.append((instruction, images))
        return next(replies)

    async def fake_search(query):
        searched.append(query)
        return "Frieren is an elf with long white hair in twin tails."

    monkeypatch.setattr(maker.brain, "image_prompt", fake_prompt)
    async def fake_images(query, max_results=3):
        searched.append(query)
        return [(b"JPEG", "image/jpeg")]

    monkeypatch.setattr(imagegen, "web_search", fake_search)
    monkeypatch.setattr(imagegen, "image_search", fake_images)
    assert await draw(maker, "画芙莉莲") == (PNG, None)
    assert searched == ["Frieren Sousou no Frieren character appearance hair outfit",
                        "Frieren Sousou no Frieren official art"]
    assert "white hair in twin tails" in asked[1][0]
    assert asked[1][1] == [(b"JPEG", "image/jpeg")]  # Gemma looks at the pictures found
    assert comfy.jobs[0]["2"]["inputs"]["text"] == "1girl, elf, white hair, twin tails, white robe"


async def test_drawing_notice_shows_the_progress_in_percent(mock_api, comfy, events):
    mock_api.reply = "a cat"
    bot = image_bot(mock_api.base_url, comfy.url)
    msg = FakeMessage("!draw a cat", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    notice = msg.sent
    await finish(bot)
    assert msg.replies[0].split("\n")[0] == DRAWING_NOTICE and msg.replies[1].filename == "image.png"
    assert msg.mentioned  # the member is pinged when the picture is posted
    assert notice.edits == [f"{DRAWING_NOTICE}\n▓▓░░░░░░░░ 25%", f"{DRAWING_NOTICE}\n▓▓▓▓▓░░░░░ 50%",
                            f"{DRAWING_NOTICE}\n▓▓▓▓▓▓▓░░░ 75%", f"{DRAWING_NOTICE}\n▓▓▓▓▓▓▓▓▓▓ 100%"]


async def test_refine_keeps_its_model_unless_asked_or_the_channel_switched(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    for text in ("!draw realistic a car", "!refine add rain", "!refine anime add snow"):
        await bot.on_message(FakeMessage(text, ch))
        await finish(bot)
    names = [job["1"]["inputs"]["ckpt_name"] for job in comfy.jobs]
    assert names == ["photo.safetensors", "photo.safetensors", "model.safetensors"]


async def test_refine_builds_on_each_members_own_picture(mock_api, comfy, events):
    """Two members drawing in one channel: a refine never builds on the other member's picture."""
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    mock_api.reply = "a cat"
    await bot.on_message(FakeMessage("!draw a cat", ch, author_id=1))
    await finish(bot)
    mock_api.reply = "a dog"
    await bot.on_message(FakeMessage("!draw a dog", ch, author_id=2))
    await finish(bot)
    mock_api.reply = "a dog, night"
    await bot.on_message(FakeMessage("!refine night", ch, author_id=2))
    await finish(bot)
    mock_api.reply = "a cat, hat"
    await bot.on_message(FakeMessage("!refine add a hat", ch, author_id=1))
    await finish(bot)
    asked = mock_api.requests[-1]["messages"][0]["content"][0]["text"]
    assert "[Tags]\na cat" in asked and "dog" not in asked
    cat, dog, _, cat_hat = comfy.jobs
    assert cat_hat["5"]["inputs"]["seed"] == cat["5"]["inputs"]["seed"]
    early = FakeMessage("!refine night", ch, author_id=3)
    await bot.on_message(early)
    assert early.replies == [NOTHING_TO_REFINE]


async def test_recall_goes_back_and_branches_without_losing_pictures(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    none = FakeMessage("!recall", ch)
    await bot.on_message(none)
    assert none.replies == [NOTHING_TO_RECALL]
    for prompt, text in (("girl, short hair", "!draw a girl"), ("girl, medium hair", "!refine longer hair"),
                         ("girl, very long hair", "!refine longer hair")):
        mock_api.reply = prompt
        await bot.on_message(FakeMessage(text, ch))
        await finish(bot)
    listed = FakeMessage("!recall", ch)
    await bot.on_message(listed)
    assert [f.filename for f in listed.files] == ["1.png", "2.png", "3.png"] and "3" in listed.replies[0]
    back = FakeMessage("!recall 2", ch)
    await bot.on_message(back)
    assert back.replies == [RECALLED.format(number=2)] and back.files[0].fp.read() == PNG
    mock_api.reply = "girl, medium hair, red ribbon"
    await bot.on_message(FakeMessage("!refine add a ribbon", ch))
    await finish(bot)
    assert "[Tags]\ngirl, medium hair\n" in mock_api.requests[-1]["messages"][0]["content"][0]["text"]
    pictures, base = bot.images.history(BOT_CHANNEL, 1)
    assert len(pictures) == 4 and base == 3  # the very long hair one is still there
    for _ in range(3):
        await bot.on_message(FakeMessage("!draw more", ch))
        await finish(bot)
    assert len(bot.images.history(BOT_CHANNEL, 1)[0]) == 5  # only the last 5 are kept
    bad = FakeMessage("!recall 9", ch)
    await bot.on_message(bad)
    assert bad.replies[0].startswith("Pick 1 to 5")


async def test_refine_only_touches_what_the_member_asked_for(mock_api, comfy, events):
    """Gemma lists the edits and the code applies them: every other tag stays, round after round."""
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    mock_api.reply = "1girl, short hair, bob cut, green hair, smile, black sweater AVOID: lowres"
    await bot.on_message(FakeMessage("!draw a girl", ch))
    await finish(bot)
    mock_api.reply = "ADD: medium hair REMOVE: short hair, Bob Cut AVOID: SIZE: medium"
    await bot.on_message(FakeMessage("!refine hair to the shoulders", ch))
    await finish(bot)
    job = comfy.jobs[-1]
    assert job["2"]["inputs"]["text"] == "1girl, (medium hair:1.3), green hair, smile, black sweater"
    assert job["3"]["inputs"]["text"].endswith("signature, short hair, bob cut, lowres")
    # Redrawn from the last picture, only as much as a medium change needs.
    assert job["8"]["inputs"]["image"] == "iVBORyBmYWtl" and job["5"]["inputs"]["denoise"] == 0.6
    asked = mock_api.requests[-1]["messages"][0]["content"][0]["text"]
    assert "[Tags]\n1girl, short hair, bob cut, green hair, smile, black sweater\n" in asked
    assert "medium hair (to the shoulders)" in asked
    mock_api.reply = "ADD: natural skin AVOID: pale skin SIZE: small"
    await bot.on_message(FakeMessage("!refine natural skin", ch))
    await finish(bot)
    job = comfy.jobs[-1]
    # The hair change from last round stays with its weight; short hair stays avoided.
    assert job["2"]["inputs"]["text"] == "1girl, (natural skin:1.3), (medium hair:1.3), green hair, smile, black sweater"
    assert "short hair, bob cut, lowres" in job["3"]["inputs"]["text"] and "pale skin" in job["3"]["inputs"]["text"]
    assert job["5"]["inputs"]["denoise"] == 0.55  # even a small change redraws over half
    mock_api.reply = "ADD: running REMOVE: smile SIZE: new"
    await bot.on_message(FakeMessage("!refine make her run", ch))
    await finish(bot)
    assert "8" not in comfy.jobs[-1] and comfy.jobs[-1]["5"]["inputs"]["denoise"] == 1.0  # starts over, same seed
    assert comfy.jobs[-1]["5"]["inputs"]["seed"] == comfy.jobs[0]["5"]["inputs"]["seed"]


async def test_refine_draws_again_when_comfyui_lacks_the_node(mock_api, comfy, events):
    comfy.has_edit_node = False
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    mock_api.reply = "1girl, short hair"
    await bot.on_message(FakeMessage("!draw a girl", ch))
    await finish(bot)
    mock_api.reply = "ADD: red hair SIZE: small"
    msg = FakeMessage("!refine red hair", ch)
    await bot.on_message(msg)
    await finish(bot)
    assert msg.replies[1].filename == "image.png" and "8" not in comfy.jobs[-1]


async def test_realistic_pictures_keep_anime_out(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    mock_api.reply = "photo of a woman, film grain AVOID:"
    await bot.on_message(FakeMessage("!draw realistic a woman", FakeChannel(BOT_CHANNEL)))
    await finish(bot)
    job = comfy.jobs[0]
    assert job["2"]["inputs"]["text"] == "photo of a woman, film grain"
    assert "anime" in job["3"]["inputs"]["text"] and "cel shading" in job["3"]["inputs"]["text"]
    assert job["5"]["inputs"]["sampler_name"] == "dpmpp_2m" and job["5"]["inputs"]["cfg"] == 4.5
    await bot.on_message(FakeMessage("!draw a girl", FakeChannel(BOT_CHANNEL)))
    await finish(bot)
    assert "anime" not in comfy.jobs[1]["3"]["inputs"]["text"]
    assert comfy.jobs[1]["5"]["inputs"]["sampler_name"] == "euler_ancestral"


async def test_edit_redraws_the_uploaded_picture_without_saving_it(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    ch = FakeChannel(BOT_CHANNEL)
    missing = FakeMessage("!edit make the hair red", ch)
    await bot.on_message(missing)
    assert missing.replies == [NO_PICTURE]
    mock_api.reply = "(red hair:1.3), 1girl AVOID: black hair"
    msg = FakeMessage("!edit make the hair red", ch, attachments=[FakeAttachment(b"JPEGDATA", "image/jpeg")])
    await bot.on_message(msg)
    await finish(bot)
    assert msg.replies[1].filename == "image.png"
    job = comfy.jobs[0]
    assert job["8"]["class_type"] == "BotLoadImageBase64"
    assert job["8"]["inputs"]["image"] == "SlBFR0RBVEE="  # the upload rides inside the job, no file upload
    assert job["4"]["class_type"] == "VAEEncode" and job["5"]["inputs"]["denoise"] < 1
    asked = mock_api.requests[-1]["messages"][0]["content"]
    assert "make the hair red" in asked[0]["text"]
    assert asked[1]["image_url"]["url"] == "data:image/jpeg;base64,SlBFR0RBVEE="  # Gemma sees the upload
    mock_api.reply = "ADD: smile SIZE: small"
    await bot.on_message(FakeMessage("!refine smile", ch))
    await finish(bot)
    again = comfy.jobs[1]
    assert again["2"]["inputs"]["text"] == "(red hair:1.3), (smile:1.3), 1girl"
    assert again["8"]["inputs"]["image"] == "iVBORyBmYWtl"  # refined from the edited picture, same seed
    assert again["5"]["inputs"]["seed"] == job["5"]["inputs"]["seed"]


async def test_edit_asks_for_a_comfyui_restart_when_the_node_is_missing(mock_api, comfy, events):
    comfy.has_edit_node = False
    bot = image_bot(mock_api.base_url, comfy.url)
    msg = FakeMessage("!edit red hair", FakeChannel(BOT_CHANNEL),
                      attachments=[FakeAttachment(b"PNGDATA", "image/png")])
    await bot.on_message(msg)
    await finish(bot)
    assert "restart" in msg.replies[1] and comfy.jobs == []
    assert events[-1].startswith("lms load")


def test_edit_node_is_copied_into_comfyui(tmp_path, mock_api):
    (tmp_path / "ComfyUI" / "custom_nodes").mkdir(parents=True)
    ImageMaker(make_bot(mock_api.base_url).brain, "http://x", {"anime": "m"}, comfy_dir=str(tmp_path))
    copied = tmp_path / "ComfyUI" / "custom_nodes" / "discord_bot_image.py"
    assert copied.read_text() == open(imagegen.NODE_FILE).read()


async def test_recall_and_edit_work_with_full_width_bang_in_any_channel(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    other = FakeChannel(CHANNEL)  # not a bot channel
    msg = FakeMessage("！recall", other)
    await bot.on_message(msg)
    assert msg.replies == [NOTHING_TO_RECALL]
    mock_api.reply = "1girl, red hair"
    edit = FakeMessage("！edit red hair", other, attachments=[FakeAttachment(b"PNGDATA", "image/png")])
    await bot.on_message(edit)
    await finish(bot)
    assert edit.replies[1].filename == "image.png"
    back = FakeMessage("/recall 1", other)
    await bot.on_message(back)
    assert back.replies == [RECALLED.format(number=1)]


async def test_bare_edit_explains_itself_instead_of_chatting(mock_api, comfy, events):
    from bot import EDIT_USAGE
    bot = image_bot(mock_api.base_url, comfy.url)
    msg = FakeMessage("!edit", FakeChannel(CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == [EDIT_USAGE] and not mock_api.requests


async def test_edit_with_a_second_picture_puts_that_character_in_the_first(mock_api, comfy, events):
    from imagegen import EDIT_STRENGTH, SWAP_STRENGTH
    bot = image_bot(mock_api.base_url, comfy.url)
    mock_api.reply = "1girl, (blue hair:1.3), (red horns:1.3), maid outfit AVOID: pink hair"
    msg = FakeMessage("!edit 第2张的角色穿第1张的衣服", FakeChannel(BOT_CHANNEL), attachments=[
        FakeAttachment(b"BASE", "image/png"), FakeAttachment(b"CHAR", "image/jpeg")])
    await bot.on_message(msg)
    await finish(bot)
    assert msg.replies[1].filename == "image.png"
    asked = mock_api.requests[-1]["messages"][0]["content"]
    assert "Picture 2" in asked[0]["text"]  # Gemma is told which picture is which
    assert asked[1]["image_url"]["url"] == "data:image/png;base64,QkFTRQ=="  # picture 1: redrawn
    assert asked[2]["image_url"]["url"] == "data:image/jpeg;base64,Q0hBUg=="  # picture 2: the character
    job = comfy.jobs[0]
    assert job["8"]["inputs"]["image"] == "QkFTRQ=="  # only picture 1 goes to ComfyUI
    assert job["5"]["inputs"]["denoise"] == SWAP_STRENGTH > EDIT_STRENGTH  # looks change more than a tweak


async def test_slash_edit_passes_the_character_picture(mock_api, comfy, events):
    from bot import ChatBot
    from tests.test_bot import slash_interaction
    bot = ChatBot({"token": "x", "base_url": mock_api.base_url, "model": "gemma4-12b-bionic-v2", "channel_ids": set(),
                   "allowed_users": set(), "image_gen": True, "sd_checkpoint": "model.safetensors",
                   "comfyui_url": comfy.url, "image_size": 1024, "lmstudio_context": 16384})
    queued = []
    bot.queue_picture = lambda *args: queued.append(args) or "🎨 Drawing"
    interaction = slash_interaction()
    interaction.user.mention = "<@5>"
    await bot.tree.get_command("edit").callback(interaction, FakeAttachment(b"BASE", "image/png"), "换成她",
                                               FakeAttachment(b"CHAR", "image/jpeg"))
    assert queued[0][-4:] == (b"BASE", "image/png", b"CHAR", "image/jpeg")


async def test_estimate_comes_from_this_pcs_last_pictures(mock_api, comfy, events):
    from bot import eta_text
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "model.safetensors"})
    assert maker.estimate() is None  # nothing timed yet since the restart
    mock_api.reply = "a cat"
    await draw(maker, "画一只猫")
    assert maker.timings["draw"] and maker.timings["switch"] and maker.timings["prompt:draw"]
    maker.timings.clear()
    maker.timings.update(switch=deque([60.0]), draw=deque([40.0, 50.0]), prompt=deque([20.0, 100.0]))
    maker.timings["prompt:edit"] = deque([100.0])
    maker.queue.extend([Job("a", 1, 1, None), Job("b", 1, 2, None, source=b"PNG")])
    maker.waiting.update({1, 2})
    assert maker.estimate() == 60 + (60 + 45) + (100 + 45)  # a plain draw falls back to every prompt's average
    assert eta_text(None).startswith("⏱️") and eta_text(45.4) == "⏱️ 预计 45 秒 Estimate 45 sec"
    assert eta_text(1255) == "⏱️ 预计 20 分 55 秒 Estimate 20 min 55 sec"


async def test_queue_notice_tells_the_estimate(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    bot.images.timings.update(switch=deque([30.0]), draw=deque([60.0]), prompt=deque([30.0]))
    msg = FakeMessage("!draw a cat", FakeChannel(BOT_CHANNEL))
    mock_api.reply = "a cat"
    await bot.on_message(msg)
    assert msg.replies[0] == f"{DRAWING_NOTICE}\n⏱️ 预计 2 分 0 秒 Estimate 2 min 0 sec"


async def test_big_pictures_are_shrunk_for_gemma_and_comfyui(mock_api, comfy, events):
    import base64
    import io
    from PIL import Image
    big = io.BytesIO()
    Image.new("RGB", (3000, 1500), "red").save(big, "JPEG")
    bot = image_bot(mock_api.base_url, comfy.url)
    mock_api.reply = "1girl, red background"
    msg = FakeMessage("!edit make it blue", FakeChannel(BOT_CHANNEL), attachments=[FakeAttachment(big.getvalue(), "image/jpeg")])
    await bot.on_message(msg)
    await finish(bot)
    seen = mock_api.requests[-1]["messages"][0]["content"][1]["image_url"]["url"]
    assert seen.startswith("data:image/png;base64,")
    assert Image.open(io.BytesIO(base64.b64decode(seen.split(",", 1)[1]))).size == (1024, 512)
    sent = base64.b64decode(comfy.jobs[0]["8"]["inputs"]["image"])
    assert Image.open(io.BytesIO(sent)).size == (1024, 512)  # ComfyUI gets the small one too


async def test_slow_gemma_gives_up_once_with_a_countdown(mock_api, comfy, events, monkeypatch):
    import core
    from imagegen import TOO_SLOW
    monkeypatch.setattr(core, "IMAGE_PROMPT_TIMEOUT", 0.3)
    monkeypatch.setattr(imagegen, "COUNTDOWN_EVERY", 0.1)
    mock_api.delay = 0.5
    maker = ImageMaker(make_bot(mock_api.base_url).brain, comfy.url, {"anime": "model.safetensors"})
    notes, done = [], asyncio.get_running_loop().create_future()

    async def deliver(png, error):
        done.set_result((png, error))

    async def progress(update):
        notes.append(update)
    maker.submit("a cat", 1, 1, deliver, progress=progress)
    assert await done == (None, TOO_SLOW)
    assert len(mock_api.requests) == 1  # no retries piling up on LM Studio
    assert notes and all(isinstance(n, str) and "最多再等" in n for n in notes)


async def test_countdown_text_shows_under_the_drawing_notice(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    edits = []

    async def edit(text):
        edits.append(text)

    async def send(text=None, file=None):
        pass
    bot.queue_picture("a cat", 1, 1, False, send, edit)
    job = bot.images.queue[0]
    await job.progress("🧠 最多再等 4 分 45 秒")
    await job.progress(50)
    assert edits == [f"{DRAWING_NOTICE}\n🧠 最多再等 4 分 45 秒", f"{DRAWING_NOTICE}\n▓▓▓▓▓░░░░░ 50%"]
    await finish(bot)


async def test_edit_redraws_as_much_as_gemma_says_the_change_needs(mock_api, comfy, events):
    bot = image_bot(mock_api.base_url, comfy.url)
    mock_api.reply = "1girl, (red hair:1.3), horns, starry sky AVOID: blue hair, ganyu (genshin impact) SIZE: big"
    msg = FakeMessage("!edit 把头发改成红色", FakeChannel(BOT_CHANNEL), attachments=[FakeAttachment(b"PNG", "image/png")])
    await bot.on_message(msg)
    await finish(bot)
    job = comfy.jobs[0]
    assert job["5"]["inputs"]["denoise"] == 0.75
    assert job["2"]["inputs"]["text"] == "1girl, (red hair:1.3), horns, starry sky"
    assert "blue hair, ganyu (genshin impact)" in job["3"]["inputs"]["text"] and "SIZE" not in job["3"]["inputs"]["text"]
    asked = mock_api.requests[-1]["messages"][0]["content"][0]["text"]
    assert "big for hair color" in asked and "leave out the character's name" in asked


def _picture(color, size=(64, 64)):
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def _colorfulness(data):
    import io
    from PIL import Image
    r, g, b = Image.open(io.BytesIO(data)).convert("RGB").getpixel((5, 5))
    return max(r, g, b) - min(r, g, b)


def test_fade_colors_takes_most_color_out_and_keeps_outlines():
    import io
    from PIL import Image, ImageDraw
    from imagegen import fade_colors
    blue = _picture((40, 90, 230))
    assert _colorfulness(fade_colors(blue)) < _colorfulness(blue) * 0.5
    picture = Image.new("RGB", (64, 64), (120, 120, 200))
    ImageDraw.Draw(picture).line((32, 0, 32, 63), fill=(120, 120, 200))
    ImageDraw.Draw(picture).rectangle((33, 0, 63, 63), fill=(200, 120, 120))  # same brightness, other hue
    out = io.BytesIO()
    picture.save(out, "PNG")
    faded = Image.open(io.BytesIO(fade_colors(out.getvalue()))).convert("L")
    assert faded.getpixel((32, 30)) < faded.getpixel((10, 30)) - 20  # the border is drawn darker
    assert fade_colors(b"not a picture") == b"not a picture"


async def test_recolor_edit_and_refine_fade_the_old_colors(mock_api, comfy, events, monkeypatch):
    import base64
    bot = image_bot(mock_api.base_url, comfy.url)
    blue = _picture((40, 90, 230))
    mock_api.reply = "1girl, (red hair:1.3), horns AVOID: blue hair SIZE: big RECOLOR"
    msg = FakeMessage("!edit 把头发改成红色", FakeChannel(BOT_CHANNEL), attachments=[FakeAttachment(blue, "image/png")])
    await bot.on_message(msg)
    await finish(bot)
    job = comfy.jobs[0]
    assert job["2"]["inputs"]["text"] == "1girl, (red hair:1.3), horns" and "RECOLOR" not in job["3"]["inputs"]["text"]
    sent = base64.b64decode(job["8"]["inputs"]["image"])
    # Big change, but fading frees the color, so it redraws only 60% and the shapes stay.
    assert _colorfulness(sent) < _colorfulness(blue) * 0.5 and job["5"]["inputs"]["denoise"] == 0.6
    mock_api.reply = "ADD: smile SIZE: small"
    await bot.on_message(FakeMessage("!refine smile", FakeChannel(BOT_CHANNEL)))
    await finish(bot)
    assert base64.b64decode(comfy.jobs[1]["8"]["inputs"]["image"]) == PNG  # no recolor: the last picture as is
    monkeypatch.setattr(imagegen, "fade_colors", lambda data: b"FADED")
    mock_api.reply = "ADD: green hair REMOVE: (red hair:1.3) SIZE: big RECOLOR"
    await bot.on_message(FakeMessage("!refine green hair", FakeChannel(BOT_CHANNEL)))
    await finish(bot)
    assert base64.b64decode(comfy.jobs[2]["8"]["inputs"]["image"]) == b"FADED"
    assert "RECOLOR" not in comfy.jobs[2]["2"]["inputs"]["text"] + comfy.jobs[2]["3"]["inputs"]["text"]
