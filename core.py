"""Model access, per-channel memory and reply splitting. No Discord code here."""
import asyncio
import os
from collections import defaultdict, deque

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

SYSTEM_PROMPT = (
    "You are a friendly assistant in a Discord server. Keep answers short and "
    "conversational unless asked for detail. Several people may talk to you; "
    "each user message starts with their name. Always reply in the language "
    "the user wrote in (English or Chinese). You have no tools: you cannot "
    "read files, run commands or browse the web, so never claim to."
)

OFFLINE_MESSAGE = "Sorry, my brain (LM Studio) is offline right now. Try again in a bit."
ERROR_MESSAGE = "Sorry, something went wrong while thinking. Try again in a bit."
DISCORD_LIMIT = 2000


def estimate_tokens(text):
    # Rough: ~4 chars per token for English, ~1 per CJK character.
    wide = sum(1 for c in text if ord(c) > 0x2E80)
    return wide + (len(text) - wide) // 4 + 1


class ChannelMemory:
    """Last N messages per channel, kept in RAM and trimmed to a token budget."""

    def __init__(self, max_messages=10, max_tokens=3000):
        self.max_tokens = max_tokens
        self._history = defaultdict(lambda: deque(maxlen=max_messages))

    def add(self, channel_id, role, content):
        self._history[channel_id].append({"role": role, "content": content})

    def get(self, channel_id):
        kept, used = [], 0
        for msg in reversed(self._history[channel_id]):
            used += estimate_tokens(msg["content"])
            if used > self.max_tokens:
                break
            kept.append(msg)
        return kept[::-1]

    def reset(self, channel_id):
        self._history.pop(channel_id, None)


class Brain:
    """Talks to LM Studio one request at a time; waiting callers queue up in order."""

    def __init__(self, base_url, model, memory, temperature=0.5, top_p=0.95):
        self.client = AsyncOpenAI(base_url=base_url, api_key="lm-studio", timeout=120)
        self.model = model
        self.memory = memory
        self.temperature = temperature
        self.top_p = top_p
        self._lock = asyncio.Lock()  # asyncio locks wake waiters in FIFO order

    async def ask(self, channel_id, user_name, text):
        user_msg = f"{user_name}: {text}"
        async with self._lock:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages += self.memory.get(channel_id)
            messages.append({"role": "user", "content": user_msg})
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                )
            except (APIConnectionError, APITimeoutError):
                return OFFLINE_MESSAGE
            except APIStatusError:
                return ERROR_MESSAGE
            # Gemma 4 puts its reasoning in reasoning_content; only content is posted.
            reply = (resp.choices[0].message.content or "").strip() or "..."
            self.memory.add(channel_id, "user", user_msg)
            self.memory.add(channel_id, "assistant", reply)
            return reply


def split_message(text, limit=DISCORD_LIMIT):
    """Split text into chunks under Discord's limit, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def load_config():
    def ids(name):
        return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}

    return {
        "token": os.getenv("DISCORD_TOKEN", "").strip(),
        "base_url": os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        "model": os.getenv("LMSTUDIO_MODEL", "gemma4-12b-bionic-v2"),
        "channel_ids": ids("BOT_CHANNEL_ID"),
        "allowed_users": ids("ALLOWED_USER_IDS"),
    }
