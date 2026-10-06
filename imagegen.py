"""Image generation that takes turns with Gemma on the graphics card.

Gemma writes the Stable Diffusion prompt, then leaves the GPU (lms unload); ComfyUI
draws, then frees its model; Gemma comes back (lms load). No Discord code here.
"""
import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
from collections import deque

import aiohttp

from search import web_search

log = logging.getLogger("imagegen")

# Written by Gemma for every request, without the persona or unfiltered note.
PROMPT_WRITER = (
    "Turn the request below into one Stable Diffusion prompt: comma-separated English "
    "tags, subject first, then style, lighting and quality tags. Under 60 words. Reply "
    "with only the prompt. If the request is sexual and involves anyone who is or looks "
    "under 18, or is a sexual or degrading picture of a real person, reply only REFUSED. "
    "If it names a specific character, person, place, product or artwork (a proper name, "
    "even one you think you know), reply only SEARCH: <name, its series, appearance>.\n\n"
    "[Request]\n{request}"
)
# For /refine: change the channel's last prompt; the same seed keeps the overall look.
REFINER = (
    "Here is a Stable Diffusion prompt: {prompt}\n\nChange it as asked below and keep "
    "everything else the same. If the picture drawn from it is attached, first make the "
    "prompt describe exactly what it shows (subject, colors, fur or hair, clothes, pose, "
    "background) so those stay the same. Reply with only the new prompt, comma-separated English "
    "tags, under 60 words. If the change makes it sexual and involving anyone who is or "
    "looks under 18, or a sexual or degrading picture of a real person, reply only REFUSED. "
    "If the change names a specific character, person, place, product or artwork (a proper "
    "name, even one you think you know), reply only SEARCH: <name, its series, appearance>."
    "\n\n[Change]\n{request}"
)
# Added after a SEARCH: reply, so Gemma describes the look for ComfyUI, which has no internet.
LOOKUP = ("\n\n[What the web says about it; describe its look from this, do not reply SEARCH again]\n"
          "{results}")
NOTHING_TO_REFINE = "Nothing to refine yet in this channel. Draw one first with /draw or !draw."
NEGATIVE = "lowres, bad anatomy, bad hands, extra fingers, blurry, watermark, text, signature"

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


