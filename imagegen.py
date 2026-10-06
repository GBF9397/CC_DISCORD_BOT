"""Image generation that takes turns with Gemma on the graphics card.

Gemma writes the Stable Diffusion prompt, then leaves the GPU (lms unload); ComfyUI
draws, then frees its model; Gemma comes back (lms load). No Discord code here.
"""
import asyncio
import logging
import random
import re
import shutil

import aiohttp

log = logging.getLogger("imagegen")

# Written by Gemma for every request, without the persona or unfiltered note.
PROMPT_WRITER = (
    "Turn the request below into one Stable Diffusion prompt: comma-separated English "
    "tags, subject first, then style, lighting and quality tags. Under 60 words. Reply "
    "with only the prompt. If the request is sexual and involves anyone who is or looks "
    "under 18, or is a sexual or degrading picture of a real person, reply only REFUSED.\n\n"
    "[Request]\n{request}"
)
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
    def __init__(self, brain, comfy_url, checkpoint, size=1024, context_length=16384,
                 timeout=600):
        self.brain = brain
        self.comfy_url = comfy_url.rstrip("/")
        self.checkpoint = checkpoint
        self.size = size
        self.context_length = context_length
        self.timeout = timeout
        self.drawing = False  # while True the bot stays silent

    async def draw(self, request, notice=None):
        """Returns PNG bytes. Raises DrawError. Gemma is always back online afterwards.
        notice: optional coroutine function run first, e.g. telling the channel."""
        self.drawing = True  # set before any await so a second request can't slip in
        try:
            if notice:
                await notice()
            async with self.brain._lock:  # wait for replies in progress, block new ones
                prompt = await self.brain.image_prompt(PROMPT_WRITER.format(request=request))
                if prompt is None:
                    raise DrawError("Sorry, my brain (LM Studio) is offline right now.")
                if prompt.startswith("REFUSED") or blocked(prompt):
                    raise DrawError("Sorry, I won't draw that.")
                log.info("Drawing; unloading %s from the GPU", self.brain.model)
                await self._lms("unload", self.brain.model)
                try:
                    return await self._comfy(prompt)
                finally:
                    await self._free_comfy()
                    log.info("Loading %s back onto the GPU", self.brain.model)
                    if not await self._lms("load", self.brain.model,
                                           "--context-length", str(self.context_length)):
                        log.error("Could not reload %s; load it in LM Studio by hand", self.brain.model)
        finally:
            self.drawing = False

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

    def _workflow(self, prompt):
        return {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.checkpoint}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": self.size, "height": self.size, "batch_size": 1}},
            "5": {"class_type": "KSampler", "inputs": {
                "seed": random.randrange(2**32), "steps": 25, "cfg": 6.0,
                "sampler_name": "euler_ancestral", "scheduler": "normal", "denoise": 1.0,
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            # PreviewImage writes to ComfyUI's temp folder, which ComfyUI empties on start.
            "7": {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}},
        }

    async def _comfy(self, prompt):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(f"{self.comfy_url}/prompt",
                                     json={"prompt": self._workflow(prompt)}) as r:
                    body = await r.json(content_type=None)
                    if r.status != 200 or "prompt_id" not in body:
                        log.error("ComfyUI rejected the job: %s", str(body)[:500])
                        raise DrawError("Sorry, the image generator rejected the job.")
                job = body["prompt_id"]
                for _ in range(self.timeout):
                    await asyncio.sleep(1)
                    async with http.get(f"{self.comfy_url}/history/{job}") as r:
                        history = await r.json(content_type=None)
                    if job in history:
                        break
                else:
                    raise DrawError("Sorry, the drawing took too long.")
                for output in history[job].get("outputs", {}).values():
                    for image in output.get("images", []):
                        async with http.get(f"{self.comfy_url}/view", params=image) as r:
                            if r.status == 200:
                                return await r.read()
                log.error("ComfyUI finished without an image: %s", str(history[job].get("status"))[:500])
                raise DrawError("Sorry, the drawing failed.")
        except aiohttp.ClientError as e:
            log.error("ComfyUI unreachable: %s", e)
            raise DrawError("Sorry, the image generator (ComfyUI) is offline.")

    async def _free_comfy(self):
        """Ask ComfyUI to drop its model from the GPU. Never raises."""
        try:
            async with aiohttp.ClientSession() as http:
                await http.post(f"{self.comfy_url}/free",
                                json={"unload_models": True, "free_memory": True})
        except aiohttp.ClientError as e:
            log.warning("Could not ask ComfyUI to free the GPU: %s", e)
