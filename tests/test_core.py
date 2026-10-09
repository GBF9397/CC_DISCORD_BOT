import asyncio

from core import DEFAULT_PERSONA, OFFLINE_MESSAGE, PERSONAS, Brain, ChannelMemory, split_message


def make_brain(url):
    return Brain(url, "gemma4-12b-bionic-v2", ChannelMemory())


async def test_reply_uses_content_not_reasoning(mock_api):
    brain = make_brain(mock_api.base_url)
    reply = await brain.ask(1, "Alex", "hello")
    assert reply == "echo: Alex: hello"
    assert "SECRET" not in reply
    sent = mock_api.requests[0]
    assert sent["model"] == "gemma4-12b-bionic-v2"
    assert sent["temperature"] == 0.9 and sent["top_p"] == 0.95
    assert sent["messages"][0]["role"] == "system"
    assert "tools" not in sent


async def test_memory_is_per_channel_and_resettable(mock_api):
    brain = make_brain(mock_api.base_url)
    await brain.ask(1, "Alex", "first")
    await brain.ask(2, "Bo", "other channel")
    await brain.ask(1, "Alex", "second")
    history = mock_api.requests[-1]["messages"]
    contents = [m["content"] for m in history]
    assert "Alex: first" in contents and "Bo: other channel" not in contents
    brain.memory.reset(1)
    await brain.ask(1, "Alex", "after reset")
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
    assert await brain.ask(1, "Alex", "hi") == OFFLINE_MESSAGE
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
    await brain.ask(1, "Alex", "hi")
    assert PERSONAS[DEFAULT_PERSONA] in mock_api.requests[0]["messages"][0]["content"]
    assert "name" in mock_api.requests[0]["messages"][0]["content"]
    brain.set_persona(1, PERSONAS["pirate"])
    assert brain.memory.get(1) == []
    await brain.ask(1, "Alex", "ahoy")
    await brain.ask(2, "Bo", "hello")
    assert PERSONAS["pirate"] in mock_api.requests[1]["messages"][0]["content"]
    assert PERSONAS[DEFAULT_PERSONA] in mock_api.requests[2]["messages"][0]["content"]


def test_apply_extras_converts_emoji_and_pulls_sticker():
    from core import apply_extras, extras_note
    emojis, stickers = {"pepe": "<:pepe:1>"}, {"catjam": "STICKER"}
    assert apply_extras("hi :pepe: at 12:30:00 <:pepe:1> :nope:", emojis, {}) == \
        ("hi <:pepe:1> at 12:30:00 <:pepe:1> :nope:", None)
    assert apply_extras("我不是NPC。pepe", emojis, {}) == ("我不是NPC。 <:pepe:1>", None)
    assert apply_extras("pepe is a frog", emojis, {}) == ("pepe is a frog", None)
    assert apply_extras("我是pepe", emojis, {}) == ("我是pepe", None)
    assert apply_extras("好的：pepe：", emojis, {}) == ("好的 <:pepe:1>", None)
    assert apply_extras("好的:pepe:<a:x:2>", emojis, {}) == ("好的 <:pepe:1> <a:x:2>", None)
    assert apply_extras("出发了！:pepe", emojis, {}) == ("出发了！ <:pepe:1>", None)  # closing colon missing
    assert apply_extras("肉！:pepe :nope 冒险！", emojis, {}) == ("肉！ <:pepe:1> :nope 冒险！", None)
    assert apply_extras("at 12:30 ok", emojis, {}) == ("at 12:30 ok", None)
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
    await make_brain(mock_api.base_url).ask(1, "Alex", "hi")
    assert UNFILTERED_NOTE not in mock_api.requests[-1]["messages"][0]["content"]
    brain = Brain(mock_api.base_url, "gemma4-12b-bionic-v2", ChannelMemory(), unfiltered=True)
    await brain.ask(1, "Alex", "hi")
    system = mock_api.requests[-1]["messages"][0]["content"]
    assert UNFILTERED_NOTE in system and "minors" in system


