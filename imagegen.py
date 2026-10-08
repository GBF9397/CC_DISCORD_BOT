"""Image generation that takes turns with Gemma on the graphics card.

Gemma writes the Stable Diffusion prompt, then leaves the GPU (lms unload); ComfyUI
draws, then frees its model; Gemma comes back (lms load). No Discord code here.
"""
import asyncio
import base64
import filecmp
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from statistics import fmean
from dataclasses import dataclass

import aiohttp
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

import core as brain_core
from core import TooSlow

from search import image_search, web_search

log = logging.getLogger("imagegen")

# Written by Gemma for every request, without the persona or unfiltered note.
PROMPT_WRITER = (
    "Turn the request below into one Stable Diffusion prompt: comma-separated English "
    "tags, subject first, then style, lighting and quality tags. Under 60 words. {style} "
    "Reply with only the prompt, then on the same line AVOID: and any tags the picture must "
    "not have, or nothing after AVOID:. If the request is sexual and involves anyone who is or "
    "looks under 18, or is a sexual or degrading picture of a real person, reply only REFUSED. "
    "If it names a specific character, person, place, product or artwork (a proper name, "
    "even one you think you know), reply only SEARCH: <its name and series>.\n\n"
    "[Request]\n{request}"
)
# Drawing-model vocabulary for /refine and /edit, so a requested change lands where the member meant.
TAG_WORDS = (
    "Use the drawing model's own words: hair length from shortest is very short hair, short hair, "
    "medium hair (to the shoulders), long hair (past the shoulders), very long hair (to the "
    "waist); hairstyle tags like bob cut or pixie cut also fix the length, so they go when the "
    "length changes; for a bit longer, shorter, bigger or darker move one step, not to the extreme. "
    "Write color codes like #98FB98 as color names (pale green hair). "
)
SAFETY = (
    "If the change makes it sexual and involving anyone who is or looks under 18, or a sexual "
    "or degrading picture of a real person, reply only REFUSED. If the change names a specific "
    "character, person, place, product or artwork (a proper name, even one you think you know), "
    "reply only SEARCH: <its name and series>."
)
# How big a change is decides how much of the old picture is redrawn (REFINE_STRENGTH).
SIZE_RULES = (
    "SIZE: small for expression, lighting or the color of a small thing; medium for hairstyle, eye "
    "color, clothes or background; big for hair color, pose, framing, or adding or removing someone; "
    "new to draw it again from scratch. When the change goes against how a named character normally "
    "looks (like another hair color), leave out the character's name and series tags and describe the "
    "looks instead, since the drawing model always draws a named character with their usual looks, "
    "and make it at least big. When the change gives a big area a new color (hair, clothes, skin or "
    "background), also write RECOLOR at the very end. "
)
# For /edit: the change wins over what the uploaded picture shows.
CHANGE_RULES = (
    "The change always wins over the picture: leave out everything it contradicts, put the changed "
    "tags first with weight 1.3, like (medium hair:1.3), and keep everything else. " + TAG_WORDS +
    "Reply with only the new prompt, comma-separated English tags, under 60 words, {style} "
    "then on the same line AVOID: and the tags the change got rid of (like AVOID: short hair, backlighting), "
    "then SIZE: and how big the change is. " + SIZE_RULES + SAFETY
)
# For /refine: Gemma lists only the edits and the code applies them, so every tag the member
# didn't mention stays exactly as it was, however many rounds they refine.
REFINER = (
    "Here are the tags of a Stable Diffusion picture, which is attached:\n[Tags]\n{prompt}\n\n"
    "Change it as asked below. Do not rewrite the tags: reply with only the edits on one line, like "
    "ADD: medium hair REMOVE: short hair, bob cut AVOID: SIZE: medium\n"
    "ADD: the new tags the change needs. REMOVE: tags from the list above, copied exactly, that the "
    "change replaces or contradicts. AVOID: things the change gets rid of that are not in the list "
    "(no backlight means AVOID: backlighting). " + SIZE_RULES.replace("leave out", "REMOVE") +
    "Every tag you don't remove stays exactly as it is, so only touch "
    "what the change asks for. " + TAG_WORDS + "{style} " + SAFETY + "\n\n[Change]\n{request}"
)
# For /edit: the member's uploaded picture is redrawn by the drawing model with the change.
EDITOR = (
    "The attached picture will be redrawn by Stable Diffusion with the change below. Write the "
    "prompt for the picture as it should look after the change: describe what it shows (subject, "
    "colors, fur or hair, clothes, pose, background) plus the change. "
    + CHANGE_RULES + "\n\n[Change]\n{request}"
)
# For /edit with a second picture: that picture's character takes over the first one.
SWAP_NOTE = (
    "Picture 1 is the one being redrawn. Picture 2 shows a character to put into picture 1: keep "
    "picture 1's clothes, pose, framing and background, but give the person picture 2's looks (hair "
    "colour and style, eye colour, face, skin, horns, ears, hair ornaments), and name the character if "
    "you know who it is. Keep picture 2's character name and series tags even though the outfit is new: "
    "their usual looks are exactly what is wanted, so the rule about leaving names out doesn't apply, "
    "and don't write RECOLOR. "
)
# Tells Gemma which drawing model will read the prompt.
STYLE_HINTS = {
    "anime": "The drawing model is an anime model: use Danbooru tags.",
    "realistic": ("The drawing model is a photo model: describe a real photograph and add photo, "
                  "realistic, raw photo, film grain; never write anime, manga, illustration, cel "
                  "shading or Danbooru quality tags like masterpiece; a character from anime or games "
                  "is a real person in cosplay."),
}
# Added after a SEARCH: reply, so Gemma describes the look for ComfyUI, which has no internet.
LOOKUP = ("\n\nNow reply as asked above (do not reply SEARCH again). Draw only the subject the request "
          "names, never a game screen, menu, logo or character list. First read the web text below to "
          "learn how it looks. Pictures found on the web are attached after any earlier picture; some may "
          "show other characters from the same series, so use only the pictures that match the web text "
          "and each other, ignore the rest, and if none match go by the text. For a character start with "
          "1girl or 1boy, solo, then describe exactly how it looks: hair color and style, streaks, eye "
          "color, each piece of clothing with its colors, accessories and weapon. The drawing model only "
          "knows characters that came out before 2025: add the character's name and series as tags only "
          "if the web text shows it came out before 2025. Otherwise leave out its name and series "
          "entirely, or the drawing model swaps in another character it knows from that series.\n\n"
          "[What the web says about it]\n{results}")
