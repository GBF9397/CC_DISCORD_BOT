"""Discord chat bot backed by the local Gemma 4 Bionic model in LM Studio."""
import asyncio
import io
import logging
import os
import random
import re
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import (CUSTOM_MAX_CHARS, PERSONAS, Brain, ChannelMemory, apply_extras, extras_note,
                  load_config, load_meanings, split_message)
import monitor
from imagegen import DrawError, ImageMaker
from search import needs_search, web_search

log = logging.getLogger("bot")

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
STICKER_CHANCE = 0.2  # share of chat replies where the model is offered stickers
CDN = "https://cdn.discordapp.com"
CUSTOM_EMOJI = re.compile(r"<a?:(\w+):(\d+)>")
DRAWING_NOTICE = "🎨 Drawing... I'm offline until it's done. 画画中，画完才回来。"
DONE_NOTICE = "🎨 Done! 画好了！"
# Added to the system prompt when drawing is on, so Gemma stops saying it can't make pictures.
DRAW_HINT = ("\n\nThis bot can draw pictures, but not in a normal reply: if someone asks you to draw "
             "or make a picture, tell them to send !draw followed by what they want (add realistic "
             "first for a photo look), or use /draw.")
QUEUED_NOTICE = ("🎨 Queued, {ahead} picture(s) ahead of you. I'll chat again once every picture is done. "
                 "已排队，前面还有 {ahead} 张，全部画完我才回来聊天。")
NO_PICTURE = "Attach the picture to edit. 请附上要修改的图片。"
NOTHING_TO_RECALL = "You have no pictures here yet. 你在这个频道还没有图。"
RECALL_LIST = ("Your last pictures here, 1 = oldest. Send !recall <number> (or /recall) to go back to one; "
               "your next /refine builds on it and the others stay. Now on: {base}. "
               "你最近的图，1 是最早的。用 !recall 编号 回到那张，之后 /refine 从它改，其他的都保留。现在在第 {base} 张。")
RECALLED = ("Back to picture {number}. Your next /refine builds on it; the others are kept. "
            "已回到第 {number} 张，下次 /refine 从这张改，其他的都保留。")
ALREADY_QUEUED = ("You already have a picture waiting. Ask again once it's done. "
                  "你已经有一张在排队了，画完才能再点。")
STATUS_EVERY, STATUS_UPDATES = 2.5, 20  # /status refreshes about every 3 s for a minute
EXAMPLES_KEPT, EXAMPLE_CHARS = 2, 80  # per emoji/sticker, RAM only
# Text commands work in every channel, and also when typed with a full-width ！ or pasted as a /name line
# (Discord sends a pasted slash command as plain text). Add new ! commands here.
TEXT_COMMANDS = {"!ask", "!reset", "!draw", "!refine", "!recall", "!edit", "!style", "!search", "!event", "!poll"}
POLL_SPLIT = re.compile(r"[|/,，、｜／]")
MEMBER_MENTION = re.compile(r"<@!?(\d+)>")
POLL_VOTERS = "🗳️ Ends once these members have all voted 这些成员都投完就结束: "
POLL_USAGE = ("Write it as: !poll question | answers | @members (answers and members optional), e.g. "
              "!poll 今晚吃什么 | 炒饭，煎蛋 | @Daddy宏\n格式：!poll 问题 | 选项 | @成员（选项和成员可不写）")
EVENT_USAGE = ("Write it as: !event name | date | time | place (| hours | details), e.g. "
               "!event 电影夜 | 10-10 | 8:30pm | 语音频道\n格式：!event 名称 | 日期 | 时间 | 地点（| 小时 | 说明）")
NO_EVENT_PERMISSION = ("I need the Create Events permission in this server to do that. "
                       "我在这个服务器没有「创建活动」权限，请管理员给我加上。")


def text_command(text):
    """Returns (command, rest) when text starts with a known text command (!, ！ or /), else (None, text)."""
    first, _, rest = text.strip().partition(" ")
    name = "!" + first[1:].lower() if first[:1] in "!！/" else ""
    return (name, rest.strip()) if name in TEXT_COMMANDS else (None, text)


