"""假模型服务：OpenAI 兼容的 ``/v1/chat/completions``。

零成本、完全确定、可断言。回复内容由请求里的最后一条 user 消息决定：

- 含 ``[[echo:XXX]]``       → 回复 ``ECHO:XXX``（便于断言模型确实被调用）
- 含 ``[[slow:N]]``         → 延迟 N 秒再回（测超时/取消）
- 含 ``[[tool:名字]]``      → 返回一个 tool_call（将来测工具循环用）
- 其它                      → 回复 ``FAKE-LLM-OK``

启动::

    python -m tests.e2e.fake_llm --port 18180
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import web

__all__ = ["FakeLLM", "build_app"]

_ECHO_RE = re.compile(r"\[\[echo:(?P<tag>[^\]]*)\]\]")
_SLOW_RE = re.compile(r"\[\[slow:(?P<sec>[\d.]+)\]\]")
_TOOL_RE = re.compile(r"\[\[tool:(?P<name>[A-Za-z0-9_]+)\]\]")


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # 多模态
                return " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
    return ""


@dataclass
class FakeLLM:
    """记录所有请求，便于测试断言"模型被调用了 / 没被调用"。"""

    host: str = "127.0.0.1"
    port: int = 18180
    requests: list[dict] = field(default_factory=list)
    _runner: web.AppRunner | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        app = build_app(self)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def reset(self) -> None:
        self.requests.clear()


def _reply_text(prompt: str) -> str:
    if m := _ECHO_RE.search(prompt):
        return f"ECHO:{m.group('tag')}"
    return "FAKE-LLM-OK"


def _sse_chunk(text: str, model: str, first: bool) -> str:
    delta = {"role": "assistant", "content": text} if first else {"content": text}
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_app(state: FakeLLM) -> web.Application:
    app = web.Application()

    async def list_models(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": "fake-model",
                        "object": "model",
                        "created": 0,
                        "owned_by": "local",
                    }
                ],
            }
        )

    async def chat_completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        state.requests.append(body)

        messages = body.get("messages", [])
        prompt = _last_user_text(messages)
        model = body.get("model") or "fake-model"

        if m := _SLOW_RE.search(prompt):
            await asyncio.sleep(float(m.group("sec")))

        # 工具调用分支（当前仅用于占位，后续测 Worker 工具循环时启用）
        tool_name = None
        if m := _TOOL_RE.search(prompt):
            tool_name = m.group("name")

        if body.get("stream"):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await resp.prepare(request)
            if tool_name:
                chunk = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_{uuid.uuid4().hex[:8]}",
                                        "type": "function",
                                        "function": {"name": tool_name, "arguments": "{}"},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            else:
                await resp.write(_sse_chunk(_reply_text(prompt), model, True).encode())
            done = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            await resp.write(f"data: {json.dumps(done)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        text = _reply_text(prompt)
        if tool_name:
            message: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": text}
            finish = "stop"

        return web.json_response(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 16,
                    "completion_tokens": 8,
                    "total_tokens": 24,
                },
            }
        )

    app.router.add_get("/v1/models", list_models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18180)
    args = parser.parse_args()

    llm = FakeLLM(host=args.host, port=args.port)
    await llm.start()
    print(f"fake-llm listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await llm.stop()


if __name__ == "__main__":
    asyncio.run(_main())
