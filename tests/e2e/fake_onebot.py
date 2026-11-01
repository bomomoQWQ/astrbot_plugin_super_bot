"""模拟 OneBot v11 协议端（反向 WebSocket 的**客户端**）。

AstrBot 的 aiocqhttp 适配器是**服务端**（`astrbot/core/platform/sources/aiocqhttp/
aiocqhttp_platform_adapter.py:55` 用 ``CQHttp(use_ws_reverse=True)``，:435 起
``bot.run_task(host, port)``），协议端作为客户端连到
``ws://<host>:<port>/ws``。所以这里实现的是客户端：

1. 连上去，发一条 ``lifecycle`` 元事件（OneBot 惯例）；
2. 发送 ``message`` 事件模拟群聊/私聊消息；
3. 收到 AstrBot 的 action 调用（``send_group_msg`` 等）后按 OneBot v11 格式回执；
4. 把收到的所有 action 记下来，供测试断言"机器人到底回了什么"。

启动一个独立进程::

    python -m tests.e2e.fake_onebot --port 16199 --group 10001
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field

import aiohttp

__all__ = ["FakeOneBot", "GroupMessage", "onebot_text"]

_message_ids = itertools.count(1000)


def onebot_text(text: str) -> list[dict]:
    """把纯文本包成 OneBot v11 的 message 数组。"""
    return [{"type": "text", "data": {"text": text}}]


@dataclass
class GroupMessage:
    """一条待发送的群消息。"""

    group_id: int
    user_id: int
    text: str
    self_id: int = 10000
    message_id: int | None = None
    nickname: str = "tester"
    at: bool = False
    """是否在文本前加一个 @机器人 的消息段（用来测唤醒路径）。"""

    def payload(self) -> dict:
        chain: list[dict] = []
        if self.at:
            chain.append({"type": "at", "data": {"qq": str(self.self_id)}})
        chain.append({"type": "text", "data": {"text": self.text}})
        return {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self.message_id or next(_message_ids),
            "group_id": self.group_id,
            "user_id": self.user_id,
            "message": chain,
            "raw_message": self.text,
            "font": 0,
            "self_id": self.self_id,
            "time": int(time.time()),
            "sender": {
                "user_id": self.user_id,
                "nickname": self.nickname,
                "card": "",
                "role": "member",
            },
        }


@dataclass
class FakeOneBot:
    """模拟一个 OneBot v11 协议端。"""

    host: str = "127.0.0.1"
    port: int = 16199
    path: str = "/ws"
    token: str = ""
    group_id: int = 10001
    self_id: int = 10000

    actions: list[dict] = field(default_factory=list)
    """收到的所有 action 调用（含回执）。"""

    connected: bool = False
    _session: aiohttp.ClientSession | None = None
    _ws: aiohttp.ClientWebSocketResponse | None = None
    _reader: asyncio.Task | None = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # -- 连接 -------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"

    def headers(self) -> dict[str, str]:
        """OneBot v11 反向 WS 握手头。

        aiocqhttp 的 ``_handle_wsr`` 直接读这两个头、**没有默认值**
        （site-packages/aiocqhttp/__init__.py:506 ``X-Client-Role``），
        缺了会 400/500。role 只认 ``event`` / ``api`` / ``universal``
        三个值，``universal`` 表示同一条连接既收事件也收 API 回执。
        """
        hdrs = {
            "X-Self-ID": str(self.self_id),
            "X-Client-Role": "Universal",
            "User-Agent": "fake-onebot/0.1",
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    async def connect(self, timeout: float = 20.0) -> None:
        """带重试地连上去（AstrBot 启动需要时间）。"""
        self._session = aiohttp.ClientSession()
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._ws = await self._session.ws_connect(
                    self.url, headers=self.headers(), heartbeat=30
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                await asyncio.sleep(0.5)
        else:
            await self._session.close()
            self._session = None
            raise TimeoutError(f"连不上 {self.url}: {last_err}")

        self.connected = True
        self._reader = asyncio.create_task(self._read_loop())
        await self.send_lifecycle()

    async def close(self) -> None:
        self.connected = False
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- 收：处理 AstrBot 的 action 调用 ------------------------------------

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.connected = False

    async def _on_text(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        # 可能一次收到多条（换行分隔）
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if "action" in item:
                self.actions.append(item)
                await self._reply_action(item)

    async def _reply_action(self, item: dict) -> None:
        action = item.get("action", "")
        data: dict = {}
        if action in ("send_msg", "send_group_msg"):
            data = {"message_id": next(_message_ids)}
        elif action == "send_private_msg":
            data = {"message_id": next(_message_ids)}
        elif action == "get_login_info":
            data = {"user_id": self.self_id, "nickname": "fake-bot"}
        elif action in ("get_group_list",):
            data = [
                {"group_id": self.group_id, "group_name": "测试群", "member_count": 3}
            ]
        elif action in ("get_group_member_info", "get_stranger_info"):
            data = {"user_id": self.self_id, "nickname": "tester"}
        payload: dict = {"status": "ok", "retcode": 0, "data": data}
        if "echo" in item:
            payload["echo"] = item["echo"]
        async with self._send_lock:
            if self._ws is not None and not self._ws.closed:
                await self._ws.send_str(json.dumps(payload, ensure_ascii=False))

    # -- 发：事件 ---------------------------------------------------------

    async def _send_json(self, payload: dict) -> None:
        async with self._send_lock:
            if self._ws is None or self._ws.closed:
                raise RuntimeError("WS 未连接")
            await self._ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def send_lifecycle(self) -> None:
        await self._send_json(
            {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "self_id": self.self_id,
                "time": int(time.time()),
            }
        )

    async def send_heartbeat(self) -> None:
        await self._send_json(
            {
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "self_id": self.self_id,
                "time": int(time.time()),
                "status": {"online": True, "good": True},
                "interval": 30000,
            }
        )

    async def send_group(self, text: str, *, user_id: int = 20001, **kw) -> int:
        """发一条群消息，返回 message_id。"""
        msg = GroupMessage(
            group_id=self.group_id, user_id=user_id, text=text, self_id=self.self_id, **kw
        )
        payload = msg.payload()
        await self._send_json(payload)
        return int(payload["message_id"])

    async def send_private(self, text: str, *, user_id: int = 20001) -> int:
        mid = next(_message_ids)
        await self._send_json(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": mid,
                "user_id": user_id,
                "message": onebot_text(text),
                "raw_message": text,
                "font": 0,
                "self_id": self.self_id,
                "time": int(time.time()),
                "sender": {"user_id": user_id, "nickname": "tester"},
            }
        )
        return mid

    async def send_raw(self, payload: dict) -> None:
        """发任意原始事件（测边界用）。"""
        await self._send_json(payload)

    # -- 断言辅助 ---------------------------------------------------------

    async def wait_for_action(
        self, action: str | None = None, timeout: float = 15.0
    ) -> dict | None:
        """等到某个 action（或任意 action）出现。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in self.actions:
                if action is None or item.get("action") == action:
                    return item
            await asyncio.sleep(0.1)
        return None

    def sent_texts(self) -> list[str]:
        """把所有已发送 action 里的文本抽出来，便于断言"机器人说了什么"。"""
        out: list[str] = []
        for item in self.actions:
            params = item.get("params") or {}
            message = params.get("message")
            if isinstance(message, str):
                out.append(message)
            elif isinstance(message, list):
                out.append(
                    "".join(
                        seg.get("data", {}).get("text", "")
                        for seg in message
                        if isinstance(seg, dict) and seg.get("type") == "text"
                    )
                )
        return out

    async def wait_for_text(self, needle: str, timeout: float = 15.0) -> str | None:
        """等到某条发出的消息里包含 ``needle``。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for text in self.sent_texts():
                if needle in text:
                    return text
            await asyncio.sleep(0.1)
        return None

    def reset(self) -> None:
        self.actions.clear()


# ---------------------------------------------------------------------------
# 独立运行：手工联调
# ---------------------------------------------------------------------------


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=16199)
    parser.add_argument("--path", default="/ws")
    parser.add_argument("--token", default="")
    parser.add_argument("--group", type=int, default=10001)
    args = parser.parse_args()

    bot = FakeOneBot(
        host=args.host, port=args.port, path=args.path, token=args.token, group_id=args.group
    )
    await bot.connect()
    print(f"connected to {bot.url}", flush=True)

    async def reporter() -> None:
        seen = 0
        while True:
            while seen < len(bot.actions):
                print("[action]", json.dumps(bot.actions[seen], ensure_ascii=False), flush=True)
                seen += 1
            await asyncio.sleep(0.2)

    task = asyncio.create_task(reporter())
    try:
        while True:
            text = await asyncio.to_thread(input, "输入消息（空行退出）> ")
            if not text.strip():
                break
            await bot.send_group(text.strip())
    finally:
        task.cancel()
        await bot.close()


if __name__ == "__main__":
    asyncio.run(_main())