TOO_SLOW = ("Sorry, Gemma took too long looking at that (over 5 minutes), so I stopped. Try again, maybe with "
            "fewer or simpler pictures. Gemma 看太久了（超过 5 分钟），已停止，请再试一次。")
COUNTDOWN_EVERY = 15  # seconds between "giving up in ..." updates while Gemma writes a prompt
MAX_PICTURE_SIDE = 1024  # uploads are shrunk to this before Gemma and ComfyUI see them


def fade_colors(data):
    """For a recolor: most of the old color taken out and the outlines drawn darker, so the drawing
    model paints the new colors instead of keeping the old ones, yet still sees where hair ends and
    a similar-looking background begins. A picture Pillow can't read is left as it is. RAM only."""
    try:
        picture = Image.open(io.BytesIO(data)).convert("RGB")
    except (OSError, ValueError):
        return data
    faded = ImageEnhance.Color(picture).enhance(KEEP_COLOR)
    edges = picture.filter(ImageFilter.FIND_EDGES).convert("L")  # color edges too, not just brightness
    shade = edges.point(lambda v: 255 - min(200, v * 3))  # 255 = untouched, darker along the edges
    out = io.BytesIO()
    ImageChops.multiply(faded, Image.merge("RGB", (shade, shade, shade))).save(out, "PNG")
    return out.getvalue()


def shrink(data, mime):
    """Any picture as (PNG, mime) at most MAX_PICTURE_SIDE wide or tall, upright, first frame only;
    one Pillow can't read is passed on as it is. RAM only."""
    try:
        picture = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (OSError, ValueError):
        return data, mime
    picture.thumbnail((MAX_PICTURE_SIDE, MAX_PICTURE_SIDE))
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue(), "image/png"


NOTHING_TO_REFINE = ("You have nothing to refine yet in this channel. Draw one first with /draw or !draw. "
                     "你在这个频道还没有画过图，先用 /draw 或 !draw 画一张。")