class ImageMaker:
    def __init__(self, brain, comfy_url, checkpoints, size=1024, context_length=16384,
                 timeout=600, comfy_dir="", startup_wait=180):
        self.brain = brain
        self.comfy_url = comfy_url.rstrip("/")
        self.checkpoints = checkpoints  # style -> checkpoint file; the first is the default
        self.size = size
        self.context_length = context_length
        self.timeout = timeout
        self.comfy_dir = comfy_dir  # ComfyUI_windows_portable folder, to start it when it's off
        self.startup_wait = startup_wait
        self.drawing = False  # while True the bot only takes picture requests
        self.queue = deque()  # (request, channel_id, user_id, deliver, refine, checkpoint), RAM only
        self.waiting = set()  # members with a picture queued or being drawn
        self.last = {}  # channel_id -> (prompt, seed, checkpoint) of its last picture, RAM only
        self.styles = {}  # channel_id -> style name, RAM only
        self.pictures = {}  # channel_id -> PNG of its last picture, shown to Gemma on refine, RAM only

    def style(self, channel_id):
        return self.styles.get(channel_id, next(iter(self.checkpoints)))

    def set_style(self, channel_id, name):
        """Returns the style name, or None if unknown."""
        name = name.strip().lower()
        if name not in self.checkpoints:
            return None
        self.styles[channel_id] = name
        return name

    def submit(self, request, channel_id, user_id, deliver, refine=False):
        """Queue a picture. Returns how many pictures are ahead (0 = starting now), or None if
        this member already has one waiting or being drawn. Raises DrawError if there is
        nothing to refine. deliver(png, error) is awaited
        with the PNG bytes or an error text once this picture is done."""
        if user_id in self.waiting:
            return None
        if refine and channel_id not in self.last and not any(job[1] == channel_id for job in self.queue):
            raise DrawError(NOTHING_TO_REFINE)
        ahead = len(self.waiting)
        self.waiting.add(user_id)
        checkpoint = None  # a refine reuses its picture's model
        if not refine:
            # The style is fixed now, so a later /drawstyle doesn't change queued pictures.
            # A request may start with a style name: "realistic a sports car".
            first, _, rest = request.partition(" ")
            style = self.style(channel_id)
            if first.lower() in self.checkpoints and rest.strip():
                style, request = first.lower(), rest.strip()
            checkpoint = self.checkpoints[style]
        self.queue.append((request, channel_id, user_id, deliver, refine, checkpoint))
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
                await self._deliver(job[3], job[2], None, "Sorry, something went wrong while drawing.")
            self.queue.clear()
        finally:
            self.waiting.clear()
            self.drawing = False

    async def _write_prompts(self):
        """Gemma (still loaded) writes the prompt for each queued picture."""
        jobs = []
        while self.queue:
            request, channel_id, user_id, deliver, refine, checkpoint = self.queue.popleft()
            if refine and channel_id not in self.last:
                await self._deliver(deliver, user_id, None, NOTHING_TO_REFINE)
                continue
            if refine:
                old, seed, checkpoint = self.last[channel_id]
                instruction = REFINER.format(prompt=old, request=request)
            else:
                seed, instruction = random.randrange(2**32), PROMPT_WRITER.format(request=request)
            # Gemma sees the last picture, so a refine keeps details the prompt never named.
            image = self.pictures.get(channel_id) if refine else None
            prompt = await self.brain.image_prompt(instruction, image)
            if prompt and prompt.startswith("SEARCH:"):  # one lookup per picture, results never stored
                results = await web_search(prompt[len("SEARCH:"):].strip())
                log.info("Looked up a named subject on the web (%d characters found)", len(results))
                prompt = await self.brain.image_prompt(instruction + LOOKUP.format(results=results or "(no results)"),
                                                       image)
            if prompt is None:
                await self._deliver(deliver, user_id, None, "Sorry, my brain (LM Studio) is offline right now.")
            elif prompt.startswith("REFUSED") or blocked(prompt):
                await self._deliver(deliver, user_id, None, "Sorry, I won't draw that.")
            else:
                self.last[channel_id] = (prompt, seed, checkpoint)  # a queued /refine builds on it
                jobs.append((prompt, seed, checkpoint, channel_id, user_id, deliver))
        return jobs

    async def _draw_all(self, jobs):
        await self._start_comfy()
        log.info("Drawing %d picture(s); unloading %s from the GPU", len(jobs), self.brain.model)
        await self._lms("unload", self.brain.model)
        try:
            for prompt, seed, checkpoint, channel_id, user_id, deliver in jobs:
                try:
                    png = await self._comfy(prompt, seed, checkpoint)
                except DrawError as e:
                    await self._deliver(deliver, user_id, None, str(e))
                else:
                    self.pictures[channel_id] = png
                    await self._deliver(deliver, user_id, png, None)
        finally:
            await self._free_comfy()
            log.info("Loading %s back onto the GPU", self.brain.model)
            if not await self._lms("load", self.brain.model,
                                   "--context-length", str(self.context_length)):
                log.error("Could not reload %s; load it in LM Studio by hand", self.brain.model)

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

    def _workflow(self, prompt, seed, checkpoint):
        return {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": self.size, "height": self.size, "batch_size": 1}},
            "5": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": 25, "cfg": 6.0,
                "sampler_name": "euler_ancestral", "scheduler": "normal", "denoise": 1.0,
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            # SaveImageWebsocket (ships with ComfyUI) sends the PNG over the websocket,
            # so the picture is never written to disk, only held in RAM.
            "7": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["6", 0]}},
        }

    async def _comfy(self, prompt, seed, checkpoint):
        client = uuid.uuid4().hex
        ws_url = self.comfy_url.replace("http", "ws", 1) + f"/ws?clientId={client}"
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(ws_url, max_msg_size=0) as ws:
                async with http.post(f"{self.comfy_url}/prompt", json={
                        "prompt": self._workflow(prompt, seed, checkpoint), "client_id": client}) as r:
                    body = await r.json(content_type=None)
                    if r.status != 200 or "prompt_id" not in body:
                        # Only the error type: details can echo the prompt, and nothing members wrote is logged.
                        log.error("ComfyUI rejected the job: %s", str(body.get("error", {}).get("type"))[:100])
                        raise DrawError("Sorry, the image generator rejected the job.")
                return await asyncio.wait_for(self._receive(ws, body["prompt_id"]), self.timeout)
        except asyncio.TimeoutError:
            raise DrawError("Sorry, the drawing took too long.")
        except aiohttp.ClientError as e:
            log.error("ComfyUI unreachable: %s", e)
            raise DrawError("Sorry, the image generator (ComfyUI) is offline.")

    async def _receive(self, ws, job):
        """Collects the PNG the save node sends; binary messages from other nodes are previews."""
        node, png = None, None
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

    async def _free_comfy(self):
        """Ask ComfyUI to drop its model from the GPU and forget the job's prompt. Never raises."""
        try:
            async with aiohttp.ClientSession() as http:
                await http.post(f"{self.comfy_url}/free",
                                json={"unload_models": True, "free_memory": True})
                await http.post(f"{self.comfy_url}/history", json={"clear": True})
        except aiohttp.ClientError as e:
            log.warning("Could not ask ComfyUI to free the GPU: %s", e)
