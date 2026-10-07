"""Drives the bot's message and slash-command handlers with fake Discord objects."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone

import discord

from bot import NO_EVENT_PERMISSION, ChatBot, event_start, poll_answers

BOT_ID, CHANNEL, BOT_CHANNEL, BOT_CHANNEL_2 = 999, 10, 20, 30


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent, self.typing_used = [], False

    @asynccontextmanager
    async def _typing(self):
        self.typing_used = True
        yield

    def typing(self):
        return self._typing()

    async def send(self, text=None, stickers=None):
        self.sent.append(text if stickers is None else stickers)


class FakeMessage:
    def __init__(self, content, channel, author_id=1, bot=False, mentions=(), attachments=(), guild=None):
        self.content = content
        self.guild = guild
        self.stickers = []
        self.attachments = list(attachments)
        self.channel = channel
        self.author = SimpleNamespace(id=author_id, bot=bot, display_name=f"user{author_id}")
        self.mentions = list(mentions)
        self.replies = []

    async def reply(self, text=None, mention_author=True, file=None):
        self.replies.append(text if file is None else file)
        self.mentioned = mention_author
        self.sent = FakeSent()
        return self.sent


class FakeSent:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None):
        self.edits.append(content)


class FakeAttachment:
    def __init__(self, data, content_type):
        self.data, self.content_type = data, content_type

    async def read(self):
        return self.data


def make_bot(url, allowed=(), stickers=True):
    bot = ChatBot({"token": "x", "base_url": url, "model": "gemma4-12b-bionic-v2",
                   "channel_ids": {BOT_CHANNEL, BOT_CHANNEL_2}, "allowed_users": set(allowed),
                   "stickers": stickers})
    bot._connection.user = SimpleNamespace(id=BOT_ID)
    return bot


async def test_replies_when_mentioned(mock_api):
    bot = make_bot(mock_api.base_url)
    ch = FakeChannel(CHANNEL)
    msg = FakeMessage(f"<@{BOT_ID}> hi there", ch, mentions=[bot.user])
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: hi there"]
    assert ch.typing_used


async def test_replies_in_bot_channel_and_ignores_others(mock_api):
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("hello", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: hello"]
    second = FakeMessage("hi", FakeChannel(BOT_CHANNEL_2))
    await bot.on_message(second)
    assert second.replies == ["echo: user1: hi"]
    quiet = FakeMessage("hello", FakeChannel(CHANNEL))
    await bot.on_message(quiet)
    assert quiet.replies == []


async def test_ignores_bots_and_non_allowlisted(mock_api):
    bot = make_bot(mock_api.base_url, allowed={1})
    from_bot = FakeMessage("hi", FakeChannel(BOT_CHANNEL), author_id=1, bot=True)
    stranger = FakeMessage("hi", FakeChannel(BOT_CHANNEL), author_id=2)
    await bot.on_message(from_bot)
    await bot.on_message(stranger)
    assert from_bot.replies == stranger.replies == []
    assert mock_api.requests == []


async def test_bang_reset_clears_channel_memory(mock_api):
    bot = make_bot(mock_api.base_url)
    ch = FakeChannel(BOT_CHANNEL)
    await bot.on_message(FakeMessage("remember me", ch))
    reset = FakeMessage("!reset", ch)
    await bot.on_message(reset)
    assert reset.replies == ["Memory for this channel cleared."]
    assert bot.memory.get(BOT_CHANNEL) == []


async def test_long_reply_is_split(mock_api, monkeypatch):
    import core
    monkeypatch.setattr(core, "shorten", lambda reply, limit: reply)  # test splitting alone
    mock_api.reply = "word " * 1000
    bot = make_bot(mock_api.base_url)
    ch = FakeChannel(BOT_CHANNEL)
    msg = FakeMessage("long please", ch)
    await bot.on_message(msg)
    assert len(msg.replies) == 1 and ch.sent
    assert all(len(t) <= 2000 for t in msg.replies + ch.sent)


async def test_slash_commands_registered_and_work(mock_api):
    bot = make_bot(mock_api.base_url)
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"ask", "reset", "search", "persona", "poll", "event"}

    sent = []
    async def followup_send(text):
        sent.append(text)
    async def defer(thinking=False):
        pass
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=5, display_name="member"), channel_id=CHANNEL, guild=None,
        response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send))
    await bot.tree.get_command("ask").callback(interaction, "what is 2+2?")
    assert sent == ["echo: member: what is 2+2?"]


async def test_on_ready_warns_when_bot_channel_not_visible(mock_api, caplog):
    bot = make_bot(mock_api.base_url)
    bot.config["channel_ids"] = {BOT_CHANNEL}
    bot.get_channel = lambda cid: None
    with caplog.at_level("INFO", logger="bot"):
        await bot.on_ready()
    assert f"BOT_CHANNEL_ID {BOT_CHANNEL} not found" in caplog.text
    bot.get_channel = lambda cid: "sad"
    caplog.clear()
    with caplog.at_level("INFO", logger="bot"):
        await bot.on_ready()
    assert "Answering every message in #sad" in caplog.text


async def test_image_attachments_are_sent_to_the_model(mock_api):
    bot = make_bot(mock_api.base_url)
    ch = FakeChannel(BOT_CHANNEL)
    png = FakeAttachment(b"PNGDATA", "image/png")
    text_file = FakeAttachment(b"notes", "text/plain")
    msg = FakeMessage("what is this?", ch, attachments=[png, text_file])
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: what is this?"]
    parts = mock_api.requests[0]["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "user1: what is this?"}
    assert parts[1:] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,UE5HREFUQQ=="}}]
    # Memory keeps only text, never the image bytes.
    assert bot.memory.get(BOT_CHANNEL)[0]["content"] == "user1: what is this? [sent 1 image(s)]"


async def test_image_without_text_still_gets_a_reply(mock_api):
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("", FakeChannel(BOT_CHANNEL),
                      attachments=[FakeAttachment(b"x", "image/jpeg; charset=binary")])
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1:"]
    assert mock_api.requests[0]["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_non_image_attachment_alone_is_ignored(mock_api):
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("", FakeChannel(BOT_CHANNEL), attachments=[FakeAttachment(b"x", "text/plain")])
    await bot.on_message(msg)
    assert msg.replies == [] and mock_api.requests == []

async def test_persona_command_switches_and_shows(mock_api):
    from discord import app_commands
    from core import CUSTOM_MAX_CHARS, PERSONAS
    bot = make_bot(mock_api.base_url)
    said = []
    async def send_message(text, ephemeral=False):
        said.append((text, ephemeral))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=5, display_name="member"), channel_id=CHANNEL,
        response=SimpleNamespace(send_message=send_message))
    cmd = bot.tree.get_command("persona").callback

    await cmd(interaction, app_commands.Choice(name="wuxia", value="wuxia"), None)
    assert bot.brain.persona(CHANNEL) == PERSONAS["wuxia"]
    assert "member switched me to **wuxia**" in said[-1][0] and not said[-1][1]

    await cmd(interaction, None, "a grumpy cat " + "x" * 500)
    assert bot.brain.persona(CHANNEL).startswith("a grumpy cat")
    assert len(bot.brain.persona(CHANNEL)) == CUSTOM_MAX_CHARS

    await cmd(interaction, None, None)
    assert said[-1][1] and "grumpy cat" in said[-1][0] and "pirate" in said[-1][0]


class FakeEmoji(SimpleNamespace):
    def __str__(self):
        return f"<a:{self.name}:42>"


async def test_uses_server_emoji_and_sometimes_a_sticker(mock_api, monkeypatch):
    import bot as bot_module
    guild = SimpleNamespace(
        emojis=[FakeEmoji(id=1, name="pepe", available=True), FakeEmoji(id=2, name="gone", available=False)],
        stickers=[SimpleNamespace(id=3, name="catjam", available=True, description="dancing cat", emoji="cat")])
    mock_api.reply = "lol :pepe: :gone: [sticker: catjam]"
    monkeypatch.setattr(bot_module, "load_meanings", lambda: {"catjam": "party mood"})
    bot = make_bot(mock_api.base_url)
    bot.meanings[1] = "smug laugh"
    ch = FakeChannel(BOT_CHANNEL)

    monkeypatch.setattr(bot_module.random, "random", lambda: 0.0)  # offer stickers this time
    msg = FakeMessage("hi", ch, guild=guild)
    await bot.on_message(msg)
    system = mock_api.requests[0]["messages"][0]["content"]
    assert "pepe (smug laugh)" in system and "gone" not in system and "catjam (party mood; cat)" in system
    assert msg.replies == ["lol <a:pepe:42> :gone:"]
    assert ch.sent == [[guild.stickers[0]]]
    assert "[sticker" not in bot.memory.get(BOT_CHANNEL)[-1]["content"]

    monkeypatch.setattr(bot_module.random, "random", lambda: 0.99)  # no stickers offered
    ch.sent.clear()
    await bot.on_message(FakeMessage("again", ch, guild=guild))
    assert "catjam" not in mock_api.requests[1]["messages"][0]["content"]
    assert ch.sent == []


async def test_learns_emoji_meanings_from_pictures_once(mock_api, monkeypatch):
    import discord
    import bot as bot_module
    monkeypatch.setattr(bot_module, "load_meanings", lambda: {"handset": "written by hand"})
    mock_api.reply = "  happy\n dancing cat "
    bot = make_bot(mock_api.base_url)
    fetched = []
    async def get_from_cdn(url):
        fetched.append(url)
        return b"PNG"
    bot.http.get_from_cdn = get_from_cdn
    guild = SimpleNamespace(
        emojis=[FakeEmoji(id=7, name="cat", available=True), FakeEmoji(id=6, name="handset", available=True)],
        stickers=[SimpleNamespace(id=8, name="s8", url="https://x/8.png", format=discord.StickerFormatType.png),
                  SimpleNamespace(id=9, name="s9", url="https://x/9.json", format=discord.StickerFormatType.lottie)])
    await bot.learn_meanings(guild)
    await bot.learn_meanings(guild)  # already known: not asked again
    assert fetched == ["https://cdn.discordapp.com/emojis/7.png", "https://x/8.png"]
    assert bot.meanings == {7: "happy dancing cat", 8: "happy dancing cat"}
    assert mock_api.requests[0]["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,UE5H"


async def test_stickers_off_never_learns_or_sends_them(mock_api, monkeypatch):
    import discord
    import bot as bot_module
    monkeypatch.setattr(bot_module, "load_meanings", lambda: {})
    monkeypatch.setattr(bot_module.random, "random", lambda: 0.0)
    mock_api.reply = "lol [sticker: catjam]"
    bot = make_bot(mock_api.base_url, stickers=False)
    fetched = []
    async def get_from_cdn(url):
        fetched.append(url)
        return b"PNG"
    bot.http.get_from_cdn = get_from_cdn
    guild = SimpleNamespace(emojis=[], stickers=[SimpleNamespace(
        id=3, name="catjam", available=True, description="", emoji="", url="https://x/3.png",
        format=discord.StickerFormatType.png)])
    await bot.learn_meanings(guild)
    assert fetched == []
    ch = FakeChannel(BOT_CHANNEL)
    await bot.on_message(FakeMessage("hi", ch, guild=guild))
    assert "catjam" not in mock_api.requests[0]["messages"][0]["content"]
    assert ch.sent == []


async def test_learns_from_how_members_use_emoji(mock_api, monkeypatch):
    import bot as bot_module
    monkeypatch.setattr(bot_module.random, "random", lambda: 0.0)
    guild = SimpleNamespace(emojis=[FakeEmoji(id=1, name="kekw", available=True)],
                            stickers=[SimpleNamespace(id=3, name="catjam", available=True, description="", emoji="")])
    bot = make_bot(mock_api.base_url)
    elsewhere = FakeChannel(CHANNEL)  # bot doesn't answer here, but still learns
    await bot.on_message(FakeMessage("lol you lost again <:kekw:1>", elsewhere, guild=guild))
    await bot.on_message(FakeMessage("<:kekw:1>", elsewhere, guild=guild))  # emoji alone: nothing to learn
    sticky = FakeMessage("party time " + "x" * 100, elsewhere, guild=guild)
    sticky.stickers = [SimpleNamespace(id=3)]
    await bot.on_message(sticky)
    assert mock_api.requests == []

    await bot.on_message(FakeMessage("hi", FakeChannel(BOT_CHANNEL), guild=guild))
    system = mock_api.requests[0]["messages"][0]["content"]
    assert 'kekw (used like "lol you lost again :kekw:")' in system
    assert 'catjam (used like "party time ' in system and '..."' in system


async def test_bang_search_reply_has_no_length_cap(mock_api, monkeypatch):
    import bot as bot_module

    async def fake_search(query):
        return "result"
    monkeypatch.setattr(bot_module, "web_search", fake_search)
    mock_api.reply = "一二三四五六七八九十" * 20
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("!search news", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["一二三四五六七八九十" * 20]
    assert "Length limit" not in mock_api.requests[-1]["messages"][0]["content"]


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, ephemeral=False, poll=None):
        self.sent.append((content, ephemeral, poll))


def slash_interaction(guild=None):
    return SimpleNamespace(user=SimpleNamespace(id=5, display_name="member"), channel_id=CHANNEL,
                           guild=guild, response=FakeResponse())


def test_poll_answers_split_and_default_to_yes_no():
    assert poll_answers("是 | 不是") == ["是", "不是"]
    assert poll_answers("红，蓝、绿/黄,紫") == ["红", "蓝", "绿", "黄", "紫"]
    assert poll_answers("") == ["是 Yes", "不是 No"]


async def test_poll_command_posts_a_native_poll(mock_api):
    bot = make_bot(mock_api.base_url)
    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "gib是不是男同", "是 | 不是", 12, False)
    (content, ephemeral, vote), = interaction.response.sent
    assert vote.question == "gib是不是男同"
    assert [a.text for a in vote.answers] == ["是", "不是"]
    assert vote.duration == timedelta(hours=12) and not vote.multiple and not ephemeral

    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "q", "|".join("abcdefghijk"), 24, False)
    assert interaction.response.sent[0][1] is True  # too many answers: only the asker is told


def test_event_start_reads_dates_and_times():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert event_start("2026-10-10", "20:30", now) == datetime(2026, 10, 10, 20, 30, tzinfo=timezone.utc)
    assert event_start("2026/10/10", "20：30", now) == datetime(2026, 10, 10, 20, 30, tzinfo=timezone.utc)
    assert event_start("10-10", "8:05", now) == datetime(2026, 10, 10, 8, 5, tzinfo=timezone.utc)
    assert event_start("1/5", "20:00", now) == datetime(2027, 1, 5, 20, 0, tzinfo=timezone.utc)  # passed: next year
    assert event_start("next friday", "20:00", now) is None
    assert event_start("10-10", "8pm", now) is None


class FakeGuild:
    def __init__(self, forbidden=False):
        self.forbidden, self.created = forbidden, None

    async def create_scheduled_event(self, **kwargs):
        if self.forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.created = kwargs
        return SimpleNamespace(name=kwargs["name"], location=kwargs["location"], url="https://discord.com/events/1/2")


async def test_event_command_creates_an_external_event(mock_api):
    bot = make_bot(mock_api.base_url)
    guild = FakeGuild()
    interaction = slash_interaction(guild)
    date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    await bot.tree.get_command("event").callback(interaction, "Movie night", date, "20:30", "Voice channel", 2.0, "")
    made = guild.created
    assert made["entity_type"] == discord.EntityType.external and made["location"] == "Voice channel"
    assert made["end_time"] - made["start_time"] == timedelta(hours=2)
    content, ephemeral, _ = interaction.response.sent[0]
    assert "Movie night" in content and "https://discord.com/events/1/2" in content and not ephemeral


async def test_event_command_refuses_bad_or_past_times_and_missing_permission(mock_api):
    bot = make_bot(mock_api.base_url)
    cmd = bot.tree.get_command("event").callback
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    for date, time, guild in (("soon", "20:30", FakeGuild()), ("2020-01-01", "20:30", FakeGuild()),
                              (future, "20:30", FakeGuild(forbidden=True))):
        interaction = slash_interaction(guild)
        await cmd(interaction, "x", date, time, "here", 2.0, "")
        assert interaction.response.sent[0][1] is True and guild.created is None
    assert interaction.response.sent[0][0] == NO_EVENT_PERMISSION
