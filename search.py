"""Free DuckDuckGo web lookups so the model can answer with current information. Nothing is saved."""
import asyncio
import logging
import re

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