NEGATIVE = "lowres, bad anatomy, bad hands, extra fingers, blurry, watermark, text, signature"
# Added to NEGATIVE for that style, so the photo model doesn't drift into drawing.
STYLE_NEGATIVE = {"realistic": "anime, manga, cartoon, illustration, drawing, painting, 2d, cel shading, cgi, 3d render"}
# Sampler settings per style; Juggernaut XL (the realistic model) is made for DPM++ 2M Karras at a low CFG.
STYLE_SAMPLER = {"realistic": {"sampler_name": "dpmpp_2m", "scheduler": "karras", "steps": 30, "cfg": 4.5}}
KEEP_VERSIONS = 5  # pictures per member per channel that /recall can go back to, RAM only
EDIT_STRENGTH = 0.6  # how much /edit may change the uploaded picture (1.0 = draw from scratch)
SWAP_STRENGTH = 0.75  # more for a new character, since hair and face have to change
NODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comfy_node.py")
EDIT_NODE = "BotLoadImageBase64"  # in comfy_node.py, copied into ComfyUI's custom_nodes

SIZE_WORD = re.compile(r"\bSIZE:\s*(\w+)")
RECOLOR_WORD = re.compile(r"\s*\bRECOLOR\b")
KEEP_COLOR = 0.35  # share of the old colors left in a picture before a recolor
# Fading already frees the color, so a recolor redraws less and the shapes (horns, pose) stay.
RECOLOR_STRENGTH = 0.6
# With SD_CONTROLNET (a canny ControlNet in ComfyUI/models/controlnet), a recolor follows the picture's
# outlines instead: the shapes are locked, so it can redraw nearly all of it and the new colour wins.
RECOLOR_CONTROLNET_STRENGTH = 0.9
CONTROLNET_WEIGHT, CONTROLNET_UNTIL = 0.8, 0.8  # how hard the outlines hold, and for how much of the drawing
EDIT_WORDS = re.compile(r"\b(ADD|REMOVE|AVOID|SIZE):\s*(.*?)(?=\s*\b(?:ADD|REMOVE|AVOID|SIZE):|$)")
WEIGHTED = re.compile(r"^\((.*?)(?::[\d.]+)?\)$")
# How much of the last picture a /refine redraws (1.0 = draw again with the same seed). Every asked-for
# change redraws over half, or the old picture wins and nothing seems to change.
REFINE_STRENGTH = {"small": 0.55, "medium": 0.6, "big": 0.75, "new": 1.0}
MAX_AVOID = 12  # negative tags carried from round to round
TIMINGS_KEPT = 5  # recent seconds per step, for the time estimate; RAM only


def split_tags(text):
    return [t.strip() for t in text.split(",") if t.strip(" .")]


def core(tag):
    """'(medium hair:1.3)' -> 'medium hair', to compare tags whatever their weight."""
    return WEIGHTED.sub(r"\1", tag.strip()).strip().lower()


# Second line of defence in case the prompt writer lets one through.
SEXUAL = re.compile(r"\b(nsfw|nude|naked|nudity|sex|sexual|porn|hentai|lewd|explicit|topless|"
                    r"genitals?|breasts?|nipples?)\b", re.IGNORECASE)
MINOR = re.compile(r"\b(child|children|kid|kids|minor|underage|loli|lolita|shota|toddler|baby|"
                   r"teen|teenager|preteen|schoolgirl|schoolboy|young girl|young boy|little girl|"
                   r"little boy|\d{1,2} ?(yo|years? old))\b", re.IGNORECASE)


def blocked(prompt):
    return bool(SEXUAL.search(prompt) and MINOR.search(prompt))


class DrawError(Exception):
    """Something failed; the message is safe to show members."""


@dataclass
class Job:
    """One queued picture. source is the picture /edit redraws; RAM only."""
    request: str
    channel_id: int
    user_id: int
    deliver: object
    refine: bool = False
    checkpoint: str = None
    progress: object = None
    source: bytes = None
    style: str = None  # the channel's style when it was asked, see _refine_checkpoint
    prompt: str = ""
    tags: list = None  # the prompt's tags, without this round's extra weight
    negative: list = None
    seed: int = 0
    strength: float = 1.0  # share of the source picture redrawn; 1.0 when there is none
    png: bytes = None  # the finished picture, kept for /refine and /recall
    source_type: str = "image/png"
    reference: bytes = None  # /edit's second picture: whose looks to use; RAM only, dropped once read
    reference_type: str = "image/png"
    recolor: bool = False  # a big area gets a new color: the old colors are faded before redrawing
    controlnet: bool = False  # a recolor drawn along the source's outlines (SD_CONTROLNET)


