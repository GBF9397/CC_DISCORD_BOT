import asyncio

from core import DEFAULT_PERSONA, OFFLINE_MESSAGE, PERSONAS, Brain, ChannelMemory, split_message


def make_brain(url):
    return Brain(url, "gemma4-12b-bionic-v2", ChannelMemory())


async def test_reply_uses_content_not_reasoning(mock_api):
    brain = make_brain(mock_api.base_url)
    reply = await brain.ask(1, "Ep", "hello")
    assert reply == "echo: Ep: hello"
    assert "SECRET" not in reply
    sent = mock_api.requests[0]
    assert sent["model"] == "gemma4-12b-bionic-v2"
    assert sent["temperature"] == 0.9 and sent["top_p"] == 0.95
    assert sent["messages"][0]["role"] == "system"
    assert "tools" not in sent


async def test_memory_is_per_channel_and_resettable(mock_api):
    brain = make_brain(mock_api.base_url)
    await brain.ask(1, "Ep", "first")
    await brain.ask(2, "Bo", "other channel")
    await brain.ask(1, "Ep", "second")
    history = mock_api.requests[-1]["messages"]
    contents = [m["content"] for m in history]
    assert "Ep: first" in contents and "Bo: other channel" not in contents
    brain.memory.reset(1)
    await brain.ask(1, "Ep", "after reset")
    assert len(mock_api.requests[-1]["messages"]) == 2  # system + new message


def test_memory_keeps_last_ten_and_trims_by_tokens():
    mem = ChannelMemory(max_messages=10, max_tokens=50)
    for i in range(15):
        mem.add(1, "user", f"msg {i}")
    assert [m["content"] for m in mem.get(1)][0] == "msg 5"
    mem.add(1, "user", "x" * 400)  # ~100 tokens, over budget on its own
    assert mem.get(1) == []


def test_memory_defaults_to_thirty_messages():
    mem = ChannelMemory()
    for i in range(35):
        mem.add(1, "user", f"a typical chat message number {i}")
    kept = mem.get(1)
    assert len(kept) == 30 and kept[0]["content"].endswith("number 5")


async def test_requests_run_one_at_a_time(mock_api):
    mock_api.delay = 0.1
    brain = make_brain(mock_api.base_url)
    replies = await asyncio.gather(*(brain.ask(c, "u", f"q{c}") for c in range(4)))
    assert mock_api.max_active == 1
    assert replies == [f"echo: u: q{c}" for c in range(4)]


async def test_offline_gives_friendly_error_and_keeps_no_memory():
    brain = make_brain("http://127.0.0.1:9/v1")  # nothing listens here
    brain.client = brain.client.with_options(max_retries=0)
    assert await brain.ask(1, "Ep", "hi") == OFFLINE_MESSAGE
    assert brain.memory.get(1) == []


def test_split_message_respects_discord_limit():
    text = "\n".join("line %d %s" % (i, "y" * 90) for i in range(60))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert all(len(c) <= 2000 for c in split_message("z" * 4500))
    assert split_message("short") == ["short"]


def test_bot_channel_id_accepts_several_ids(monkeypatch):
    from core import load_config
    monkeypatch.setenv("BOT_CHANNEL_ID", "1556580599748108389, 1556584437339398144")
    assert load_config()["channel_ids"] == {1556580599748108389, 1556584437339398144}
    monkeypatch.setenv("BOT_CHANNEL_ID", "")
    assert load_config()["channel_ids"] == set()


async def test_persona_is_per_channel_and_switch_clears_memory(mock_api):
    brain = make_brain(mock_api.base_url)
    await brain.ask(1, "Ep", "hi")
    assert mock_api.requests[0]["messages"][0]["content"].endswith(PERSONAS[DEFAULT_PERSONA])
    assert "name" in mock_api.requests[0]["messages"][0]["content"]
    brain.set_persona(1, PERSONAS["pirate"])
    assert brain.memory.get(1) == []
    await brain.ask(1, "Ep", "ahoy")
    await brain.ask(2, "Bo", "hello")
    assert mock_api.requests[1]["messages"][0]["content"].endswith(PERSONAS["pirate"])
    assert mock_api.requests[2]["messages"][0]["content"].endswith(PERSONAS[DEFAULT_PERSONA])


def test_apply_extras_converts_emoji_and_pulls_sticker():
    from core import apply_extras, extras_note
    emojis, stickers = {"pepe": "<:pepe:1>"}, {"catjam": "STICKER"}
    assert apply_extras("hi :pepe: at 12:30:00 <:pepe:1> :nope:", emojis, {}) == \
        ("hi <:pepe:1> at 12:30:00 <:pepe:1> :nope:", None)
    assert apply_extras("ok [Sticker: catjam]", emojis, stickers) == ("ok", "STICKER")
    assert apply_extras("[sticker: catjam]", emojis, stickers) == ("", "STICKER")
    assert apply_extras("[sticker: unknown]", emojis, stickers) == ("...", None)
    assert extras_note({}, {}) == ""
    assert "pepe" in extras_note(emojis, {}) and "sticker" not in extras_note(emojis, {})
    assert "catjam" in extras_note({}, stickers)


def test_load_meanings_reads_name_colon_meaning(tmp_path):
    from core import load_meanings
    f = tmp_path / "m.txt"
    f.write_text("# my notes\ncatstare: speechless at nonsense\n:awk: awkward  # comment\nbad line\n",
                 encoding="utf-8")
    assert load_meanings(f) == {"catstare": "speechless at nonsense", "awk": "awkward"}
    assert load_meanings(tmp_path / "missing.txt") == {}


async def test_unfiltered_mode_is_off_by_default_and_loosens_prompt_when_on(mock_api, monkeypatch):
    from core import UNFILTERED_NOTE, load_config
    monkeypatch.delenv("UNFILTERED_MODE", raising=False)
    assert load_config()["unfiltered"] is False
    assert load_config()["stickers"] is False
    monkeypatch.setenv("UNFILTERED_MODE", "on")
    assert load_config()["unfiltered"] is True
    await make_brain(mock_api.base_url).ask(1, "Ep", "hi")
    assert UNFILTERED_NOTE not in mock_api.requests[-1]["messages"][0]["content"]
    brain = Brain(mock_api.base_url, "gemma4-12b-bionic-v2", ChannelMemory(), unfiltered=True)
    await brain.ask(1, "Ep", "hi")
    system = mock_api.requests[-1]["messages"][0]["content"]
    assert UNFILTERED_NOTE in system and "minors" in system
