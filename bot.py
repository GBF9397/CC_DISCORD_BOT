"""Discord chat bot backed by the local Gemma 4 Bionic model in LM Studio."""
import logging
import os
import sys

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import Brain, ChannelMemory, load_config, split_message

log = logging.getLogger("bot")


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
        self.brain = Brain(config["base_url"], config["model"], self.memory)
        self.tree = app_commands.CommandTree(self)
        self._add_slash_commands()

    def allowed(self, user):
        allowed = self.config["allowed_users"]
        return not allowed or user.id in allowed

    def _add_slash_commands(self):
        @self.tree.command(name="ask", description="Ask the bot something")
        async def ask(interaction: discord.Interaction, question: str):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            reply = await self.brain.ask(interaction.channel_id, interaction.user.display_name, question)
            for chunk in split_message(reply):
                await interaction.followup.send(chunk)

        @self.tree.command(name="reset", description="Clear the bot's memory of this channel")
        async def reset(interaction: discord.Interaction):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            self.memory.reset(interaction.channel_id)
            await interaction.response.send_message("Memory for this channel cleared.")

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (model: %s)", self.user, self.config["model"])
        channel_id = self.config["channel_id"]
        if channel_id is None:
            log.info("No BOT_CHANNEL_ID set; answering @mentions and /ask only")
        elif self.get_channel(channel_id) is None:
            log.warning("BOT_CHANNEL_ID %s not found: wrong ID, or the bot can't view that channel", channel_id)
        else:
            log.info("Answering every message in #%s", self.get_channel(channel_id))

    async def on_message(self, message):
        log.info("Message in channel %s from user %s (%d chars)",
                 message.channel.id, message.author.id, len(message.content))
        if message.author.bot or not self.allowed(message.author):
            return
        text = message.content.strip()

        if text == "!reset":
            self.memory.reset(message.channel.id)
            await message.reply("Memory for this channel cleared.", mention_author=False)
            return

        mentioned = self.user in message.mentions
        in_bot_channel = message.channel.id == self.config["channel_id"]
        if not (mentioned or in_bot_channel):
            return

        for tag in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            text = text.replace(tag, "")
        text = text.strip()
        if not text:
            return

        async with message.channel.typing():
            reply = await self.brain.ask(message.channel.id, message.author.display_name, text)
        chunks = split_message(reply)
        await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


def main():
    load_dotenv()
    config = load_config()
    if not config["token"]:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token there.")
    lower_priority()
    ChatBot(config).run(config["token"])


if __name__ == "__main__":
    main()