def poll_answers(options):
    """Splits '是 | 不是' (also / , ， 、 ｜ ／) into poll answers; empty means a yes/no poll."""
    answers = [a.strip()[:55] for a in POLL_SPLIT.split(options or "") if a.strip()]
    return answers or ["是 Yes", "不是 No"]


def clock_time(text):
    """Reads 20:30, 8pm, 8:30 PM, 晚上8:30 or 上午9点 as (hour, minute), or None."""
    text = text.strip().lower().replace("：", ":").replace(" ", "").replace("点", ":").rstrip(":")
    pm = text.endswith("pm") or text.startswith(("下午", "晚上", "傍晚"))
    am = text.endswith("am") or text.startswith(("上午", "早上", "凌晨", "中午"))
    match = re.fullmatch(r"(?:上午|早上|凌晨|中午|下午|晚上|傍晚)?(\d{1,2})(?::(\d{2}))?(?:am|pm)?", text)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if (am or pm) and not 1 <= hour <= 12:
        return None
    if pm and hour < 12:
        hour += 12
    elif am and hour == 12 and not text.startswith("中午"):
        hour = 0
    if not match[2] and not (am or pm):
        return None  # a bare "8" could be morning or evening
    return (hour, minute) if hour < 24 and minute < 60 else None


def event_start(date, time, now=None):
    """Parses date (2026-10-10, 2026/10/10, 10-10 or 10/10) and time (20:30, 8pm, 晚上8:30) as the PC's
    local time. Returns None if either can't be read. A date without a year that has passed means next year."""
    now = now or datetime.now().astimezone()
    clock = clock_time(time)
    if clock is None:
        return None
    date = date.strip()
    for fmt, year in (("%Y-%m-%d", ""), ("%Y/%m/%d", ""), ("%Y-%m-%d", f"{now.year}-"), ("%Y/%m/%d", f"{now.year}/")):
        try:
            start = datetime.strptime(f"{year}{date}", fmt).replace(hour=clock[0], minute=clock[1],
                                                                    tzinfo=now.tzinfo)
        except ValueError:
            continue
        if year and start < now:
            start = start.replace(year=now.year + 1)
        return start
    return None


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
            checkpoints = {"anime": config["sd_checkpoint"]}  # SD_CHECKPOINT is the default style
            if config.get("sd_checkpoint_realistic"):
                checkpoints["realistic"] = config["sd_checkpoint_realistic"]
            self.images = ImageMaker(self.brain, config["comfyui_url"], checkpoints,
                                     config["image_size"], config["lmstudio_context"],
                                     comfy_dir=config.get("comfyui_dir", ""))
        self._add_slash_commands()

    def allowed(self, user):
        allowed = self.config["allowed_users"]
        return not allowed or user.id in allowed

    def drawing(self):
        """While pictures are being drawn Gemma is offline and the bot only takes picture requests."""
        return self.images is not None and self.images.drawing

    def is_draw_request(self, content):
        command, rest = text_command(content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", ""))
        return bool(self.images) and (command == "!recall" or (bool(rest) and command in ("!draw", "!refine", "!edit")))

    def recall(self, channel_id, user_id, number):
        """Returns (text, PNG files) to post for /recall; no number lists the member's pictures."""
        pictures, base = self.images.history(channel_id, user_id)
        if not pictures:
            return NOTHING_TO_RECALL, []
        if number is None:
            return RECALL_LIST.format(base=base + 1), [
                discord.File(io.BytesIO(png), f"{i}.png") for i, png in enumerate(pictures, start=1)]
        png = self.images.recall(channel_id, user_id, number)
        if png is None:
            return f"Pick 1 to {len(pictures)}. 请选 1 到 {len(pictures)}。", []
        return RECALLED.format(number=number), [discord.File(io.BytesIO(png), f"{number}.png")]

    def queue_picture(self, request, channel_id, user_id, refine, send, edit, source=None, source_type=None):
        """Queues a picture; send(text) / send(file=...) posts the result later, and
        edit(text) updates the notice with the drawing progress.
        Returns the notice to post now. The picture stays in RAM."""
        async def deliver(png, error):
            if png:
                await send(file=discord.File(io.BytesIO(png), "image.png"))
            else:
                await send(error)

        async def progress(percent):
            await edit(f"{DRAWING_NOTICE}\n{'▓' * (percent // 10)}{'░' * (10 - percent // 10)} {percent}%")

        try:
            ahead = self.images.submit(request, channel_id, user_id, deliver, refine, progress, source,
                                       source_type or "image/png")
        except DrawError as e:
            return str(e)
        if ahead is None:
            return ALREADY_QUEUED
        log.info("Picture queued, %d ahead", ahead)
        return DRAWING_NOTICE if ahead == 0 else QUEUED_NOTICE.format(ahead=ahead)

    def change_style(self, channel_id, name):
        """Text to post after a member asks to switch drawing style (empty name shows the current one)."""
        styles = ", ".join(self.images.checkpoints)
        if not name.strip():
            return f"Drawing style here: {self.images.style(channel_id)}. Styles: {styles}"
        style = self.images.set_style(channel_id, name)
        if style is None:
            return f"Unknown style. Styles: {styles}"
        return f"Drawing style switched to {style}."

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
                                     extras_note(labels, sticker_labels) + (DRAW_HINT if self.images else ""),
                                     limited=not search)
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

    def make_poll(self, question, options="", members="", hours=None, multiple=False):
        """Returns (error, message text, poll); error is set when the poll can't be made."""
        answers = poll_answers(options)
        if len(answers) > 10:
            return "A poll can have at most 10 answers. 投票最多 10 个选项。", None, None
        voters = list(dict.fromkeys(MEMBER_MENTION.findall(members)))
        if members.strip() and not voters:
            return "Pick members with @, e.g. @Daddy宏 @启胜. 请用 @ 选成员。", None, None
        hours = hours or (168 if voters else 24)
        vote = discord.Poll(question=question[:300], duration=timedelta(hours=hours), multiple=multiple)
        for answer in answers:
            vote.add_answer(text=answer)
        # The voter list lives in the poll message itself, so nothing is kept and a restart loses nothing.
        content = (POLL_VOTERS + " ".join(f"<@{v}>" for v in voters)) if voters else None
        return None, content, vote

    async def create_event(self, guild, user_name, name, date, time, place, hours=2.0, details=""):
        """Creates a Discord scheduled event; returns the text to post."""
        start = event_start(date, time)
        if start is None:
            return ("I can't read that date or time. Use e.g. date 2026-10-10 and time 20:30 or 8:30pm. "
                    "日期或时间看不懂，请写成 2026-10-10 和 20:30 或 8:30pm。")
        if start <= datetime.now().astimezone():
            return "That time has already passed. 这个时间已经过了。"
        log.info("Creating an event in guild %s", guild.id)
        try:
            created = await guild.create_scheduled_event(
                name=name[:100], start_time=start, end_time=start + timedelta(hours=hours),
                entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                location=place[:100], description=details[:1000])
        except discord.Forbidden:
            return NO_EVENT_PERMISSION
        except discord.HTTPException as e:
            log.warning("Creating an event failed: HTTP %s, code %s", e.status, e.code)
            return f"Discord refused the event (HTTP {e.status}, code {e.code}). Discord 拒绝了这个活动。"
        return (f"📅 {user_name} created an event 建了一个活动: **{created.name}**\n"
                f"🕒 <t:{int(start.timestamp())}:F>\n📍 {created.location}\n{created.url}")

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

        @self.tree.command(name="status", description="Show how busy the bot's PC is (graphics card, CPU, RAM)")
        async def status(interaction: discord.Interaction):
            # Works while drawing too, so members can see the graphics card load.
            async def reading():
                stats = await asyncio.to_thread(monitor.read, 0.5)
                return "```\n" + "\n".join(monitor.lines(stats)) + "\n```"

            await interaction.response.send_message(await reading(), ephemeral=True)
            for _ in range(STATUS_UPDATES):  # live for a while, then the last reading stays
                await asyncio.sleep(STATUS_EVERY)
                try:
                    await interaction.edit_original_response(content=await reading())
                except discord.HTTPException:  # the member dismissed it
                    return

        @self.tree.command(name="poll", description="Start a poll members vote on")
        @app_commands.describe(question="What to vote on",
                               options="Answers split by | , or /, e.g. '是 | 不是'; leave empty for yes/no",
                               members="Who votes, e.g. '@Daddy宏 @启胜': the poll ends once they all have",
                               hours="How long it stays open (1-768 hours; default 24, or 168 = 7 days when members are named)",
                               multiple="Let members pick more than one answer")
        async def poll(interaction: discord.Interaction, question: str, options: str = "", members: str = "",
                       hours: app_commands.Range[int, 1, 768] = None, multiple: bool = False):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            error, content, vote = self.make_poll(question, options, members, hours, multiple)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await interaction.response.send_message(content, poll=vote)

        @self.tree.command(name="event", description="Create a server event with a date, time and place")
        @app_commands.describe(name="What the event is", date="Date, e.g. 2026-10-10 or 10-10",
                               time="Start time, e.g. 20:30, 8:30pm or 晚上8:30", place="Where it happens",
                               hours="How long it lasts (default 2 hours)", details="More about it (optional)")
        async def event(interaction: discord.Interaction, name: str, date: str, time: str, place: str,
                        hours: app_commands.Range[float, 0.25, 72.0] = 2.0, details: str = ""):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            if interaction.guild is None:
                await interaction.response.send_message("Events only work in a server. 活动只能在服务器里建。",
                                                        ephemeral=True)
                return
            await interaction.response.defer(thinking=True)  # Discord gives up on a reply after 3 seconds
            await interaction.followup.send(await self.create_event(
                interaction.guild, interaction.user.display_name, name, date, time, place, hours, details))

        if self.images is None:
            return

        async def draw_command(interaction, request, refine, image=None):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            source = source_type = None
            if image is not None:
                source_type = (image.content_type or "").split(";")[0]
                if source_type not in IMAGE_TYPES:
                    await interaction.response.send_message(NO_PICTURE, ephemeral=True)
                    return
                source = await image.read()  # RAM only
            caption = f"{interaction.user.mention}: {request[:200]}"

            async def send(text=None, file=None):
                # The channel, not the interaction: its token expires after 15 minutes in the queue.
                if file:
                    await interaction.channel.send(caption, file=file)
                else:
                    await interaction.channel.send(f"{interaction.user.mention} {text}")

            async def edit(text):
                await interaction.edit_original_response(content=text)

            notice = self.queue_picture(request, interaction.channel_id, interaction.user.id, refine, send, edit,
                                        source, source_type)
            await interaction.response.send_message(notice, ephemeral=not notice.startswith("🎨"))  # refusals only to the asker

        @self.tree.command(name="draw", description="Draw a picture (Gemma goes offline until all pictures are done)")
        @app_commands.describe(request="What to draw; start with anime or realistic to pick the style")
        async def draw(interaction: discord.Interaction, request: str):
            await draw_command(interaction, request, refine=False)

        @self.tree.command(name="refine", description="Change your last picture in this channel (or the one /recall picked)")
        @app_commands.describe(changes="What to change, e.g. 'make it night time'")
        async def refine(interaction: discord.Interaction, changes: str):
            await draw_command(interaction, changes, refine=True)

        @self.tree.command(name="edit", description="Upload a picture and say what to change")
        @app_commands.describe(image="The picture to change", changes="What to change, e.g. 'make the hair red'")
        async def edit(interaction: discord.Interaction, image: discord.Attachment, changes: str):
            await draw_command(interaction, changes, refine=False, image=image)

        @self.tree.command(name="recall", description="Go back to one of your last pictures, so /refine builds on it")
        @app_commands.describe(number="Which picture (1 = oldest); leave empty to see them all")
        async def recall(interaction: discord.Interaction, number: int = None):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            text, files = self.recall(interaction.channel_id, interaction.user.id, number)
            await interaction.response.send_message(text, files=files, ephemeral=not files)

        @self.tree.command(name="drawstyle", description="Switch the drawing style in this channel")
        @app_commands.describe(style="anime or realistic; leave empty to see the current one")
        async def drawstyle(interaction: discord.Interaction, style: str = ""):
            if self.drawing():
                return
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            await interaction.response.send_message(self.change_style(interaction.channel_id, style))

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

    async def on_raw_poll_vote_add(self, payload):
        """Ends a /poll that names its voters once every one of them has voted."""
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return
        message = await channel.fetch_message(payload.message_id)
        if message.author.id != self.user.id or not message.content.startswith(POLL_VOTERS) \
                or message.poll is None or message.poll.is_finalised():
            return
        wanted = {int(v) for v in MEMBER_MENTION.findall(message.content)}
        voted = set()
        for answer in message.poll.answers:
            voted |= {u.id async for u in answer.voters()}
        if wanted <= voted:
            log.info("Every named member voted; ending poll %s", message.id)
            await message.end_poll()

    async def on_message(self, message):
        log.info("Message in channel %s from user %s (%d chars, %d attachments)",
                 message.channel.id, message.author.id, len(message.content), len(message.attachments))
        if message.author.bot or (self.drawing() and not self.is_draw_request(message.content)):
            return
        self.note_usage(message)
        if not self.allowed(message.author):
            return
        text = message.content.strip()

        for tag in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            text = text.replace(tag, "")
        command, rest = text_command(text)
        if command:
            text = f"{command} {rest}".strip()  # one spelling for everything below
        mentioned = self.user in message.mentions
        in_bot_channel = message.channel.id in self.config["channel_ids"]
        if not (command or mentioned or in_bot_channel):
            return  # outside the bot channels only text commands and @mentions are answered
        text = text.strip()

        if command == "!reset":
            self.memory.reset(message.channel.id)
            await message.reply("Memory for this channel cleared.", mention_author=False)
            return
        if command == "!event" and message.guild is not None:
            parts = [p.strip() for p in re.split(r"[|｜]", rest)]
            if len(parts) < 4 or not all(parts[:4]):
                await message.reply(EVENT_USAGE, mention_author=False)
                return
            hours = 2.0
            if len(parts) > 4 and parts[4]:
                try:
                    hours = min(max(float(parts[4].rstrip("小时hH ")), 0.25), 72.0)
                except ValueError:
                    pass
            await message.reply(await self.create_event(message.guild, message.author.display_name,
                                                        *parts[:4], hours, " | ".join(parts[5:])),
                                mention_author=False)
            return
        if command == "!poll":
            # !poll question | answers | @members  (answers and members optional)
            parts = [p.strip() for p in re.split(r"[|｜]", rest, maxsplit=2)] + ["", ""]
            if not parts[0]:
                await message.reply(POLL_USAGE, mention_author=False)
                return
            error, content, vote = self.make_poll(parts[0], parts[1], parts[2])
            if error:
                await message.reply(error, mention_author=False)
            else:
                await message.channel.send(content, poll=vote)
            return
        if command == "!ask":
            text = rest  # a plain question, answered like any chat message
        command = text.split(" ", 1)[0].lower()
        if self.images and command == "!style":
            await message.reply(self.change_style(message.channel.id, text[len(command):]),
                                mention_author=False)
            return
        if self.images and command == "!recall":
            number = text[len(command):].strip()
            text, files = self.recall(message.channel.id, message.author.id,
                                      int(number) if number.isdigit() else None)
            await message.reply(text, files=files, mention_author=False)
            return
        if self.images and command in ("!draw", "!refine", "!edit") and text[len(command):].strip():
            source = source_type = None
            if command == "!edit":
                pictures = [a for a in message.attachments if (a.content_type or "").split(";")[0] in IMAGE_TYPES]
                if not pictures:
                    await message.reply(NO_PICTURE, mention_author=False)
                    return
                source, source_type = await pictures[0].read(), pictures[0].content_type.split(";")[0]  # RAM only

            async def send(text=None, file=None):
                # mention_author pings the member, since the picture can arrive minutes later.
                if file:
                    await message.reply(DONE_NOTICE, file=file, mention_author=True)
                else:
                    await message.reply(text, mention_author=True)

            notice_message = None

            async def edit(text):
                if notice_message:
                    await notice_message.edit(content=text)

            notice = self.queue_picture(text[len(command):].strip(), message.channel.id, message.author.id,
                                        command == "!refine", send, edit, source, source_type)
            notice_message = await message.reply(notice, mention_author=False)
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
    if config["monitor_window"]:
        monitor.open_window()
    for name in ("httpx", "httpx2"):  # their INFO lines carry request URLs
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("primp", "ddgs"):  # their lines carry web search queries; search.py logs failures itself
        logging.getLogger(name).disabled = True
    ChatBot(config).run(config["token"], root_logger=True)


if __name__ == "__main__":
    main()