async def test_each_reply_draws_a_length_cap_and_long_replies_are_cut(mock_api, monkeypatch):
    import core
    monkeypatch.setattr(core.random, "choice", lambda options: 10)
    mock_api.reply = "哈哈哈哈哈哈。今天天气真好呀！你呢？"
    brain = make_brain(mock_api.base_url)
    reply = await brain.ask(1, "Alex", "hi")
    assert reply == "哈哈哈哈哈哈。今天天气真好呀！"  # cut at a sentence end within 1.5x the cap
    sent = mock_api.requests[0]["messages"]
    assert "Length limit" not in sent[0]["content"]  # with the newest message, not the system prompt
    assert sent[-1]["content"].startswith("Alex: hi\n\n[Length limit for this reply: at most 10 Chinese")
    assert brain.memory.get(1) == [{"role": "user", "content": "Alex: hi"},
                                   {"role": "assistant", "content": "哈哈哈哈哈哈。今天天气真好呀！"}]
    assert "max_tokens" not in mock_api.requests[-1]  # reasoning needs the room


def test_shorten_counts_chinese_characters_and_english_words():
    from core import REPLY_LENGTHS, shorten
    assert REPLY_LENGTHS == (10, 30, 50, 100)
    assert shorten("short reply", 10) == "short reply"
    assert shorten("一二三四五六七八九十十一", 10) == "一二三四五六七八九十十一"  # a bit over is kept whole
    assert shorten("一二三四五六七八九十" * 2, 10) == "一二三四五六七八九十一二三四五…"
    assert shorten("one two. three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen",
                   10) == "one two. three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen…"
    assert shorten("one two three four five six seven. eight nine ten eleven twelve thirteen fourteen fifteen sixteen",
                   10) == "one two three four five six seven."
    assert shorten("好的 :catcry: 我知道了，然后还有很多很多话要说", 6) == "好的 :catcry: 我知道了"
    assert shorten("好的 :catcry: 我知道了然后还有很多很多话要说", 5) == "好的 :catcry: 我知道了…"


async def test_character_persona_looks_things_up_quietly(mock_api):
    import core
    brain = make_brain(mock_api.base_url)
    assert brain.lore_query(1, "枫丹的歌剧院叫什么？") is None  # not a character persona
    brain.set_persona(1, "Furina text", character="芙宁娜 原神")
    assert brain.lore_query(1, "枫丹的歌剧院叫什么？") == "芙宁娜 原神 枫丹的歌剧院叫什么？"
    assert brain.lore_query(1, "") is None
    queries = [brain.lore_query(1, "哈哈好的") for _ in range(core.LORE_EVERY * 2)]
    refresh = "芙宁娜 原神 character personality speech style quotes"
    assert queries.count(refresh) == 2 and set(queries) == {None, refresh}
    brain.set_persona(1, "a grumpy cat")
    assert brain.lore_query(1, "who are you?") is None

    mock_api.reply = "I am Furina!"
    await brain.ask(1, "Alex", "who are you?", lore="Opera Epiclese")
    sent = mock_api.requests[-1]["messages"][-1]["content"]
    assert "Opera Epiclese" in sent and "looked up quietly" in sent and sent.count("Alex: who are you?") == 1
    assert "Opera Epiclese" not in str(brain.memory.get(1))


async def test_character_persona_is_told_not_to_repeat_catchphrases(mock_api):
    mock_api.reply = "King of Curses."
    text = await make_brain(mock_api.base_url).character_persona("Sukuna", "results")
    assert "never repeat a word or phrase" in text and "is Sukuna only when it clearly is" in text


async def test_reply_over_the_cap_is_rewritten_within_it(mock_api, monkeypatch):
    import core
    monkeypatch.setattr(core.random, "choice", lambda options: 10)
    mock_api.reply = ["一二三四五六七八九十" * 3, "好的，懂了。"]
    brain = make_brain(mock_api.base_url)
    app_reply = await brain.ask(1, "Alex", "hi")
    assert app_reply == "好的，懂了。" and len(mock_api.requests) == 2
    assert "at most 10 Chinese characters" in mock_api.requests[1]["messages"][-1]["content"]
    assert brain.memory.get(1)[-1]["content"] == "好的，懂了。"
