"""Drives the bot's message and slash-command handlers with fake Discord objects."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from bot import ChatBot

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

    async def send(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, content, channel, author_id=1, bot=False, mentions=(), attachments=()):
        self.content = content
        self.attachments = list(attachments)
        self.channel = channel
        self.author = SimpleNamespace(id=author_id, bot=bot, display_name=f"user{author_id}")
        self.mentions = list(mentions)
        self.replies = []

    async def reply(self, text, mention_author=True):
        self.replies.append(text)


class FakeAttachment:
    def __init__(self, data, content_type):
        self.data, self.content_type = data, content_type

    async def read(self):
        return self.data


def make_bot(url, allowed=()):
    bot = ChatBot({"token": "x", "base_url": url, "model": "gemma4-12b-bionic-v2",
                   "channel_ids": {BOT_CHANNEL, BOT_CHANNEL_2}, "allowed_users": set(allowed)})
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


async def test_long_reply_is_split(mock_api):
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
    assert names == {"ask", "reset", "search"}

    sent = []
    async def followup_send(text):
        sent.append(text)
    async def defer(thinking=False):
        pass
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=5, display_name="member"), channel_id=CHANNEL,
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
