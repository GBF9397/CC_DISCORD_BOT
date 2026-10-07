"""Free DuckDuckGo web lookups so the model can answer with current information. Nothing is saved."""
import asyncio
import logging
import re

import aiohttp
from ddgs import DDGS

log = logging.getLogger("bot")

# Words that suggest the answer depends on up-to-date information.
_TIME_SENSITIVE = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|right now|current(ly)?|latest|recent(ly)?|news|"
    r"this (week|month|year)|price|weather|score|who won|20[2-9]\d)\b"
    r"|今天|昨天|明天|现在|目前|最新|最近|新闻|今年|本周|这周|价格|天气|比分|谁赢",
    re.IGNORECASE,
)


def needs_search(text):
    return bool(_TIME_SENSITIVE.search(text))


# Questions about facts (names, places, who, when...), for a character persona's quiet lookups.
_FACT_QUESTION = re.compile(
    r"\b(what|who|whose|where|which|when|how (many|much|old|long|tall))\b"
    r"|叫什么|什么名字|是什么|是谁|谁是|哪里|哪儿|哪个|哪位|哪一|多少|几岁|几个|什么时候|为什么|怎么回事",
    re.IGNORECASE,
)


def is_fact_question(text):
    return bool(_FACT_QUESTION.search(text))


def _search(query, max_results):
    return DDGS().text(query, max_results=max_results)


async def web_search(query, max_results=5):
    """Search results as one text block for the model, or "" if the lookup fails."""
    try:
        results = await asyncio.to_thread(_search, query, max_results)
    except Exception as e:
        log.warning("Web search failed: %s", type(e).__name__)  # the message can hold the query
        return ""
    return "\n\n".join(f"{r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}" for r in results)


PICTURE_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_PICTURE = 5_000_000  # bytes


def _images(query, max_results):
    return DDGS().images(query, safesearch="on", max_results=max_results)


async def image_search(query, max_results=3):
    """Pictures for the query as (bytes, mime type) pairs, held in RAM only; [] if the lookup fails."""
    try:
        results = await asyncio.to_thread(_images, query, max_results)
    except Exception as e:
        log.warning("Image search failed: %s", type(e).__name__)
        return []
    pictures = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        for r in results:
            for url in (r.get("image"), r.get("thumbnail")):  # the full picture, else the small one
                try:
                    async with http.get(url) as resp:
                        if resp.status == 200 and resp.content_type in PICTURE_TYPES \
                                and (resp.content_length or 0) <= MAX_PICTURE:
                            data = await resp.read()
                            if len(data) <= MAX_PICTURE:
                                pictures.append((data, resp.content_type))
                                break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
    return pictures
