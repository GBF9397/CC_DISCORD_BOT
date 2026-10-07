"""ComfyUI node for the Discord bot's /edit: reads the member's picture from the job itself.

The bot copies this file into ComfyUI/custom_nodes. ComfyUI's own LoadImage needs the picture
uploaded into its input folder, which saves it to disk; this keeps it in RAM only.
"""
import base64
import io

import numpy as np
import torch
from PIL import Image, ImageOps


class BotLoadImageBase64:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("STRING", {"multiline": False}),
                             "size": ("INT", {"default": 1024, "min": 256, "max": 4096})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "load"
    CATEGORY = "image"

    def load(self, image, size):
        picture = ImageOps.exif_transpose(Image.open(io.BytesIO(base64.b64decode(image)))).convert("RGB")
        # About size x size pixels in total, same shape, sides a multiple of 8 for the VAE.
        scale = (size * size / (picture.width * picture.height)) ** 0.5
        width, height = (max(8, round(side * scale / 8) * 8) for side in picture.size)
        picture = picture.resize((width, height), Image.LANCZOS)
        return (torch.from_numpy(np.asarray(picture, dtype=np.float32) / 255.0)[None],)


NODE_CLASS_MAPPINGS = {"BotLoadImageBase64": BotLoadImageBase64}
NODE_DISPLAY_NAME_MAPPINGS = {"BotLoadImageBase64": "Load Image (Discord bot, RAM only)"}