class ImageMaker:
    def __init__(self, brain, comfy_url, checkpoints, size=1024, context_length=16384,
                 timeout=600, comfy_dir="", startup_wait=180, controlnet=""):
        self.brain = brain
        self.comfy_url = comfy_url.rstrip("/")
        self.checkpoints = checkpoints  # style -> checkpoint file; the first is the default
        self.controlnet = controlnet  # ControlNet model file for recolors, "" = none
        self.size = size
        self.context_length = context_length
        self.timeout = timeout
        self.comfy_dir = comfy_dir  # ComfyUI_windows_portable folder, to start it when it's off
        self.startup_wait = startup_wait
        self.drawing = False  # while True the bot only takes picture requests
        self.queue = deque()  # Jobs, RAM only
        self.waiting = set()  # members with a picture queued or being drawn
        self.timings = defaultdict(lambda: deque(maxlen=TIMINGS_KEPT))  # step -> recent seconds, RAM only
        # (channel_id, user_id) -> that member's last KEEP_VERSIONS finished Jobs with their png, RAM only.
        # Per member, so one member's /refine never builds on another member's picture.
        self.versions = {}
        self.base = {}  # (channel_id, user_id) -> index in versions the next /refine builds on
        self.styles = {}  # channel_id -> style name, RAM only
        self._install_node()

    def style(self, channel_id):
        return self.styles.get(channel_id, next(iter(self.checkpoints)))

    def set_style(self, channel_id, name):
        """Returns the style name, or None if unknown."""
        name = name.strip().lower()
        if name not in self.checkpoints:
            return None
        self.styles[channel_id] = name
        return name

    def style_of(self, checkpoint):
        return next((name for name, file in self.checkpoints.items() if file == checkpoint), None)

    def history(self, channel_id, user_id):
        """The member's pictures here, oldest first, and the index /refine builds on."""
        key = (channel_id, user_id)
        return [job.png for job in self.versions.get(key, [])], self.base.get(key)

    def recall(self, channel_id, user_id, number):
        """Make picture `number` (1 = oldest) the one the next /refine builds on.
        Returns its PNG, or None if there is no such picture. The others are kept."""
        key = (channel_id, user_id)
        kept = self.versions.get(key, [])
        if not 1 <= number <= len(kept):
            return None
        self.base[key] = number - 1
        return kept[number - 1].png

    def _keep(self, job, png):
        key = (job.channel_id, job.user_id)
        job.png = png
        kept = self.versions.setdefault(key, [])
        kept.append(job)
        del kept[:-KEEP_VERSIONS]
        self.base[key] = len(kept) - 1

    def submit(self, request, channel_id, user_id, deliver, refine=False, progress=None, source=None,
               source_type="image/png", reference=None, reference_type="image/png", style_channel=None):
        """Queue a picture. Returns how many pictures are ahead (0 = starting now), or None if
        this member already has one waiting or being drawn. Raises DrawError if there is
        nothing to refine. deliver(png, error) is awaited
        with the PNG bytes or an error text once this picture is done; progress(percent),
        if given, is awaited every 10% while ComfyUI draws it, and progress(text) with a countdown
        while Gemma writes its prompt. source: picture bytes to
        redraw with the change (/edit), kept in RAM only; reference: a second picture whose
        character takes over the first. channel_id keys the member's picture history; style_channel,
        if given, is the channel whose drawing style to use (private pictures have their own history)."""
        style_channel = style_channel or channel_id
        if user_id in self.waiting:
            return None
        if refine and (channel_id, user_id) not in self.versions:
            raise DrawError(NOTHING_TO_REFINE)
        ahead = len(self.waiting)
        self.waiting.add(user_id)
        # The style is fixed now, so a later /drawstyle doesn't change queued pictures.
        # A request may start with a style name: "realistic a sports car".
        first, _, rest = request.partition(" ")
        style, checkpoint = self.style(style_channel), None  # a refine without a style name decides later
        if first.lower() in self.checkpoints and rest.strip():
            style, request = first.lower(), rest.strip()
            checkpoint = self.checkpoints[style]
        elif not refine:
            checkpoint = self.checkpoints[style]
        self.queue.append(Job(request, channel_id, user_id, deliver, refine, checkpoint, progress, source,
                              self.style(style_channel), source_type=source_type, reference=reference,
                              reference_type=reference_type))
        if not self.drawing:
            self.drawing = True  # set before any await so the bot goes silent at once
            self._worker = asyncio.create_task(self._work())
        return ahead

    async def _work(self):
        """Draw every queued picture. Gemma stays offline until the queue is empty."""
        try:
            while self.queue:
                async with self.brain._lock:  # wait for replies in progress, block new ones
                    jobs = await self._write_prompts()
                    if jobs:
                        await self._draw_all(jobs)
        except Exception:
            log.exception("Drawing failed")
            for job in self.queue:
                await self._deliver(job.deliver, job.user_id, None, "Sorry, something went wrong while drawing.")
            self.queue.clear()
        finally:
            self.waiting.clear()
            self.drawing = False

    @staticmethod
    def _kind(job):
        return "refine" if job.refine else "edit" if job.source else "draw"

    def estimate(self):
        """Seconds until every queued picture is done, from this PC's last few pictures; None until one
        was timed. Gemma's prompt time differs by kind (pictures to look at take longer)."""
        t = self.timings
        if not t["draw"]:
            return None
        draw, any_prompt = fmean(t["draw"]), fmean(t["prompt"]) if t["prompt"] else 0.0
        being_drawn = len(self.waiting) - len(self.queue)  # prompts written, still to draw
        return (fmean(t["switch"]) if t["switch"] else 0.0) + being_drawn * draw + sum(
            fmean(t[f"prompt:{self._kind(j)}"]) if t[f"prompt:{self._kind(j)}"] else any_prompt for j in self.queue
        ) + len(self.queue) * draw

    async def _ask(self, job, instruction, images):
        """Gemma writes the prompt while the member's notice counts down to when the bot gives up."""
        async def countdown():
            deadline = time.monotonic() + brain_core.IMAGE_PROMPT_TIMEOUT
            while True:
                await asyncio.sleep(COUNTDOWN_EVERY)
                minutes, seconds = divmod(max(0, round(deadline - time.monotonic())), 60)
                try:
                    await job.progress(f"🧠 Gemma is writing the prompt, giving up in {minutes} min {seconds} sec. "
                                       f"Gemma 正在写提示，最多再等 {minutes} 分 {seconds} 秒。")
                except Exception as e:
                    log.warning("Could not show the countdown: %s", type(e).__name__)
        ticker = asyncio.create_task(countdown()) if job.progress else None
        try:
            return await self.brain.image_prompt(instruction, images)
        finally:
            if ticker:
                ticker.cancel()

    def _refine_checkpoint(self, job, old):
        """A refine keeps its picture's model, unless the change starts with a style name or
        the channel switched style after the picture was drawn."""
        if job.checkpoint:
            return job.checkpoint
        if job.style != old.style:
            return self.checkpoints[job.style]
        return old.checkpoint

    async def _write_prompts(self):
        """Gemma (still loaded) writes the prompt for each queued picture."""
        jobs = []
        while self.queue:
            job = self.queue.popleft()
            images, swap = [], bool(job.reference)
            if job.refine:
                key = (job.channel_id, job.user_id)
                old = self.versions[key][self.base[key]]
                job.checkpoint = self._refine_checkpoint(job, old)
                job.seed = old.seed
                instruction = REFINER.format(prompt=old.prompt, request=job.request,
                                             style=STYLE_HINTS.get(self.style_of(job.checkpoint), ""))
                # Gemma sees the picture, so a refine keeps details the prompt never named.
                images = [(old.png, "image/png")]
            elif job.source:
                # Big uploads slow Gemma down a lot; both Gemma and ComfyUI get the small copy.
                job.source, job.source_type = await asyncio.to_thread(shrink, job.source, job.source_type)
                job.seed, job.strength = random.randrange(2**32), SWAP_STRENGTH if job.reference else EDIT_STRENGTH
                instruction = (SWAP_NOTE if job.reference else "") + EDITOR.format(
                    request=job.request, style=STYLE_HINTS.get(self.style_of(job.checkpoint), ""))
                images = [(job.source, job.source_type)]
                if job.reference:
                    images.append(await asyncio.to_thread(shrink, job.reference, job.reference_type))
                    job.reference = None  # only Gemma needs it; ComfyUI redraws picture 1
            else:
                job.seed = random.randrange(2**32)
                instruction = PROMPT_WRITER.format(request=job.request,
                                                   style=STYLE_HINTS.get(self.style_of(job.checkpoint), ""))
            started = time.monotonic()
            try:
                prompt = await self._ask(job, instruction, images)
                if prompt and prompt.startswith("SEARCH:"):  # one lookup per picture, results never stored
                    name = prompt[len("SEARCH:"):].strip()
                    results = await web_search(name + " character appearance hair outfit")
                    found = await image_search(name + " official art", max_results=5)
                    log.info("Looked up a named subject on the web (%d characters, %d pictures)", len(results), len(found))
                    found = [await asyncio.to_thread(shrink, data, mime) for data, mime in found]
                    prompt = await self._ask(job, instruction + LOOKUP.format(results=results or "(no results)"),
                                             images + found)
            except TooSlow:
                log.warning("Gemma took over %d s to write a picture prompt", brain_core.IMAGE_PROMPT_TIMEOUT)
                await self._deliver(job.deliver, job.user_id, None, TOO_SLOW)
                continue
            if prompt is not None:
                took = time.monotonic() - started
                self.timings["prompt"].append(took)
                self.timings[f"prompt:{self._kind(job)}"].append(took)
            if prompt is None:
                await self._deliver(job.deliver, job.user_id, None, "Sorry, my brain (LM Studio) is offline right now.")
            elif prompt.startswith("REFUSED"):
                await self._deliver(job.deliver, job.user_id, None, "Sorry, I won't draw that.")
            else:
                job.recolor = bool(RECOLOR_WORD.search(prompt))
                prompt = RECOLOR_WORD.sub("", prompt)
                if job.refine and EDIT_WORDS.search(prompt):
                    self._apply_edits(job, old, prompt)
                else:  # a whole prompt (a refine whose reply ignored the edit format starts over too)
                    size = SIZE_WORD.search(prompt)
                    text, _, avoid = SIZE_WORD.sub("", prompt).partition("AVOID:")
                    job.tags, job.negative = split_tags(text), split_tags(avoid)
                    job.prompt = ", ".join(job.tags)
                    if job.source and not job.refine and not swap:  # a one-picture /edit
                        size = size[1].lower() if size else "none"
                        job.strength = REFINE_STRENGTH.get(size, EDIT_STRENGTH)
                        log.info("Edit: SIZE %s, redrawing %d%%", size, job.strength * 100)
                if blocked(job.prompt):
                    await self._deliver(job.deliver, job.user_id, None, "Sorry, I won't draw that.")
                    continue
                if job.recolor and job.source and job.strength < 1 and not swap:  # swaps work better unfaded
                    job.strength = min(job.strength, RECOLOR_STRENGTH)
                    log.info("Recolor: fading the old colors before redrawing, redrawing %d%%", job.strength * 100)
                    job.source = await asyncio.to_thread(fade_colors, job.source)
                jobs.append(job)
        return jobs

    @staticmethod
    def _apply_edits(job, old, reply):
        """Apply Gemma's ADD/REMOVE/AVOID/SIZE edits to the old picture's tags. Tags nobody
        mentioned are copied unchanged, and the picture is redrawn from the old one only as much
        as the change needs, so the parts that were fine stay fine."""
        edits = {key: value for key, value in EDIT_WORDS.findall(reply)}
        add = [core(t) for t in split_tags(edits.get("ADD", ""))]
        remove = split_tags(edits.get("REMOVE", ""))
        gone = {core(t) for t in remove} | set(add)
        kept = [t for t in old.tags if core(t) not in gone]
        # After the subject (1girl), before the rest. Tags nobody mentioned keep their exact text and
        # weight, so an earlier change like (red hair:1.3) isn't pulled back toward the old picture.
        job.tags = kept[:1] + [f"({t}:1.3)" for t in add] + kept[1:]
        job.prompt = ", ".join(job.tags)
        avoid = [core(t) for t in remove + split_tags(edits.get("AVOID", ""))] + old.negative
        job.negative = list(dict.fromkeys(t for t in avoid if t not in add))[:MAX_AVOID]
        size = (edits.get("SIZE", "").split() or ["medium"])[0].lower().strip(" .,")
        job.strength = REFINE_STRENGTH.get(size, REFINE_STRENGTH["medium"])
        log.info("Refine: SIZE %s, redrawing %d%%, %d tags added, %d removed", size, job.strength * 100, len(add),
                 len(remove))
        if job.strength < 1:
            job.source = old.png  # redraw part of the last picture instead of starting over

    async def _draw_all(self, jobs):
        started = time.monotonic()
        await self._start_comfy()
        log.info("Drawing %d picture(s); unloading %s from the GPU", len(jobs), self.brain.model)
        await self._lms("unload", self.brain.model)
        switch = time.monotonic() - started
        try:
            for job in jobs:
                try:
                    if job.recolor and job.source and self.controlnet and await self._has_controlnet():
                        job.controlnet, job.strength = True, RECOLOR_CONTROLNET_STRENGTH
                        log.info("Recolor: following the outlines with ControlNet, redrawing %d%%", job.strength * 100)
                    if job.source and not await self._has_edit_node():
                        if not job.refine:
                            raise DrawError("Sorry, /edit needs ComfyUI restarted once. Close ComfyUI and "
                                            "I'll start it again. /edit 需要先重启一次 ComfyUI。")
                        job.source, job.strength = None, 1.0  # a refine draws again with the same seed
                    started = time.monotonic()
                    png = await self._comfy(job, job.progress)
                    self.timings["draw"].append(time.monotonic() - started)
                except DrawError as e:
                    await self._deliver(job.deliver, job.user_id, None, str(e))
                else:
                    self._keep(job, png)
                    await self._deliver(job.deliver, job.user_id, png, None)
        finally:
            await self._free_comfy()
            log.info("Loading %s back onto the GPU", self.brain.model)
            started = time.monotonic()
            if not await self._lms("load", self.brain.model,
                                   "--context-length", str(self.context_length)):
                log.error("Could not reload %s; load it in LM Studio by hand", self.brain.model)
            self.timings["switch"].append(switch + time.monotonic() - started)

    async def _deliver(self, deliver, user_id, png, error):
        """Hand back one result; the member may then queue another picture."""
        self.waiting.discard(user_id)
        try:
            await deliver(png, error)
        except Exception:
            log.exception("Could not post a picture")

    async def _lms(self, *args):
        """Runs the LM Studio CLI; returns True on success. Never raises."""
        lms = shutil.which("lms")
        if lms is None:
            log.error("lms (LM Studio CLI) not found on PATH")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                lms, *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 300)
        except (OSError, asyncio.TimeoutError) as e:
            log.error("lms %s failed: %s", args[0], e)
            return False
        if proc.returncode:
            log.error("lms %s failed: %s", args[0], out.decode(errors="replace").strip()[-300:])
        return proc.returncode == 0

    def _workflow(self, job):
        negative = ", ".join(n for n in (NEGATIVE, STYLE_NEGATIVE.get(self.style_of(job.checkpoint), ""),
                                         ", ".join(job.negative)) if n)
        flow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": job.checkpoint}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": job.prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": self.size, "height": self.size, "batch_size": 1}},
            "5": {"class_type": "KSampler", "inputs": {
                "seed": job.seed, "steps": 25, "cfg": 6.0,
                "sampler_name": "euler_ancestral", "scheduler": "normal", "denoise": 1.0,
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            # SaveImageWebsocket (ships with ComfyUI) sends the PNG over the websocket,
            # so the picture is never written to disk, only held in RAM.
            "7": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["6", 0]}},
        }
        flow["5"]["inputs"].update(STYLE_SAMPLER.get(self.style_of(job.checkpoint), {}))
        if job.source:
            # /edit and /refine: the picture goes inside the job, not through ComfyUI's upload (which
            # saves a file), and is redrawn only partly so it keeps its shape.
            flow["8"] = {"class_type": EDIT_NODE, "inputs": {
                "image": base64.b64encode(job.source).decode(), "size": self.size}}
            flow["4"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["8", 0], "vae": ["1", 2]}}
            flow["5"]["inputs"]["denoise"] = job.strength
        if job.controlnet:  # the upload's outlines hold the shapes while the colours are redrawn
            flow["20"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": self.controlnet}}
            flow["21"] = {"class_type": "Canny", "inputs": {"image": ["8", 0], "low_threshold": 0.3,
                                                            "high_threshold": 0.7}}
            flow["22"] = {"class_type": "ControlNetApplyAdvanced", "inputs": {
                "positive": ["2", 0], "negative": ["3", 0], "control_net": ["20", 0], "image": ["21", 0],
                "strength": CONTROLNET_WEIGHT, "start_percent": 0.0, "end_percent": CONTROLNET_UNTIL}}
            flow["5"]["inputs"]["positive"], flow["5"]["inputs"]["negative"] = ["22", 0], ["22", 1]
        return flow

    async def _comfy(self, job, progress=None):
        client = uuid.uuid4().hex
        ws_url = self.comfy_url.replace("http", "ws", 1) + f"/ws?clientId={client}"
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(ws_url, max_msg_size=0) as ws:
                async with http.post(f"{self.comfy_url}/prompt", json={
                        "prompt": self._workflow(job), "client_id": client}) as r:
                    body = await r.json(content_type=None)
                    if r.status != 200 or "prompt_id" not in body:
                        # Only the error type: details can echo the prompt, and nothing members wrote is logged.
                        log.error("ComfyUI rejected the job: %s", str(body.get("error", {}).get("type"))[:100])
                        raise DrawError("Sorry, the image generator rejected the job.")
                return await asyncio.wait_for(self._receive(ws, body["prompt_id"], progress), self.timeout)
        except asyncio.TimeoutError:
            raise DrawError("Sorry, the drawing took too long.")
        except aiohttp.ClientError as e:
            log.error("ComfyUI unreachable: %s", e)
            raise DrawError("Sorry, the image generator (ComfyUI) is offline.")

    async def _receive(self, ws, job, progress=None):
        """Collects the PNG the save node sends; binary messages from other nodes are previews."""
        node, png, shown = None, None, 0
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                if node == "7":
                    png = msg.data[8:]  # 8-byte header: message type, image format
            elif msg.type == aiohttp.WSMsgType.TEXT:
                event = json.loads(msg.data)
                data = event.get("data", {})
                if data.get("prompt_id") != job:
                    continue
                if event["type"] == "execution_error":
                    log.error("ComfyUI failed: %s", str(data.get("exception_message"))[:500])
                    raise DrawError("Sorry, the drawing failed.")
                if event["type"] == "execution_success":
                    break
                if event["type"] == "progress" and progress and data.get("max"):
                    percent = 100 * data["value"] // data["max"]
                    if percent // 10 > shown // 10:  # every 10%, to stay under Discord's edit limit
                        shown = percent
                        try:
                            await progress(percent)
                        except Exception as e:
                            log.warning("Could not show drawing progress: %s", type(e).__name__)
                if event["type"] == "executing":
                    node = data.get("node")
                    if node is None:  # the whole job is done (older ComfyUI)
                        break
        if not png:
            raise DrawError("Sorry, the drawing failed.")
        return png

    async def _comfy_running(self):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{self.comfy_url}/system_stats",
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                    return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def _start_comfy(self):
        """Start ComfyUI in the background, with no window, if it isn't running. Never raises;
        if it still isn't up, the drawing reports ComfyUI as offline."""
        if not self.comfy_dir or await self._comfy_running():
            return
        log.info("ComfyUI is off; starting it")
        try:
            # Same as run_nvidia_gpu.bat, minus opening the browser.
            subprocess.Popen(
                [os.path.join(self.comfy_dir, "python_embeded", "python.exe"), "-s",
                 os.path.join("ComfyUI", "main.py"), "--windows-standalone-build", "--disable-auto-launch"],
                cwd=self.comfy_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except OSError as e:
            log.error("Could not start ComfyUI: %s", e)
            return
        for _ in range(self.startup_wait):
            await asyncio.sleep(1)
            if await self._comfy_running():
                return
        log.error("ComfyUI did not come up within %d seconds", self.startup_wait)

    def _install_node(self):
        """Put the /edit loader node into ComfyUI's custom_nodes; ComfyUI reads it when it starts."""
        if not self.comfy_dir:
            return
        target = os.path.join(self.comfy_dir, "ComfyUI", "custom_nodes", "discord_bot_image.py")
        try:
            if not (os.path.exists(target) and filecmp.cmp(NODE_FILE, target, shallow=False)):
                shutil.copyfile(NODE_FILE, target)
                log.info("Added the /edit node to ComfyUI's custom_nodes")
        except OSError as e:
            log.error("Could not add the /edit node to ComfyUI: %s", e)

    async def _has_edit_node(self):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{self.comfy_url}/object_info/{EDIT_NODE}",
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return r.status == 200 and EDIT_NODE in await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def _has_controlnet(self):
        """True if ComfyUI lists SD_CONTROLNET among its ControlNet models; otherwise recolors go without it."""
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{self.comfy_url}/object_info/ControlNetLoader",
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    info = await r.json(content_type=None) if r.status == 200 else {}
            names = info["ControlNetLoader"]["input"]["required"]["control_net_name"][0]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError):
            names = []
        if self.controlnet not in names:
            log.warning("SD_CONTROLNET %s is not in ComfyUI/models/controlnet; recoloring without it",
                        self.controlnet)
        return self.controlnet in names

    async def _free_comfy(self):
        """Ask ComfyUI to drop its model from the GPU and forget the job's prompt. Never raises."""
        try:
            async with aiohttp.ClientSession() as http:
                await http.post(f"{self.comfy_url}/free",
                                json={"unload_models": True, "free_memory": True})
                await http.post(f"{self.comfy_url}/history", json={"clear": True})
        except aiohttp.ClientError as e:
            log.warning("Could not ask ComfyUI to free the GPU: %s", e)
