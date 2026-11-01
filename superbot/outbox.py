"""可靠投递（Outbox）—— 外发消息的唯一出口。

为什么需要它：AstrBot 的 `RespondStage` 是**直接 await `event.send()`**，
既没有队列也没有锁（`pipeline/respond/stage.py:230,281,310,320`），
而 `context.send_message` 同样是直连平台（`star/context.py:643`）并返回 bool。
于是"Router 的主动投递"和"别人的正常回复"会互相穿插，且发失败没人知道。

Outbox 解决三件事：

1. **同一 UMO 串行**：每个会话一把锁 + 单消费者，避免两条消息交错；
2. **失败重试**：按退避序列重试，仍失败则计入 failed 并留痕；
3. **幂等入队**：同一个 key 只发一次（重试/重放不会重复刷屏）。

本模块**不 import astrbot**：发送动作由外部注入一个 callable，
于是完全可以在没有 AstrBot 的环境里单测。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import limits

__all__ = ["Outbox", "Outbound", "SendFn"]

SendFn = Callable[[str, str], Awaitable[bool]]
"""``(umo, text) -> 是否发送成功``。由适配层实现（内部调 ``context.send_message``）。"""


@dataclass
class Outbound:
    """一条待发消息。"""

    id: str
    umo: str
    text: str
    created_at: float
    attempts: int = 0
    last_error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.umo}\x00{self.text}"


@dataclass
class Outbox:
    """按会话串行的可靠投递队列。

    Args:
        send: 真正发送的函数，返回是否成功。
        max_attempts: 最大尝试次数（含首次）。
        backoff: 每次失败后的等待秒数，不足时取最后一个。
        clock: 注入时钟，便于测试。
        sleeper: 注入 sleep，便于测试里零等待。
    """

    send: SendFn
    max_attempts: int = limits.SEND_MAX_ATTEMPTS
    backoff: tuple[float, ...] = limits.SEND_BACKOFF_SEC
    clock: Callable[[], float] = time.time
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep

    sent: int = 0
    failed: int = 0
    _queues: dict[str, list[Outbound]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _locks: dict[str, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))
    _seen: set[str] = field(default_factory=set)
    _closed: bool = False

    # -- 入队 -------------------------------------------------------------

    async def enqueue(
        self, umo: str, text: str, *, key: str | None = None
    ) -> str:
        """入队一条消息，返回消息 id。

        同一个 ``key`` 只会入队一次（幂等）；不传 key 时用 ``umo + text`` 去重。
        """
        if self._closed:
            raise RuntimeError("Outbox 已关闭")

        dedup_key = key or f"{umo}\x00{text}"
        if dedup_key in self._seen:
            return ""  # 已发过/已入队
        self._seen.add(dedup_key)

        msg = Outbound(
            id=uuid.uuid4().hex[:12],
            umo=umo,
            text=text,
            created_at=self.clock(),
        )
        self._queues[umo].append(msg)
        return msg.id

    def pending(self, umo: str | None = None) -> int:
        if umo is None:
            return sum(len(q) for q in self._queues.values())
        return len(self._queues.get(umo, ()))

    # -- 投递 -------------------------------------------------------------

    async def drain(self, umo: str | None = None) -> None:
        """把队列发空。

        ``umo=None`` 表示所有会话；同一个 umo 的投递通过其专属锁串行化，
        不同 umo 之间可以并行。
        """
        if umo is not None:
            await self._drain_one(umo)
            return
        umos = [k for k, q in self._queues.items() if q]
        if umos:
            await asyncio.gather(*(self._drain_one(u) for u in umos))

    async def _drain_one(self, umo: str) -> None:
        queue = self._queues.get(umo)
        if not queue:
            return
        async with self._locks[umo]:
            while queue:
                msg = queue[0]
                ok = await self._deliver(msg)
                queue.pop(0)
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1

    async def _deliver(self, msg: Outbound) -> bool:
        """按退避重试投递一条消息。"""
        for attempt in range(1, self.max_attempts + 1):
            msg.attempts = attempt
            try:
                if await self.send(msg.umo, msg.text):
                    return True
                msg.last_error = "sender returned False"
            except Exception as exc:  # noqa: BLE001
                msg.last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.max_attempts:
                delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                await self.sleeper(delay)
        return False

    # -- 生命周期 ---------------------------------------------------------

    async def close(self) -> None:
        """关闭后再入队会抛错；未发出的消息仍可 drain。"""
        self._closed = True

    def reset_stats(self) -> None:
        self.sent = 0
        self.failed = 0
