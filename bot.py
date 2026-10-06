"""Discord chat bot backed by the local Gemma 4 Bionic model in LM Studio."""
import io
import logging
import os
import random
import re
import sys
from collections import defaultdict, deque

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import (CUSTOM_MAX_CHARS, PERSONAS, Brain, ChannelMemory, apply_extras, extras_note,
                  load_config, load_meanings, split_message)
from imagegen import DrawError, ImageMaker
from search import needs_search, web_search

log = logging.getLogger("bot")

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
STICKER_CHANCE = 0.2  # share of chat replies where the model is offered stickers
CDN = "https://cdn.discordapp.com"
CUSTOM_EMOJI = re.compile(r"<a?:(\w+):(\d+)>")
DRAWING_NOTICE = "🎨 Drawing... I'm offline until it's done. 画画中，画完才回来。"
EXAMPLES_KEPT, EXAMPLE_CHARS = 2, 80  # per emoji/sticker, RAM only


def lower_priority():
    """Run below normal priority so the bot never competes with games or LM Studio."""
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x4000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            os.nice(5)
    except Exception:
        pass


class ChatBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.memory = ChannelMemory()
        self.brain = Brain(config["base_url"], config["model"], self.memory,
                           unfiltered=config.get("unfiltered", False))
        self.tree = app_commands.CommandTree(self)
        self.meanings = {}  # emoji/sticker id -> what Gemma thinks it means, RAM only
        self.examples = defaultdict(lambda: deque(maxlen=EXAMPLES_KEPT))  # id -> recent member uses, RAM only
        self.images = None
        if config.get("image_gen"):
            self.images = ImageMaker(self.brain, config["comfyui_url"], config["sd_checkpoint"],
                                     config["image_size"], config["lmstudio_context"])
        self._add_slash_commands()

    def allowed(self, user):
        allowed = self.config["allowed_users"]
        return not allowed or user.id in allowed

    def drawing(self):
        """While an image is being drawn Gemma is offline and the bot answers nobody."""
        return self.images is not None and self.images.drawing

    async def draw(self, request, notice):
        """Returns (discord.File or None, error text or None). The picture stays in RAM."""
        log.info("Drawing an image")
        try:
            png = await self.images.draw(request, notice)
        except DrawError as e:
            return None, str(e)
        return discord.File(io.BytesIO(png), "image.png"), None

    async def answer(self, channel_id, user_name, text, images=(), search=False, guild=None, stickers=False):
        """Returns (reply text, sticker to send or None). Uses the server's own custom
        emoji, and on some replies (stickers=True) one of its stickers."""
        results = ""
        if text and (search or needs_search(text)):
            log.info("Searching the web for a message in channel %s", channel_id)
            results = await web_search(text)
        emojis, sticker_map, labels, sticker_labels = {}, {}, [], []
        if guild is not None:
            hand = load_meanings()  # read each time so edits work without a restart
            for e in guild.emojis:
                if e.available:
                    emojis[e.name] = str(e)
                    labels.append(self.label(e.id, e.name, hand.get(e.name) or self.meanings.get(e.id)))
            if stickers and random.random() < STICKER_CHANCE:
                for s in guild.stickers:
                    if s.available:
                        sticker_map[s.name] = s
                        sticker_labels.append(self.label(
                            s.id, s.name, hand.get(s.name) or self.meanings.get(s.id) or s.description, s.emoji))
        reply = await self.brain.ask(channel_id, user_name, text, images, results,
                                     extras_note(labels, sticker_labels))
        return apply_extras(reply, emojis, sticker_map)

    def label(self, item_id, name, *hints):
        hints = [h for h in hints if h]
        hints += [f'used like "{ex}"' for ex in self.examples.get(item_id, ())]
        return f"{name} ({'; '.join(hints)})" if hints else name

    def note_usage(self, message):
        """Remember a short line showing how a member used a custom emoji or sticker."""
        used = [int(i) for _, i in CUSTOM_EMOJI.findall(message.content)]
        used += [s.id for s in message.stickers]
        line = CUSTOM_EMOJI.sub(r":\1:", message.content).strip()
        if not used or not CUSTOM_EMOJI.sub("", message.content).strip():
            return  # nothing but the emoji itself: no context to learn from
        line = line[:EXAMPLE_CHARS] + ("..." if len(line) > EXAMPLE_CHARS else "")
        for item_id in set(used):
            self.examples[item_id].append(line)

    async def learn_meanings(self, guild):
        """Show each new emoji/sticker picture to Gemma once so it knows what it means."""
        hand = load_meanings()  # no need to guess these
        items = [(e.id, f"{CDN}/emojis/{e.id}.png")  # .png = first frame of GIFs
                 for e in guild.emojis if e.name not in hand]
        items += [(s.id, s.url) for s in guild.stickers if self.config.get("stickers") and s.name not in hand
                  and s.format in (discord.StickerFormatType.png, discord.StickerFormatType.apng)]
        for item_id, url in items:
            if item_id in self.meanings:
                continue
            try:
                image = await self.http.get_from_cdn(url)
            except discord.HTTPException:
                continue
            meaning = await self.brain.describe(image)
            if meaning:
                self.meanings[item_id] = meaning
        log.info("Know the meaning of %d emoji/stickers in %s", len(self.meanings), guild)

    async def on_guild_emojis_update(self, guild, before, after):
        await self.learn_meanings(guild)

    async def on_guild_stickers_update(self, guild, before, after):
        await self.learn_meanings(guild)

    def _add_slash_commands(self):
        @self.tree.command(name="ask", description="Ask the bot something")
        async def ask(interaction: discord.Interaction, question: str):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            reply, _ = await self.answer(interaction.channel_id, interaction.user.display_name, question,
                                         guild=interaction.guild)
            for chunk in split_message(reply):
                await interaction.followup.send(chunk)

        @self.tree.command(name="search", description="Look something up on the web, then answer")
        async def search(interaction: discord.Interaction, question: str):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            reply, _ = await self.answer(interaction.channel_id, interaction.user.display_name, question, search=True,
                                         guild=interaction.guild)
            for chunk in split_message(reply):
                await interaction.followup.send(chunk)

        @self.tree.command(name="reset", description="Clear the bot's memory of this channel")
        async def reset(interaction: discord.Interaction):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            self.memory.reset(interaction.channel_id)
            await interaction.response.send_message("Memory for this channel cleared.")

        @self.tree.command(name="persona", description="Change the bot's personality in this channel")
        @app_commands.describe(preset="Pick a ready-made personality",
                               custom="Or describe your own, e.g. 'a grumpy cat who loves fish'",
                               character="Or roleplay a known character, e.g. 'Ganyu Genshin Impact'")
        @app_commands.choices(preset=[app_commands.Choice(name=k, value=k) for k in PERSONAS])
        async def persona(interaction: discord.Interaction, preset: app_commands.Choice[str] = None,
                          custom: str = None, character: str = None):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            if character:
                name = character.strip()[:100]
                await interaction.response.defer(thinking=True)
                log.info("Looking up character persona for channel %s", interaction.channel_id)
                results = await web_search(f"{name} character personality speech style quotes")
                text = await self.brain.character_persona(name, results)
                if text is None:
                    await interaction.followup.send(
                        f"Sorry, I couldn't find out enough about **{name}**. "
                        "Try adding the game or show, e.g. 'Ganyu Genshin Impact'.")
                    return
                self.brain.set_persona(interaction.channel_id, text)
                await interaction.followup.send(
                    f"{interaction.user.display_name} switched me to **{name}**. Memory of this channel cleared.")
                return
            if custom:
                text, name = custom.strip()[:CUSTOM_MAX_CHARS], "custom"
            elif preset:
                text, name = PERSONAS[preset.value], preset.value
            else:
                await interaction.response.send_message(
                    "Current personality: " + self.brain.persona(interaction.channel_id)
                    + "\nPresets: " + ", ".join(PERSONAS), ephemeral=True)
                return
            self.brain.set_persona(interaction.channel_id, text)
            await interaction.response.send_message(
                f"{interaction.user.display_name} switched me to **{name}**. Memory of this channel cleared.")

        if self.images is None:
            return

        @self.tree.command(name="draw", description="Draw a picture (Gemma goes offline until it's done)")
        async def draw(interaction: discord.Interaction, request: str):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            file, error = await self.draw(request, lambda: interaction.response.defer(thinking=True))
            if file:
                await interaction.followup.send(f"{interaction.user.display_name}: {request[:200]}", file=file)
            else:
                await interaction.followup.send(error)

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (model: %s)", self.user, self.config["model"])
        if not self.config["channel_ids"]:
            log.info("No BOT_CHANNEL_ID set; answering @mentions and /ask only")
        for channel_id in self.config["channel_ids"]:
            if self.get_channel(channel_id) is None:
                log.warning("BOT_CHANNEL_ID %s not found: wrong ID, or the bot can't view that channel", channel_id)
            else:
                log.info("Answering every message in #%s", self.get_channel(channel_id))
        for guild in self.guilds:
            await self.learn_meanings(guild)

    async def on_message(self, message):
        log.info("Message in channel %s from user %s (%d chars, %d attachments)",
                 message.channel.id, message.author.id, len(message.content), len(message.attachments))
        if message.author.bot or self.drawing():
            return
        self.note_usage(message)
        if not self.allowed(message.author):
            return
        text = message.content.strip()

        if text == "!reset":
            self.memory.reset(message.channel.id)
            await message.reply("Memory for this channel cleared.", mention_author=False)
            return

        mentioned = self.user in message.mentions
        in_bot_channel = message.channel.id in self.config["channel_ids"]
        if not (mentioned or in_bot_channel):
            return

        for tag in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            text = text.replace(tag, "")
        text = text.strip()
        if self.images and text.lower().startswith("!draw "):
            file, error = await self.draw(text[len("!draw "):].strip(),
                                          lambda: message.reply(DRAWING_NOTICE, mention_author=False))
            if file:
                await message.reply(file=file, mention_author=False)
            else:
                await message.reply(error, mention_author=False)
            return
        search = text.lower().startswith("!search ")
        if search:
            text = text[len("!search "):].strip()
        image_files = [a for a in message.attachments
                       if (a.content_type or "").split(";")[0] in IMAGE_TYPES]
        if not (text or image_files):
            return

        async with message.channel.typing():
            # Image bytes stay in RAM for this one request only.
            images = [(await a.read(), a.content_type.split(";")[0]) for a in image_files]
            reply, sticker = await self.answer(message.channel.id, message.author.display_name, text,
                                               images, search, message.guild,
                                               stickers=self.config.get("stickers", False))
        chunks = split_message(reply)
        if chunks:  # empty when the model answered with only a sticker
            await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
        if sticker:
            await message.channel.send(stickers=[sticker])


def main():
    load_dotenv()
    config = load_config()
    if not config["token"]:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token there.")
    lower_priority()
    ChatBot(config).run(config["token"], root_logger=True)


if __name__ == "__main__":
    main()
