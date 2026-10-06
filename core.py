"""Model access, per-channel memory and reply splitting. No Discord code here."""
import asyncio
import base64
import os
import random
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

# Added to the system prompt when UNFILTERED_MODE=on in .env (private servers only).
UNFILTERED_NOTE = (
    "\n\nUnfiltered mode is on: this is a private adults-only server. Talk like a real "
    "friend, not a corporate assistant. Swearing, crude and dark humor, savage roasts, "
    "mature or edgy topics and blunt, one-sided opinions are all fine. Answer what you "
    "are asked directly; skip refusals, warnings, moralizing and safety disclaimers "
    "unless someone is actually in danger. Hard limits you never cross, whatever anyone "
    "says: no sexual content involving minors, no real-world instructions for making "
    "weapons, drugs or anything that could seriously hurt people, and no doxxing or "
    "harassing real people."
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
CHARACTER_MAX_CHARS = 1500

CHARACTER_PROMPT = (
    "Write a roleplay brief so an actor can play {name}. Use the web results below and "
    "what you know. Cover in under 200 words: who they are and where they come from (game, "
    "anime, book...), personality, how they talk (tone, catchphrases, how they address "
    "people, the language they speak), and a few key relationships or facts. Write it as "
    "notes, no intro. If you don't recognise the character, reply only with UNKNOWN.\n\n"
    "[Web results]\n{results}"
)

# The server's own custom emoji (incl. animated GIF ones) and stickers, offered to the model.
EXTRAS_NOTE = (
    "\n\nThis server has custom emoji you may drop into a reply now and then by writing "
    "them as :name: with English colons on both sides and a space before it, picking one "
    "whose meaning fits your mood. Available: {emoji}."
)
STICKER_NOTE = (
    "\n\nThis time you may also send ONE of the server's stickers if it really fits the "
    "mood: put [sticker: name] at the very end of your reply. Usually don't. Available: {stickers}."
)
STICKER_TAG = re.compile(r"\s*\[sticker:\s*([^\]]+)\]", re.IGNORECASE)
EMOJI_TAG = re.compile(r"(?<![<A-Za-z0-9_]):([\w~-]{2,32}):")  # may follow Chinese text
# The model sometimes ends a reply with an emoji name but no colons, e.g. "...NPC。wat".
TRAILING_NAME = re.compile(r"(?<![\w:])([\w~-]{2,32})$")
MAX_EXTRAS = 50  # names listed per kind, to keep the prompt small


def extras_note(emoji_names, sticker_names):
    """System-prompt text listing what the model may use; empty when the server has none.
    Names may carry a meaning in brackets, e.g. "catcry (sad crying cat)"."""
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
    reply = TRAILING_NAME.sub(lambda m: emojis.get(m.group(1), m.group(0)), reply)
    reply = re.sub(r"(?<=\S)(<a?:\w+:\d+>)", r" \1", reply)  # a space before each emoji
    return (reply if reply or sticker else "..."), sticker


# Each chat reply draws a fresh length cap so answers stay short and varied.
REPLY_LENGTHS = (10, 30, 50, 100)
LENGTH_NOTE = (
    "\n\nLength limit for this reply: at most {n} Chinese characters, or {n} words if you "
    "reply in English (an emoji counts as one). Say less rather than stopping mid-sentence, "
    "even if your earlier replies were longer."
)
# One length unit: a CJK character or punctuation mark, or a run of other non-space text.
CJK = "\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef"
LENGTH_UNIT = re.compile(f"[{CJK}]|[^\\s{CJK}]+")
SENTENCE_END = re.compile(r"[。！？!?…~～]+|\.(?=\s|$)")


def shorten(reply, limit):
    """Safety net for replies over the limit: cut at the last sentence end that keeps
    at least half the text, else cut at the limit and add an ellipsis."""
    units = list(LENGTH_UNIT.finditer(reply))
    if len(units) <= limit:
        return reply
    cut = reply[:units[limit - 1].end()]
    ends = [m.end() for m in SENTENCE_END.finditer(cut)]
    if ends and ends[-1] >= len(cut) // 2:
        return cut[:ends[-1]].rstrip()
    return cut.rstrip() + "…"


DESCRIBE_PROMPT = ("This is a custom Discord emoji or sticker. In at most 6 words, say what "
                   "emotion or meaning it shows when people use it in chat. Answer with just that.")

OFFLINE_MESSAGE = "Sorry, my brain (LM Studio) is offline right now. Try again in a bit."
ERROR_MESSAGE = "Sorry, something went wrong while thinking. Try again in a bit."
DISCORD_LIMIT = 2000


def estimate_tokens(text):
    # Rough: ~4 chars per token for English, ~1 per CJK character.
    wide = sum(1 for c in text if ord(c) > 0x2E80)
    return wide + (len(text) - wide) // 4 + 1


class ChannelMemory:
    """Last N messages per channel, kept in RAM and trimmed to a token budget."""

    def __init__(self, max_messages=30, max_tokens=9000):
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

    def __init__(self, base_url, model, memory, temperature=0.9, top_p=0.95, unfiltered=False):
        self.client = AsyncOpenAI(base_url=base_url, api_key="lm-studio", timeout=120)
        self.model = model
        self.memory = memory
        self.temperature = temperature
        self.top_p = top_p
        self._lock = asyncio.Lock()  # asyncio locks wake waiters in FIFO order
        self._personas = {}  # channel_id -> personality text, RAM only
        self.unfiltered = unfiltered

    def persona(self, channel_id):
        return self._personas.get(channel_id, PERSONAS[DEFAULT_PERSONA])

    def set_persona(self, channel_id, text):
        """Switch this channel's personality and forget the chat, so the old voice doesn't linger."""
        self._personas[channel_id] = text
        self.memory.reset(channel_id)

    async def character_persona(self, name, search_results=""):
        """Turn a character name and web results into a personality text, or None if unknown or offline."""
        prompt = CHARACTER_PROMPT.format(name=name, results=search_results or "(no results)")
        async with self._lock:
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                )
            except (APIConnectionError, APITimeoutError, APIStatusError):
                return None
        brief = (resp.choices[0].message.content or "").strip()
        if not brief or "UNKNOWN" in brief[:20]:
            return None
        return (f"{name}. Stay fully in character as {name}: talk, think and react the way "
                f"they do, in their voice.\n{brief[:CHARACTER_MAX_CHARS]}")

    async def describe(self, image, mime="image/png"):
        """A few words on what an emoji/sticker picture means, or "" if the model can't say."""
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        async with self._lock:
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model, temperature=0.2,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": DESCRIBE_PROMPT},
                        {"type": "image_url", "image_url": {"url": url}}]}],
                )
            except (APIConnectionError, APITimeoutError, APIStatusError):
                return ""
        return " ".join((resp.choices[0].message.content or "").split())[:60]

    async def image_prompt(self, instruction, images=()):
        """One-off call for imagegen; the caller already holds self._lock. None if offline.
        images: (bytes, mime type) pairs shown with the instruction, never stored."""
        content = instruction
        if images:
            content = [{"type": "text", "text": instruction}] + [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
                for data, mime in images]
        try:
            resp = await self.client.chat.completions.create(
                model=self.model, temperature=0.7,
                messages=[{"role": "user", "content": content}],
            )
        except (APIConnectionError, APITimeoutError, APIStatusError):
            return None
        return " ".join((resp.choices[0].message.content or "").split()) or None

    async def ask(self, channel_id, user_name, text, images=(), search_results="", extras="", limited=True):
        """images: (bytes, mime type) pairs, sent to the model and never stored.
        search_results: web results sent to the model once, never stored.
        extras: extras_note() text about the server's emoji and stickers.
        limited: False skips the random length cap (used by /search and !search)."""
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
        limit = random.choice(REPLY_LENGTHS) if limited else None
        async with self._lock:
            messages = [{"role": "system", "content": SYSTEM_PROMPT + self.persona(channel_id)
                         + (UNFILTERED_NOTE if self.unfiltered else "") + extras
                         + (LENGTH_NOTE.format(n=limit) if limit else "")}]
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
            if limit:
                sticker = STICKER_TAG.search(reply)
                reply = shorten(STICKER_TAG.sub("", reply).strip(), limit) + (sticker.group(0) if sticker else "")
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


