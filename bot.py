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
from imagegen import MAX_REFERENCES, DrawError, ImageMaker
from search import needs_search, web_search

log = logging.getLogger("bot")

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
STICKER_CHANCE = 0.2  # share of chat replies where the model is offered stickers
CDN = "https://cdn.discordapp.com"
CUSTOM_EMOJI = re.compile(r"<a?:(\w+):(\d+)>")
DRAWING_NOTICE = "🎨 Drawing... I'm offline until it's done. 画画中，画完才回来。"
DONE_NOTICE = "🎨 Done! 画好了！"
# !dmdraw, !dmedit, !dmrefine, !dmrecall: the picture only goes by DM and the channel sees no name.
PRIVATE = 0  # history key for private pictures: one per member, apart from every channel's (no channel id is 0)
DM_COMMANDS = {"!dmdraw": "!draw", "!dmedit": "!edit", "!dmrefine": "!refine", "!dmrecall": "!recall"}
PRIVATE_NOTICE = "🎨 Drawing a private request... I'm offline until it's done. 画画中（私人请求），画完才回来。"
DM_ACK = "Got it, your picture will arrive here. 收到，画好会发到这里。"
DM_STARTED = "Started; the picture will come by DM. 开始画了，画好私信给你。"
DM_FAILED = ("I can't send you private messages. Allow DMs from server members, then try again. "
             "我私信不了你，请在隐私设置打开「允许服务器成员私信」再试。")
CANT_HIDE = ("I can't delete your request in that channel (I need Manage Messages there), so others can see it; "
             "/dmdraw and the other / forms stay hidden. 我在那个频道没有「管理消息」权限，删不掉你的指令，"
             "别人看得到；请管理员给我这个权限，或改用 /dmdraw 等斜杠指令（只有你看得到）。")
DM_USAGE = ("Write what to draw or change after the command, e.g. !dmdraw 一只猫. "
            "请在指令后写要画或要改的内容，例如 !dmdraw 一只猫。")
FIRST_TIMING = "⏱️ First picture since I started, timing it; estimates start with the next one. 第一张图，计时中，下一张起会显示预计时间。"
# Added to the system prompt when drawing is on, so Gemma stops saying it can't make pictures.
DRAW_HINT = ("\n\nThis bot can draw pictures, but not in a normal reply: if someone asks you to draw "
             "or make a picture, tell them to send !draw followed by what they want (add realistic "
             "first for a photo look), or use /draw.")
QUEUED_NOTICE = ("🎨 Queued, {ahead} picture(s) ahead of you. I'll chat again once every picture is done. "
                 "已排队，前面还有 {ahead} 张，全部画完我才回来聊天。")
NO_PICTURE = "Attach the picture to edit. 请附上要修改的图片。"
EDIT_USAGE = ("Attach a picture and write the change, e.g. !edit 头发改成红色; attach a second picture to put "
              "its character into the first. 请附上图片并写要改什么，例如 !edit 头发改成红色；"
              "再附第二张图，就把第二张的角色换进第一张。")
NOTHING_TO_RECALL = "You have no pictures here yet. 你在这个频道还没有图。"
RECALLED = ("↩️ Undone: back to the picture before. Your next refine starts from it with a fresh try. "
            "已撤回最新那张，回到上一张；下次 refine 从这张重新画。")
AT_OLDEST = "Nothing to undo: this is your first picture. 没有可以撤回的了，这是你的第一张。"
ALREADY_QUEUED = ("You already have a picture waiting. Ask again once it's done. "
                  "你已经有一张在排队了，画完才能再点。")
STATUS_EVERY, STATUS_UPDATES = 2.5, 20  # /status refreshes about every 3 s for a minute
EXAMPLES_KEPT, EXAMPLE_CHARS = 2, 80  # per emoji/sticker, RAM only
# "/comment ..." typed as plain text: reposted under the member's name without the prefix.
COMMENT = re.compile(r"\s*(<@!?\d+>\s*)?[/／!！]comment\b", re.IGNORECASE)
NO_COMMENT_PERMISSION = ("I need the Manage Webhooks permission in this channel to post that. "
                         "我在这个频道没有「管理 Webhooks」权限，请管理员给我加上。")
