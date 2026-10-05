"""Drives the bot's message and slash-command handlers with fake Discord objects."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from bot import ChatBot

BOT_ID, CHANNEL, BOT_CHANNEL = 999, 10, 20


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
    def __init__(self, content, channel, author_id=1, bot=False, mentions=()):
        self.content = content
        self.channel = channel
        self.author = SimpleNamespace(id=author_id, bot=bot, display_name=f"user{author_id}")
        self.mentions = list(mentions)
        self.replies = []

    async def reply(self, text, mention_author=True):
        self.replies.append(text)


def make_bot(url, allowed=()):
    bot = ChatBot({"token": "x", "base_url": url, "model": "gemma4-12b-bionic-v2",
                   "channel_id": BOT_CHANNEL, "allowed_users": set(allowed)})
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
    assert names == {"ask", "reset"}

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