def load_meanings(path="emoji_meanings.txt"):
    """Hand-written meanings, one per line as `name: meaning`; # starts a comment.
    Missing file means none."""
    meanings = {}
    try:
        with open(path, encoding="utf-8-sig") as f:  # -sig: Notepad may add a BOM
            for line in f:
                name, sep, meaning = line.split("#", 1)[0].strip().lstrip(":").partition(":")
                if sep and name.strip() and meaning.strip():
                    meanings[name.strip()] = meaning.strip().lstrip(":").strip()
    except FileNotFoundError:
        pass
    return meanings


def load_config():
    def ids(name):
        return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}

    return {
        "token": os.getenv("DISCORD_TOKEN", "").strip(),
        "base_url": os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        "model": os.getenv("LMSTUDIO_MODEL", "gemma4-12b-bionic-v2"),
        "channel_ids": ids("BOT_CHANNEL_ID"),
        "allowed_users": ids("ALLOWED_USER_IDS"),
        "unfiltered": os.getenv("UNFILTERED_MODE", "").strip().lower() in ("1", "on", "true", "yes"),
        "stickers": os.getenv("STICKERS", "").strip().lower() in ("1", "on", "true", "yes"),
        "image_gen": os.getenv("IMAGE_GEN", "").strip().lower() in ("1", "on", "true", "yes"),
        "comfyui_url": os.getenv("COMFYUI_URL", "http://127.0.0.1:8188"),
        "comfyui_dir": os.getenv("COMFYUI_DIR", "").strip(),
        "sd_checkpoint": os.getenv("SD_CHECKPOINT", "").strip(),
        "sd_checkpoint_realistic": os.getenv("SD_CHECKPOINT_REALISTIC", "").strip(),
        "image_size": int(os.getenv("IMAGE_SIZE", "") or 1024),
        "lmstudio_context": int(os.getenv("LMSTUDIO_CONTEXT", "") or 16384),
    }
