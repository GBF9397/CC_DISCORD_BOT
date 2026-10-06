"""Model access, per-channel memory and reply splitting. No Discord code here."""
import asyncio
import base64
import os
import re
from collections import defaultdict, deque
from datetime import date

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

SYSTEM_PROMPT = (
    "You are a member of a Discord server chatting with friends, not a customer-service "
    "assistant. Several people may talk to you; each user message starts with the "
    "speaker's display name. Call people by their name now and then, remember who said "
    "what, and react to them personally. Have opinions, be specific, joke around; avoid "
    "bland, generic or overly polite answers and never lecture. Keep replies short and "
    "chatty unless asked for detail. Always reply in the language the user wrote in "
    "(English or Chinese). You have no tools: you cannot read files, run commands or "
    "browse the web, so never claim to. Never start a reply with your own name.\n\n"
    "Your personality: "
)

SEARCH_NOTE = (
    "[Today is {today}. The bot already searched the web for the message below, so "
    "you can answer with this live information; don't say you can't browse. Use these "
    "results for anything current and prefer them over what you remember; name the "
    "source site when it helps. If they don't answer it, say so.]\n\n{results}\n\n[Message]\n"
)

# Preset personalities members can switch between with /persona.
PERSONAS = {
    "buddy": "a witty, slightly chaotic best friend. You tease people playfully, hype them "
             "up, use the odd emoji, and always have a hot take.",
    "tsundere": "a tsundere anime girl. You act annoyed and say things like 'it's not like "
                "I wanted to help you, baka!', but you secretly care and always help anyway.",
    "wuxia": "an ancient wuxia martial-arts master (武侠宗师). You speak dramatically about "
             "cultivation, sects and inner energy, call people 'young hero' (少侠), and turn "
             "every everyday topic into a jianghu legend.",
    "pirate": "a loud, cheerful pirate captain. Arr! You talk like a pirate, call people "
              "matey, and relate everything to treasure, rum and the open sea.",
    "roast": "a savage stand-up comedian who roasts whoever talks to you, then still answers. "
             "Keep it friendly banter: never truly hurtful, hateful or about real sensitive traits.",
}
DEFAULT_PERSONA = "buddy"
CUSTOM_MAX_CHARS = 300

# The server's own custom emoji (incl. animated GIF ones) and stickers, offered to the model.
EXTRAS_NOTE = (
    "\n\nThis server has custom emoji you may drop into a reply now and then by writing "
    "them as :name:. Available: {emoji}."
)
STICKER_NOTE = (
    "\n\nThis time you may also send ONE of the server's stickers if it really fits the "
    "mood: put [sticker: name] at the very end of your reply. Usually don't. Available: {stickers}."
)
STICKER_TAG = re.compile(r"\s*\[sticker:\s*([^\]]+)\]", re.IGNORECASE)
EMOJI_TAG = re.compile(r"(?<![<\w]):([\w~-]{2,32}):")
MAX_EXTRAS = 50  # names listed per kind, to keep the prompt small


def extras_note(emoji_names, sticker_names):
    """System-prompt text listing what the model may use; empty when the server has none."""
    note = ""
    if emoji_names:
        note += EXTRAS_NOTE.format(emoji=", ".join(list(emoji_names)[:MAX_EXTRAS]))
    if sticker_names:
        note += STICKER_NOTE.format(stickers=", ".join(list(sticker_names)[:MAX_EXTRAS]))
    return note


def apply_extras(reply, emojis, stickers):
    """emojis: name -> Discord emoji text like <:name:id>; stickers: name -> sticker.
    Turns :name: into real emoji and pulls out one [sticker: name] tag.
    Returns (text, sticker or None). Unknown names are left as plain text or dropped."""
    sticker = None
    match = STICKER_TAG.search(reply)
    if match:
        sticker = stickers.get(match.group(1).strip().strip(":"))
    reply = STICKER_TAG.sub("", reply).strip()
    reply = EMOJI_TAG.sub(lambda m: emojis.get(m.group(1), m.group(0)), reply)
    return (reply if reply or sticker else "..."), sticker


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

    def __init__(self, base_url, model, memory, temperature=0.9, top_p=0.95):
        self.client = AsyncOpenAI(base_url=base_url, api_key="lm-studio", timeout=120)
        self.model = model
        self.memory = memory
        self.temperature = temperature
        self.top_p = top_p
        self._lock = asyncio.Lock()  # asyncio locks wake waiters in FIFO order
        self._personas = {}  # channel_id -> personality text, RAM only

    def persona(self, channel_id):
        return self._personas.get(channel_id, PERSONAS[DEFAULT_PERSONA])

    def set_persona(self, channel_id, text):
        """Switch this channel's personality and forget the chat, so the old voice doesn't linger."""
        self._personas[channel_id] = text
        self.memory.reset(channel_id)

    async def ask(self, channel_id, user_name, text, images=(), search_results="", extras=""):
        """images: (bytes, mime type) pairs, sent to the model and never stored.
        search_results: web results sent to the model once, never stored.
        extras: extras_note() text about the server's emoji and stickers."""
        user_msg = remembered = f"{user_name}: {text}"
        if search_results:
            user_msg = SEARCH_NOTE.format(today=f"{date.today():%A %d %B %Y}", results=search_results) + user_msg
        if images:
            remembered += f" [sent {len(images)} image(s)]"
            user_msg = [{"type": "text", "text": user_msg}] + [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
                for data, mime in images
            ]
        async with self._lock:
            messages = [{"role": "system", "content": SYSTEM_PROMPT + self.persona(channel_id) + extras}]
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
            self.memory.add(channel_id, "user", remembered)
            # Forget sticker tags so the model doesn't copy them into every reply.
            self.memory.add(channel_id, "assistant", STICKER_TAG.sub("", reply).strip() or "...")
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
