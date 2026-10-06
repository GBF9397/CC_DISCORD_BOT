"""Web search: when it runs, what the model sees, and that results never enter memory."""
import search
from tests.test_bot import BOT_CHANNEL, FakeAttachment, FakeChannel, FakeMessage, make_bot

RESULTS = [{"title": "Big Match", "href": "https://news.example/match", "body": "Team A won 3-1 last night."}]


def fake_search(monkeypatch, results=RESULTS):
    calls = []
    def _search(query, max_results):
        calls.append(query)
        if isinstance(results, Exception):
            raise results
        return results
    monkeypatch.setattr(search, "_search", _search)
    return calls


def test_needs_search_spots_time_sensitive_questions():
    for q in ["what's the latest iPhone?", "Who won the match today", "BTC price", "今天天气怎么样", "最新的新闻"]:
        assert search.needs_search(q), q
    for q in ["tell me a joke", "explain recursion", "讲个笑话"]:
        assert not search.needs_search(q), q


async def test_time_sensitive_message_gets_results_but_memory_stays_clean(mock_api, monkeypatch):
    calls = fake_search(monkeypatch)
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("who won the match today?", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert calls == ["who won the match today?"]
    sent = mock_api.requests[0]["messages"][-1]["content"]
    assert "Team A won 3-1" in sent and "https://news.example/match" in sent
    assert sent.endswith("user1: who won the match today?")
    assert bot.memory.get(BOT_CHANNEL)[0] == {"role": "user", "content": "user1: who won the match today?"}


async def test_casual_chat_does_not_search(mock_api, monkeypatch):
    calls = fake_search(monkeypatch)
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("tell me a joke", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert calls == [] and msg.replies == ["echo: user1: tell me a joke"]


async def test_bang_search_forces_a_lookup(mock_api, monkeypatch):
    calls = fake_search(monkeypatch)
    bot = make_bot(mock_api.base_url)
    await bot.on_message(FakeMessage("!search genshin version", FakeChannel(BOT_CHANNEL)))
    assert calls == ["genshin version"]
    assert "Team A" in mock_api.requests[0]["messages"][-1]["content"]


async def test_failed_search_still_answers(mock_api, monkeypatch):
    fake_search(monkeypatch, RuntimeError("no network"))
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("latest news?", FakeChannel(BOT_CHANNEL))
    await bot.on_message(msg)
    assert msg.replies == ["echo: user1: latest news?"]


async def test_image_with_time_sensitive_text_gets_both(mock_api, monkeypatch):
    calls = fake_search(monkeypatch)
    bot = make_bot(mock_api.base_url)
    msg = FakeMessage("is this the latest model?", FakeChannel(BOT_CHANNEL),
                      attachments=[FakeAttachment(b"PNGDATA", "image/png")])
    await bot.on_message(msg)
    assert calls == ["is this the latest model?"]
    parts = mock_api.requests[0]["messages"][-1]["content"]
    assert "Team A" in parts[0]["text"] and parts[1]["type"] == "image_url"


def character_interaction(said):
    async def defer(thinking=False):
        pass
    async def followup_send(text):
        said.append(text)
    from types import SimpleNamespace
    return SimpleNamespace(user=SimpleNamespace(id=5, display_name="member"), channel_id=BOT_CHANNEL,
                           response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send))


async def test_persona_character_searches_and_roleplays(mock_api, monkeypatch):
    calls = fake_search(monkeypatch, [{"title": "Ganyu", "href": "https://wiki.example/ganyu",
                                       "body": "Ganyu is a gentle, hardworking adeptus secretary."}])
    mock_api.reply = "Gentle half-qilin secretary of Liyue. Polite, shy, overworked."
    bot = make_bot(mock_api.base_url)
    await bot.on_message(FakeMessage("hi", FakeChannel(BOT_CHANNEL)))
    said = []
    await bot.tree.get_command("persona").callback(character_interaction(said), None, None, "Ganyu Genshin Impact")

    assert calls == ["Ganyu Genshin Impact character personality speech style quotes"]
    summary_request = mock_api.requests[-1]["messages"]
    assert len(summary_request) == 1 and "adeptus secretary" in summary_request[0]["content"]
    persona = bot.brain.persona(BOT_CHANNEL)
    assert persona.startswith("Ganyu Genshin Impact. Stay fully in character") and "half-qilin" in persona
    assert said == ["member switched me to **Ganyu Genshin Impact**. Memory of this channel cleared."]
    assert bot.memory.get(BOT_CHANNEL) == []

    mock_api.reply = None
    await bot.on_message(FakeMessage("hello", FakeChannel(BOT_CHANNEL)))
    assert "half-qilin" in mock_api.requests[-1]["messages"][0]["content"]


async def test_persona_character_unknown_keeps_old_persona(mock_api, monkeypatch):
    fake_search(monkeypatch, [])
    mock_api.reply = "UNKNOWN"
    bot = make_bot(mock_api.base_url)
    before = bot.brain.persona(BOT_CHANNEL)
    said = []
    await bot.tree.get_command("persona").callback(character_interaction(said), None, None, "Zzxq")
    assert bot.brain.persona(BOT_CHANNEL) == before
    assert "couldn't find out enough about **Zzxq**" in said[0]
