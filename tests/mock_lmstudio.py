"""A tiny stand-in for LM Studio's /v1/chat/completions endpoint."""
import asyncio

from aiohttp import web


class MockLMStudio:
    def __init__(self, delay=0.0, reply=None):
        self.delay = delay
        self.reply = reply
        self.requests = []
        self.active = 0
        self.max_active = 0

    async def handle(self, request):
        body = await request.json()
        self.requests.append(body)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        last = body["messages"][-1]["content"]
        if isinstance(last, list):  # text + image parts
            last = last[0]["text"]
        last = last.split("\n\n[Length limit", 1)[0]  # echo what the member wrote
        content = self.reply if self.reply is not None else f"echo: {last}"
        if isinstance(content, list):  # one reply per request, in order
            content = content.pop(0)
        return web.json_response({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
            "model": body["model"],
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content,
                            "reasoning_content": "SECRET REASONING"},
            }],
        })

    async def start(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    async def stop(self):
        await self.runner.cleanup()