# Text commands work in every channel, and also when typed with a full-width ！ or pasted as a /name line
# (Discord sends a pasted slash command as plain text). Add new ! commands here.
TEXT_COMMANDS = {"!ask", "!reset", "!draw", "!refine", "!recall", "!edit", "!style", "!search", "!event", "!poll",
                 "!status", "!comment", "!dmdraw", "!dmedit", "!dmrefine", "!dmrecall"}
POLL_SPLIT = re.compile(r"[|/,，、｜／]")
MEMBER_MENTION = re.compile(r"<@!?(\d+)>")
POLL_VOTERS = "🗳️ Ends once these members have all voted 这些成员都投完就结束: "
POLL_USAGE = ("Write it as: !poll question | answers | @members (answers and members optional), e.g. "
              "!poll 今晚吃什么 | 炒饭，煎蛋 | @Daddy宏\n格式：!poll 问题 | 选项 | @成员（选项和成员可不写）")
EVENT_USAGE = ("Write it as: !event name | date | time | place (| hours | details), any order as long as "
               "the name comes before the place, e.g. !event 电影夜 | 10-10 | 8:30pm | 语音频道\n"
               "格式：!event 名称 | 日期 | 时间 | 地点（| 小时 | 说明），顺序随意，名称写在地点前面就行")
NO_EVENT_PERMISSION = ("I need the Create Events permission in this server to do that. "
                       "我在这个服务器没有「创建活动」权限，请管理员给我加上。")


def text_command(text):
    """Returns (command, rest) when text starts with a known text command (!, ！ or /), else (None, text)."""
    first, _, rest = text.strip().partition(" ")
    name = "!" + first[1:].lower() if first[:1] in "!！/" else ""
    return (name, rest.strip()) if name in TEXT_COMMANDS else (None, text)


def eta_text(seconds):
    """'⏱️ 预计 20 分 55 秒 Estimate 20 min 55 sec' for a picture's wait, or FIRST_TIMING with nothing timed yet."""
    if seconds is None:
        return FIRST_TIMING
    minutes, seconds = divmod(round(seconds), 60)
    if minutes:
        return f"⏱️ 预计 {minutes} 分 {seconds} 秒 Estimate {minutes} min {seconds} sec"
    return f"⏱️ 预计 {seconds} 秒 Estimate {seconds} sec"


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


def event_hours(text):
    """Reads 3, 1.5, 3h, 2小时 or 两个小时 / 一个半小时 as hours, or None."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*个?\s*(半)?\s*(小时|钟头|h|hrs?|hours?)?",
                         text.strip(), re.IGNORECASE)
    if not match or (not match[1][0].isdigit() and not match[3]):
        return None  # a lone Chinese numeral is more likely a name than hours
    number = float(match[1]) if match[1][0].isdigit() else "一二三四五六七八九十".find(match[1]) + 1 or 2.0
    return number + (0.5 if match[2] else 0)


def event_fields(parts):
    """Sorts !event fields given in any order into (name, date, time, place, hours, details), or None.
    Date, time and hours are known by their look; the other fields are name, then place, then details."""
    date = time = hours = None
    texts = []
    for part in filter(None, parts):
        if date is None and event_start(part, "0:00") is not None:
            date = part
        elif time is None and clock_time(part) is not None:
            time = part
        elif hours is None and event_hours(part) is not None:
            hours = event_hours(part)
        else:
            texts.append(part)
    if not (date and time and len(texts) >= 2):
        return None
    return texts[0], date, time, texts[1], min(max(hours or 2.0, 0.25), 72.0), " | ".join(texts[2:])


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


ONLY_CREATOR = "Only the person who started this can end it. 只有发起人才能结束。"


def vote_summary(poll):
    """Text with each answer's current vote count."""
    if not poll.total_votes:
        return "No votes yet. 还没有人投票。"
    return "\n".join(f"• {a.text}: {a.vote_count}" for a in poll.answers)


class EndPollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"endpoll:(?P<creator>\d+)"):
    """End button under a poll; who may press it is kept in the button itself, so restarts don't matter."""
    def __init__(self, creator):
        super().__init__(discord.ui.Button(label="End poll 结束投票", style=discord.ButtonStyle.danger,
                                           custom_id=f"endpoll:{creator}"))
        self.creator = creator

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["creator"]))

    async def callback(self, interaction):
        if interaction.user.id != self.creator:
            await interaction.response.send_message(ONLY_CREATOR, ephemeral=True)
            return
        message = interaction.message
        if message.poll is None or message.poll.is_finalised():
            await interaction.response.edit_message(view=None)
            return
        summary = vote_summary(message.poll)  # counts as they are now, before Discord tallies the end
        await message.end_poll()
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(f"🗳️ {interaction.user.display_name} ended the poll 结束了投票: "
                                        f"**{message.poll.question}**\n{summary}")


class EndEventButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"endevent:(?P<creator>\d+):(?P<event>\d+)"):
    """End button under an event: cancels it if it hasn't started, ends it if it has."""
    def __init__(self, creator, event_id):
        super().__init__(discord.ui.Button(label="End event 结束活动", style=discord.ButtonStyle.danger,
                                           custom_id=f"endevent:{creator}:{event_id}"))
        self.creator, self.event_id = creator, event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["creator"]), int(match["event"]))

    async def callback(self, interaction):
        if interaction.user.id != self.creator:
            await interaction.response.send_message(ONLY_CREATOR, ephemeral=True)
            return
        await interaction.response.defer()  # fetching and cancelling can take longer than Discord's 3 s
        try:
            event = await interaction.guild.fetch_scheduled_event(self.event_id)
            if event.status == discord.EventStatus.active:
                await event.end()
            elif event.status == discord.EventStatus.scheduled:
                await event.cancel()
        except discord.NotFound:
            pass  # already deleted
        except discord.HTTPException as e:
            if e.code == 180000:  # already ended or cancelled, e.g. by a second click on the same button
                await interaction.edit_original_response(view=None)
                return
            log.warning("Ending an event failed: HTTP %s, code %s", e.status, e.code)
            await interaction.followup.send(f"Discord refused to end the event (HTTP {e.status}, code {e.code}). "
                                            "Discord 拒绝结束这个活动。", ephemeral=True)
            return
        await interaction.edit_original_response(view=None)
        await interaction.followup.send(f"📅 {interaction.user.display_name} ended the event 结束了活动。")


PICK_EVENT_TIME = "Pick the date, time and length, then press Create. 选好日期、时间和时长，再点「创建」。"
EVENT_LENGTHS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0)


def event_date_options(now=None):
    """The next 25 days (Discord's most per menu) as (2026-10-10, '10-10 周六（今天）')."""
    now = now or datetime.now().astimezone()
    days = [now + timedelta(days=i) for i in range(25)]
    return [(d.strftime("%Y-%m-%d"), f"{d:%m-%d} 周{'一二三四五六日'[d.weekday()]}" + ("（今天）", "（明天）", "")[min(i, 2)])
            for i, d in enumerate(days)]


def hour_label(hour):
    part = "凌晨" if hour < 6 else "上午" if hour < 12 else "中午" if hour == 12 else "下午" if hour < 18 else "晚上"
    return f"{part}{hour % 12 or 12}点 ({hour:02d}:00)"


