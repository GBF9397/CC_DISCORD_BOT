"""Drives the bot's message and slash-command handlers with fake Discord objects."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone

import discord

from bot import NO_EVENT_PERMISSION, POLL_VOTERS, ChatBot, event_start, poll_answers

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

    async def reply(self, text=None, mention_author=True, file=None, view=None):
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
    assert names == {"ask", "reset", "search", "persona", "poll", "event", "status", "comment"}

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
    assert parts[0]["text"].startswith("user1: what is this?\n\n[Length limit")
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


async def test_comment_messages_get_no_reply_and_are_not_remembered(mock_api):
    bot = make_bot(mock_api.base_url)
    ch = FakeChannel(BOT_CHANNEL)
    for text in ("/comment 你们晚上打不打", "/Comment lol", f"<@{BOT_ID}> /comment hi", "／comment 好", "!comment 好", "！comment 好"):
        msg = FakeMessage(text, ch, mentions=[bot.user])
        await bot.on_message(msg)
        assert msg.replies == []
    assert mock_api.requests == [] and bot.memory.get(BOT_CHANNEL) == []
    msg = FakeMessage("/commentary please", ch)
    await bot.on_message(msg)
    assert msg.replies  # only the /comment word itself is skipped


class FakeHook:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append((text, kwargs["username"]))


async def test_comment_is_reposted_without_the_command(mock_api):
    bot = make_bot(mock_api.base_url)
    hook = FakeHook()
    allowed = SimpleNamespace(manage_webhooks=True, manage_messages=True)
    ch = FakeChannel(BOT_CHANNEL)
    ch.guild = SimpleNamespace(me=None)
    ch.permissions_for = lambda member: allowed
    ch.webhooks = lambda: _return([])
    ch.create_webhook = lambda name: _return(hook)
    msg = FakeMessage("！comment 你们晚上打不打", ch, guild=ch.guild)
    msg.author.display_avatar = SimpleNamespace(url="https://cdn/a.png")
    deleted = []
    msg.delete = lambda: _return(deleted.append(True))
    await bot.on_message(msg)
    assert hook.sent == [("你们晚上打不打", "user1")] and deleted and msg.replies == []
    assert mock_api.requests == [] and bot.memory.get(BOT_CHANNEL) == []

    allowed.manage_messages = False  # can't delete it: the message stays as typed
    deleted.clear()
    await bot.on_message(msg)
    assert len(hook.sent) == 1 and not deleted


async def _return(value):
    return value


async def test_character_persona_searches_for_fact_questions(mock_api, monkeypatch):
    import bot as bot_module
    queries = []

    async def fake_search(query):
        queries.append(query)
        return "Opera Epiclese"
    monkeypatch.setattr(bot_module, "web_search", fake_search)
    bot = make_bot(mock_api.base_url)
    bot.brain.set_persona(BOT_CHANNEL, "Furina", character="Furina Genshin")
    ch = FakeChannel(BOT_CHANNEL)
    await bot.on_message(FakeMessage("what is the opera house called?", ch))
    await bot.on_message(FakeMessage("haha nice", ch))
    assert queries == ["Furina Genshin what is the opera house called?"]
    assert "Opera Epiclese" in mock_api.requests[0]["messages"][-1]["content"]
    assert "Opera Epiclese" not in mock_api.requests[1]["messages"][-1]["content"]


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

    async def send_message(self, content=None, ephemeral=False, poll=None, view=None):
        self.sent.append((content, ephemeral, poll))


def slash_interaction(guild=None):
    response = FakeResponse()

    async def defer(thinking=False):
        pass

    async def followup(content=None, view=None):
        response.sent.append((content, False, None))
    response.defer = defer
    return SimpleNamespace(user=SimpleNamespace(id=5, display_name="member"), channel_id=CHANNEL,
                           guild=guild, response=response, followup=SimpleNamespace(send=followup))


def test_poll_answers_split_and_default_to_yes_no():
    assert poll_answers("是 | 不是") == ["是", "不是"]
    assert poll_answers("红，蓝、绿/黄,紫") == ["红", "蓝", "绿", "黄", "紫"]
    assert poll_answers("ey gib｜ep hung／xuan") == ["ey gib", "ep hung", "xuan"]
    assert poll_answers("") == ["是 Yes", "不是 No"]


async def test_poll_command_posts_a_native_poll(mock_api):
    bot = make_bot(mock_api.base_url)
    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "gib是不是男同", "是 | 不是", "", 12, False)
    (content, ephemeral, vote), = interaction.response.sent
    assert vote.question == "gib是不是男同"
    assert [a.text for a in vote.answers] == ["是", "不是"]
    assert vote.duration == timedelta(hours=12) and not vote.multiple and not ephemeral and content is None

    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "q", "|".join("abcdefghijk"), "", 24, False)
    assert interaction.response.sent[0][1] is True  # too many answers: only the asker is told


def test_event_start_reads_dates_and_times():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert event_start("2026-10-10", "20:30", now) == datetime(2026, 10, 10, 20, 30, tzinfo=timezone.utc)
    assert event_start("2026/10/10", "20：30", now) == datetime(2026, 10, 10, 20, 30, tzinfo=timezone.utc)
    assert event_start("10-10", "8:05", now) == datetime(2026, 10, 10, 8, 5, tzinfo=timezone.utc)
    assert event_start("1/5", "20:00", now) == datetime(2027, 1, 5, 20, 0, tzinfo=timezone.utc)  # passed: next year
    assert event_start("next friday", "20:00", now) is None
    for text in ("8:30pm", "8:30 PM", "晚上8:30", "下午8点半"[:-1] + "30", "20:30"):
        assert event_start("10-10", text, now).hour == 20, text
    assert event_start("10-10", "8pm", now).strftime("%H:%M") == "20:00"
    assert event_start("10-10", "12am", now).hour == 0 and event_start("10-10", "12pm", now).hour == 12
    assert event_start("10-10", "上午9点", now).strftime("%H:%M") == "09:00"
    assert event_start("10-10", "8", now) is None and event_start("10-10", "13pm", now) is None


class FakeGuild:
    id = 1

    def __init__(self, forbidden=False):
        self.forbidden, self.created = forbidden, None

    async def create_scheduled_event(self, **kwargs):
        if self.forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.created = kwargs
        return SimpleNamespace(id=7, name=kwargs["name"], location=kwargs["location"], url="https://discord.com/events/1/2")


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
        assert guild.created is None
    assert interaction.response.sent[0][0] == NO_EVENT_PERMISSION


async def test_poll_with_members_names_them_and_ends_once_they_all_voted(mock_api):
    bot = make_bot(mock_api.base_url)
    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "q", "", "<@11> <@!12> <@11>", None, False)
    content, _, vote = interaction.response.sent[0]
    assert content == POLL_VOTERS + "<@11> <@12>" and vote.duration == timedelta(hours=168)

    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "q", "", "Daddy宏", 768, False)
    assert interaction.response.sent[0][1] is True  # names without @ are refused, privately

    votes = {"是 Yes": [11], "不是 No": [99]}
    ended = []

    class Answer:
        def __init__(self, ids):
            self.ids = ids

        async def voters(self):
            for i in self.ids:
                yield SimpleNamespace(id=i)

    message = SimpleNamespace(id=1, author=SimpleNamespace(id=BOT_ID), content=content,
                              poll=SimpleNamespace(answers=[Answer(v) for v in votes.values()],
                                                   is_finalised=lambda: False))
    async def end_poll():
        ended.append(True)
    async def fetch_message(mid):
        return message
    message.end_poll = end_poll
    bot.get_channel = lambda cid: SimpleNamespace(fetch_message=fetch_message)
    payload = SimpleNamespace(channel_id=CHANNEL, message_id=1)
    await bot.on_raw_poll_vote_add(payload)
    assert not ended  # member 12 hasn't voted; votes from others don't count
    message.poll.answers[1].ids.append(12)
    await bot.on_raw_poll_vote_add(payload)
    assert ended == [True]


async def test_poll_without_members_defaults_to_one_day(mock_api):
    bot = make_bot(mock_api.base_url)
    interaction = slash_interaction()
    await bot.tree.get_command("poll").callback(interaction, "q", "", "", None, False)
    assert interaction.response.sent[0][2].duration == timedelta(hours=24)


async def test_bang_event_creates_an_event_from_one_line(mock_api):
    bot = make_bot(mock_api.base_url)
    guild = FakeGuild()
    date = (datetime.now() + timedelta(days=3)).strftime("%m-%d")
    msg = FakeMessage(f"/event 电影夜 | {date} | 8:30pm | 语音频道 | 3", FakeChannel(CHANNEL), guild=guild)
    await bot.on_message(msg)
    assert guild.created["name"] == "电影夜" and guild.created["location"] == "语音频道"
    assert guild.created["start_time"].strftime("%H:%M") == "20:30"
    assert guild.created["end_time"] - guild.created["start_time"] == timedelta(hours=3)
    assert "https://discord.com/events/1/2" in msg.replies[0]

    msg = FakeMessage("！event 电影夜 10-10 8:30pm", FakeChannel(CHANNEL), guild=FakeGuild())
    await bot.on_message(msg)
    assert msg.replies[0].startswith("Write it as")


def test_text_command_accepts_bang_fullwidth_and_pasted_slash():
    from bot import text_command
    assert text_command("!draw a cat") == ("!draw", "a cat")
    assert text_command("！Search 天气") == ("!search", "天气")
    assert text_command("/refine make it night") == ("!refine", "make it night")
    assert text_command("/shrug hello") == (None, "/shrug hello")
    assert text_command("hello") == (None, "hello")


async def test_text_commands_work_outside_bot_channels(mock_api):
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("／ask hi", FakeChannel(CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == []  # unknown prefix: ignored outside bot channels
    msg = FakeMessage("/ask hi there", FakeChannel(CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: hi there"]
    msg = FakeMessage("！reset", FakeChannel(CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["Memory for this channel cleared."]


async def test_bang_poll_posts_a_poll_from_one_line(mock_api):
    bot = make_bot(mock_api.base_url)
    sent = []
    ch = FakeChannel(CHANNEL)

    async def send(content=None, poll=None, view=None):
        sent.append((content, poll))
    ch.send = send
    await bot.on_message(FakeMessage("!poll 今晚吃什么 | 炒饭，煎蛋 | <@11>", ch))
    content, vote = sent[0]
    assert vote.question == "今晚吃什么" and [a.text for a in vote.answers] == ["炒饭", "煎蛋"]
    assert content == POLL_VOTERS + "<@11>"
    msg = FakeMessage("!poll", FakeChannel(CHANNEL))
    await bot.on_message(msg)
    assert msg.replies[0].startswith("Write it as")


async def test_only_the_creator_can_end_a_poll_and_gets_the_counts():
    from bot import EndPollButton
    ended, sent = [], []
    answers = [SimpleNamespace(text="炒饭", vote_count=2), SimpleNamespace(text="煎蛋", vote_count=1)]
    poll = SimpleNamespace(question="今晚吃什么", answers=answers, total_votes=3, is_finalised=lambda: False)

    async def end_poll():
        ended.append(True)
    response = FakeResponse()

    async def edit_message(view=None):
        pass
    response.edit_message = edit_message

    async def followup(text):
        sent.append(text)
    def click(user_id):
        return SimpleNamespace(user=SimpleNamespace(id=user_id, display_name="Ep"), response=response,
                               message=SimpleNamespace(poll=poll, end_poll=end_poll),
                               followup=SimpleNamespace(send=followup))

    button = EndPollButton(5)
    await button.callback(click(6))
    assert not ended and response.sent[0][0].startswith("Only the person")
    await button.callback(click(5))
    assert ended and "炒饭: 2" in sent[0] and "煎蛋: 1" in sent[0]


async def test_end_event_answers_the_click_before_cancelling():
    from bot import EndEventButton
    steps = []
    event = SimpleNamespace(status=discord.EventStatus.scheduled)

    async def cancel():
        steps.append("cancel")
    event.cancel = cancel

    async def fetch(event_id):
        steps.append("fetch")
        return event

    async def defer():
        steps.append("defer")

    async def edit_original_response(view=None):
        steps.append("edit")

    async def followup(text, ephemeral=False):
        steps.append(text)
    interaction = SimpleNamespace(user=SimpleNamespace(id=5, display_name="Ep"),
                                  guild=SimpleNamespace(fetch_scheduled_event=fetch),
                                  response=SimpleNamespace(defer=defer),
                                  edit_original_response=edit_original_response,
                                  followup=SimpleNamespace(send=followup))
    await EndEventButton(5, 9).callback(interaction)
    # Discord gives a click only 3 s, so the bot answers before it fetches and cancels the event.
    assert steps[:4] == ["defer", "fetch", "cancel", "edit"] and "ended the event" in steps[4]


async def test_end_event_says_when_discord_refuses():
    from bot import EndEventButton
    sent = []

    async def fetch(event_id):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": ""})

    async def defer():
        pass

    async def followup(text, ephemeral=False):
        sent.append((text, ephemeral))
    interaction = SimpleNamespace(user=SimpleNamespace(id=5, display_name="Ep"),
                                  guild=SimpleNamespace(fetch_scheduled_event=fetch),
                                  response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup))
    await EndEventButton(5, 9).callback(interaction)
    assert "50013" in sent[0][0] and sent[0][1]