class EventPicker(discord.ui.View):
    """Menus for an event's date, start and length; only its maker sees them. Defaults: today 20:00, 2 hours."""
    def __init__(self, name, place, details):
        super().__init__(timeout=600)
        self.name, self.place, self.details = name, place, details
        dates = event_date_options()
        self.date, self.hour, self.minute, self.hours = dates[0][0], 20, 0, 2.0
        self.menu("日期 Date", dates, "date")
        self.menu("几点 Hour", [(h, hour_label(h)) for h in range(24)], "hour")
        self.menu("几分 Minute", [(m, f"{m:02d} 分") for m in (0, 15, 30, 45)], "minute")
        self.menu("时长 Length", [(h, f"{h:g} 小时 hours") for h in EVENT_LENGTHS], "hours")
        create = discord.ui.Button(label="Create 创建", style=discord.ButtonStyle.success)
        create.callback = self.create
        self.add_item(create)

    def menu(self, placeholder, options, field):
        default = getattr(self, field)
        select = discord.ui.Select(placeholder=placeholder, options=[
            discord.SelectOption(label=label, value=str(value), default=value == default) for value, label in options])

        async def chosen(interaction):
            setattr(self, field, type(default)(select.values[0]))
            await interaction.response.defer()
        select.callback = chosen
        self.add_item(select)

    async def create(self, interaction):
        await interaction.response.defer()
        text, view = await interaction.client.create_event(
            interaction.guild, interaction.user, self.name, self.date, f"{self.hour}:{self.minute:02d}",
            self.place, self.hours, self.details)
        if view is discord.utils.MISSING:  # e.g. the time has passed: say so and keep the menus
            await interaction.followup.send(text, ephemeral=True)
            return
        self.stop()
        await interaction.edit_original_response(content="✅", view=None)
        await interaction.followup.send(text, view=view)


class EventForm(discord.ui.Modal, title="Create an event 创建活动"):
    """Typed parts of an event; the date and time are then picked from menus."""
    event_name = discord.ui.TextInput(label="名称 Name", max_length=100)
    place = discord.ui.TextInput(label="地点 Place", max_length=100)
    details = discord.ui.TextInput(label="说明 Details (optional)", style=discord.TextStyle.paragraph,
                                   required=False, max_length=1000)

    def __init__(self, name="", place="", details=""):
        super().__init__()
        self.event_name.default, self.place.default, self.details.default = name or None, place or None, details or None

    async def on_submit(self, interaction):
        await interaction.response.send_message(PICK_EVENT_TIME, ephemeral=True, view=EventPicker(
            self.event_name.value, self.place.value, self.details.value))


class EventFormButton(discord.ui.DynamicItem[discord.ui.Button], template=r"eventform"):
    """Under a bare !event: opens the event form, since a typed message can't open one itself."""
    def __init__(self):
        super().__init__(discord.ui.Button(label="📅 Create event 创建活动", style=discord.ButtonStyle.primary,
                                           custom_id="eventform"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        if not interaction.client.allowed(interaction.user):
            await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
            return
        await interaction.response.send_modal(EventForm())


def end_view(item):
    view = discord.ui.View(timeout=None)
    view.add_item(item)
    return view


class ChatBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.memory = ChannelMemory()
        self._hooks = {}  # channel id -> webhook used to repost /comment messages
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
                                     comfy_dir=config.get("comfyui_dir", ""),
                                     controlnet=config.get("sd_controlnet", ""))
        self._add_slash_commands()

    def allowed(self, user):
        allowed = self.config["allowed_users"]
        return not allowed or user.id in allowed

    def drawing(self):
        """While pictures are being drawn Gemma is offline and the bot only takes picture requests."""
        return self.images is not None and self.images.drawing

    def is_draw_request(self, content):
        command, rest = text_command(content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", ""))
        command = DM_COMMANDS.get(command, command)  # private ones go through too
        return bool(self.images) and (command == "!recall" or (bool(rest) and command in ("!draw", "!refine", "!edit")))

    def recall(self, channel_id, user_id):
        """Returns (text, PNG files) to post for /recall: undoes the member's newest picture, the usual
        move after a refine that didn't work out, and shows only the picture before it."""
        if not self.images.history(channel_id, user_id):
            return NOTHING_TO_RECALL, []
        png = self.images.undo(channel_id, user_id)
        if png is None:
            return AT_OLDEST, []
        return RECALLED, [discord.File(io.BytesIO(png), "image.png")]

    def queue_picture(self, request, channel_id, user_id, refine, send, edit, source=None, source_type=None,
                      references=(), private=False):
        """Queues a picture; send(text) / send(file=...) posts the result later, and
        edit(text) updates the notice with the drawing progress.
        Returns the notice to post now. The picture stays in RAM. A private picture goes into the
        member's private history, keeps channel_id's drawing style, and its notice names no one."""
        header = PRIVATE_NOTICE if private else DRAWING_NOTICE
        async def deliver(png, error):
            if png:
                await send(file=discord.File(io.BytesIO(png), "image.png"))
            else:
                await send(error)

        async def progress(update):  # a percentage while ComfyUI draws, or a line of text before that
            if isinstance(update, str):
                await edit(f"{header}\n{update}")
            else:
                await edit(f"{header}\n{'▓' * (update // 10)}{'░' * (10 - update // 10)} {update}%")

        try:
            ahead = self.images.submit(request, PRIVATE if private else channel_id, user_id, deliver, refine, progress,
                                       source, source_type or "image/png", references,
                                       style_channel=channel_id)
        except DrawError as e:
            return str(e)
        if ahead is None:
            return ALREADY_QUEUED
        log.info("Picture queued, %d ahead", ahead)
        notice = header if ahead == 0 else QUEUED_NOTICE.format(ahead=ahead)
        return f"{notice}\n{eta_text(self.images.estimate())}"

    async def private_picture(self, message, command, rest):
        """!dmdraw / !dmedit / !dmrefine / !dmrecall (command is the public name): like the public ones, but
        the picture only goes by DM, the request is deleted from the channel, and the channel only sees a
        notice that names no one. If the member can't get DMs, nothing is drawn."""
        uploads = [(await a.read(), a.content_type.split(";")[0]) for a in message.attachments  # RAM only,
                   if (a.content_type or "").split(";")[0] in IMAGE_TYPES][:1 + MAX_REFERENCES]  # before the delete
        try:
            dm = await message.author.create_dm()
            await dm.send(DM_ACK)
        except discord.HTTPException:
            await message.reply(DM_FAILED, mention_author=False)
            return
        if message.guild is not None:
            if message.channel.permissions_for(message.guild.me).manage_messages:
                await message.delete()
            else:
                log.warning("Can't delete a private request: no Manage Messages in channel %s", message.channel.id)
                await dm.send(CANT_HIDE)
        if command == "!recall":
            text, files = self.recall(PRIVATE, message.author.id)
            await dm.send(text, files=files)
            return
        if not rest or (command == "!edit" and not uploads):
            await dm.send(DM_USAGE if not rest else NO_PICTURE)
            return
        source, source_type = uploads[0] if uploads else (None, None)

        async def send(text=None, file=None):
            if file:
                await dm.send(DONE_NOTICE, file=file)
            else:
                await dm.send(text)
        notices = []

        async def edit(text):
            if notices:
                await notices[0].edit(content=text)

        notice = self.queue_picture(rest, message.channel.id, message.author.id, command == "!refine", send, edit,
                                    source, source_type, uploads[1:], private=True)
        if notice.startswith("🎨"):
            notices.append(await message.channel.send(notice))
        else:  # already queued, nothing to refine: only the member hears it
            await dm.send(notice)

    async def status_text(self):
        stats = await asyncio.to_thread(monitor.read, 0.5)
        return "```\n" + "\n".join(monitor.lines(stats)) + "\n```"

    async def keep_status_live(self, edit):
        """Refresh a posted status for a while; the last reading then stays."""
        for _ in range(STATUS_UPDATES):
            await asyncio.sleep(STATUS_EVERY)
            try:
                await edit(await self.status_text())
            except discord.HTTPException:  # dismissed or deleted
                return

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
        results = lore = ""
        if text and (search or needs_search(text)):
            log.info("Searching the web for a message in channel %s", channel_id)
            results = await web_search(text)
        elif query := self.brain.lore_query(channel_id, text):
            log.info("Character looking things up quietly in channel %s", channel_id)
            lore = await web_search(query)
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
                                     limited=not search, lore=lore)
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

    async def create_event(self, guild, user, name, date, time, place, hours=2.0, details=""):
        """Creates a Discord scheduled event; returns the text to post and, on success, a view with
        an End button only the creator can use."""
        text = await self._create_event(guild, user, name, date, time, place, hours, details)
        return text if isinstance(text, tuple) else (text, discord.utils.MISSING)

    async def _create_event(self, guild, user, name, date, time, place, hours, details):
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
        return (f"📅 {user.display_name} created an event 建了一个活动: **{created.name}**\n"
                f"🕒 <t:{int(start.timestamp())}:F>\n📍 {created.location}\n{created.url}",
                end_view(EndEventButton(user.id, created.id)))

    async def post_as(self, channel, member, text, files=()):
        """Posts under the member's name and avatar through the channel's webhook; False if not allowed."""
        thread = channel if isinstance(channel, discord.Thread) else discord.utils.MISSING
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if not parent.permissions_for(parent.guild.me).manage_webhooks:
            return False
        hook = self._hooks.get(parent.id)
        if hook is None:
            hook = next((h for h in await parent.webhooks() if h.user and h.user.id == self.user.id), None) \
                or await parent.create_webhook(name="Gemma comment")
            self._hooks[parent.id] = hook
        await hook.send(text or discord.utils.MISSING, username=member.display_name,
                        avatar_url=member.display_avatar.url, files=list(files), thread=thread,
                        allowed_mentions=discord.AllowedMentions.none())  # the original already pinged
        return True

    async def hide_comment(self, message, text):
        """Swaps a '/comment ...' message for the same words under the member's name, so it reads like chat.
        Left as it is when the bot can't delete messages or use webhooks here."""
        if not message.channel.permissions_for(message.guild.me).manage_messages:
            return
        files = [await a.to_file() for a in message.attachments]  # RAM only
        if (text or files) and await self.post_as(message.channel, message.author, text, files):
            await message.delete()

    def _add_slash_commands(self):
        @self.tree.command(name="comment", description="Talk to members only: the bot won't reply or remember it")
        async def comment(interaction: discord.Interaction, text: str):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.guild is None or not await self.post_as(interaction.channel, interaction.user, text):
                await interaction.followup.send(NO_COMMENT_PERMISSION, ephemeral=True)
                return
            await interaction.delete_original_response()

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
                # Who they are, who they know (Gemma forgets teammates otherwise) and how they fight.
                results = "\n\n".join([await web_search(f"{name} character personality speech style quotes"),
                                        await web_search(f"{name} teammates friends relationships story"),
                                        await web_search(f"{name} abilities techniques explained")])
                text = await self.brain.character_persona(name, results)
                if text is None:
                    await interaction.followup.send(
                        f"Sorry, I couldn't find out enough about **{name}**. "
                        "Try adding the game or show, e.g. 'Ganyu Genshin Impact'.")
                    return
                self.brain.set_persona(interaction.channel_id, text, character=name)
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
            await interaction.response.send_message(await self.status_text(), ephemeral=True)
            await self.keep_status_live(lambda text: interaction.edit_original_response(content=text))

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
            await interaction.response.send_message(content, poll=vote,
                                                    view=end_view(EndPollButton(interaction.user.id)))

        @self.tree.command(name="event", description="Create a server event with a date, time and place")
        @app_commands.describe(name="What the event is", date="Date, e.g. 2026-10-10 or 10-10",
                               time="Start time, e.g. 20:30, 8:30pm or 晚上8:30", place="Where it happens",
                               hours="How long it lasts (default 2 hours)", details="More about it (optional)")
        async def event(interaction: discord.Interaction, name: str = "", date: str = "", time: str = "",
                        place: str = "", hours: app_commands.Range[float, 0.25, 72.0] = 2.0, details: str = ""):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            if interaction.guild is None:
                await interaction.response.send_message("Events only work in a server. 活动只能在服务器里建。",
                                                        ephemeral=True)
                return
            if not (name and date and time and place):  # pick the date and time from menus instead
                await interaction.response.send_modal(EventForm(name, place, details))
                return
            await interaction.response.defer(thinking=True)  # Discord gives up on a reply after 3 seconds
            text, view = await self.create_event(interaction.guild, interaction.user, name, date, time, place,
                                                 hours, details)
            await interaction.followup.send(text, view=view)

        if self.images is None:
            return

        async def draw_command(interaction, request, refine, image=None, character=None, private=False):
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
            references = []
            if character is not None:
                reference_type = (character.content_type or "").split(";")[0]
                if reference_type not in IMAGE_TYPES:
                    await interaction.response.send_message(NO_PICTURE, ephemeral=True)
                    return
                references.append((await character.read(), reference_type))  # RAM only
            caption = f"{interaction.user.mention}: {request[:200]}"
            dm, notices = None, []
            if private:  # the picture goes by DM, so make sure one gets through before drawing
                try:
                    dm = await interaction.user.create_dm()
                    await dm.send(DM_ACK)
                except discord.HTTPException:
                    await interaction.response.send_message(DM_FAILED, ephemeral=True)
                    return

            async def send(text=None, file=None):
                # The channel, not the interaction: its token expires after 15 minutes in the queue.
                if dm:
                    await (dm.send(DONE_NOTICE, file=file) if file else dm.send(text))
                elif file:
                    await interaction.channel.send(caption, file=file)
                else:
                    await interaction.channel.send(f"{interaction.user.mention} {text}")

            async def edit(text):
                if not private:
                    await interaction.edit_original_response(content=text)
                elif notices:
                    await notices[0].edit(content=text)

            notice = self.queue_picture(request, interaction.channel_id, interaction.user.id, refine, send, edit,
                                        source, source_type, references, private=private)
            if not private:
                await interaction.response.send_message(notice, ephemeral=not notice.startswith("🎨"))  # refusals only to the asker
                return
            started = notice.startswith("🎨")
            await interaction.response.send_message(DM_STARTED if started else notice, ephemeral=True)
            if started:  # an unnamed notice, so members know why the bot is quiet
                notices.append(await interaction.channel.send(notice))

        @self.tree.command(name="draw", description="Draw a picture (Gemma goes offline until all pictures are done)")
        @app_commands.describe(request="What to draw; start with anime or realistic to pick the style")
        async def draw(interaction: discord.Interaction, request: str):
            await draw_command(interaction, request, refine=False)

        @self.tree.command(name="refine", description="Change your last picture in this channel (or the one /recall picked)")
        @app_commands.describe(changes="What to change, e.g. 'make it night time'")
        async def refine(interaction: discord.Interaction, changes: str):
            await draw_command(interaction, changes, refine=True)

        @self.tree.command(name="edit", description="Upload a picture and say what to change")
        @app_commands.describe(image="The picture to change", changes="What to change, e.g. 'make the hair red'",
                               character="Optional: a picture of a character to put into the first one")
        async def edit(interaction: discord.Interaction, image: discord.Attachment, changes: str,
                       character: discord.Attachment = None):
            await draw_command(interaction, changes, refine=False, image=image, character=character)

        @self.tree.command(name="dmdraw", description="Draw a picture only you get, by DM")
        @app_commands.describe(request="What to draw; start with anime or realistic to pick the style")
        async def dmdraw(interaction: discord.Interaction, request: str):
            await draw_command(interaction, request, refine=False, private=True)

        @self.tree.command(name="dmrefine", description="Change your last private picture (or the one /dmrecall picked)")
        @app_commands.describe(changes="What to change, e.g. 'make it night time'")
        async def dmrefine(interaction: discord.Interaction, changes: str):
            await draw_command(interaction, changes, refine=True, private=True)

        @self.tree.command(name="dmedit", description="Upload a picture and say what to change; the result comes by DM")
        @app_commands.describe(image="The picture to change", changes="What to change, e.g. 'make the hair red'",
                               character="Optional: a picture of a character to put into the first one")
        async def dmedit(interaction: discord.Interaction, image: discord.Attachment, changes: str,
                         character: discord.Attachment = None):
            await draw_command(interaction, changes, refine=False, image=image, character=character, private=True)

        @self.tree.command(name="dmrecall", description="Undo your newest private picture; the one before comes by DM")
        async def dmrecall(interaction: discord.Interaction):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            text, files = self.recall(PRIVATE, interaction.user.id)
            try:
                await (await interaction.user.create_dm()).send(text, files=files)
            except discord.HTTPException:
                await interaction.response.send_message(DM_FAILED, ephemeral=True)
                return
            await interaction.response.send_message("Sent by DM. 已私信你。", ephemeral=True)

        @self.tree.command(name="recall", description="Undo your newest picture and go back to the one before")
        async def recall(interaction: discord.Interaction):
            if not self.allowed(interaction.user):
                await interaction.response.send_message("Sorry, you can't use this bot.", ephemeral=True)
                return
            text, files = self.recall(interaction.channel_id, interaction.user.id)
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
        self.add_dynamic_items(EndPollButton, EndEventButton, EventFormButton)
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
        if message.author.bot:
            return
        if comment := COMMENT.match(message.content):
            # Members talking among themselves: no reply, nothing remembered.
            if message.guild is not None and self.allowed(message.author):
                await self.hide_comment(message, message.content[comment.end():].strip())
            return
        if (self.drawing() and not self.is_draw_request(message.content)
                and text_command(message.content)[0] != "!status"):
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
            if not rest:  # nothing typed: offer the form with menus
                await message.reply("Press to create an event. 点按钮创建活动。", view=end_view(EventFormButton()),
                                    mention_author=False)
                return
            fields = event_fields([p.strip() for p in re.split(r"[|｜]", rest)])
            if fields is None:
                await message.reply(EVENT_USAGE, mention_author=False)
                return
            text, view = await self.create_event(message.guild, message.author, *fields)
            await message.reply(text, view=view, mention_author=False)
            return
        if command == "!status":
            sent = await message.reply(await self.status_text(), mention_author=False)
            await self.keep_status_live(lambda text: sent.edit(content=text))
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
                await message.channel.send(content, poll=vote, view=end_view(EndPollButton(message.author.id)))
            return
        if command == "!ask":
            text = rest  # a plain question, answered like any chat message
        command = text.split(" ", 1)[0].lower()
        if self.images and command == "!style":
            await message.reply(self.change_style(message.channel.id, text[len(command):]),
                                mention_author=False)
            return
        if self.images and command in DM_COMMANDS:
            await self.private_picture(message, DM_COMMANDS[command], text[len(command):].strip())
            return
        if self.images and command == "!recall":
            text, files = self.recall(message.channel.id, message.author.id)
            await message.reply(text, files=files, mention_author=False)
            return
        if self.images and command == "!edit" and not text[len(command):].strip():
            await message.reply(EDIT_USAGE, mention_author=False)
            return
        if self.images and command in ("!draw", "!refine", "!edit") and text[len(command):].strip():
            source = source_type = None
            references = []
            if command == "!edit":
                pictures = [a for a in message.attachments if (a.content_type or "").split(";")[0] in IMAGE_TYPES]
                if not pictures:
                    await message.reply(NO_PICTURE, mention_author=False)
                    return
                source, source_type = await pictures[0].read(), pictures[0].content_type.split(";")[0]  # RAM only
                for picture in pictures[1:1 + MAX_REFERENCES]:  # more pictures: put their character into the first
                    references.append((await picture.read(), picture.content_type.split(";")[0]))

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
                                        command == "!refine", send, edit, source, source_type, references)
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
        # A level, not .disabled: child loggers like "ddgs.ddgs" ignore a disabled parent.
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    ChatBot(config).run(config["token"], root_logger=True)


if __name__ == "__main__":
    main()
