"""Router —— 单槽 + 只追加的批处理调度器（对应文章 2.1 的 batching）。

核心性质（见 ``docs/设计/30-decisions.md`` ADR-06）：

- **不用锁，不用定时器。** 每个 Thread 一个 ``evaluating`` 槽位；
- 评估进行中到达的 Trigger 只往 ``pending`` 里追加；
- 下一次取批时自然把它们合并进来——**合并窗口自动等于上一次评估的存活时长**。

对比：若用定时器做节流，就要引入"窗口多长"这种拍脑袋的参数，而且窗口越大延迟
越高、越小合并效果越差。单槽 + 只追加没有这个旋钮。

失败处理照参考实现：评估抛错就把这一批**放回队首**重投，最多重投
``max_requeues`` 次；用尽则丢弃并记一条 (参考实现叫 ``unavailable``)。

本模块**不 import astrbot**：真正的评估函数由外部注入（P3 用确定性假评估器，
P4 换成调模型的真评估器）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import limits

__all__ = [
    "EvalFn",
    "Reply",
    "Router",
    "RouterStats",
    "Trigger",
]


@dataclass(frozen=True)
class Trigger:
    """一条不可变的环境事件。

    ``id`` 是幂等键（``thread_key.trigger_id``），同一个平台消息算出来的 id 相同。
    """

    id: str
    thread_key: str
    umo: str
    actor_id: str
    text: str
    ts: float


@dataclass(frozen=True)
class Reply:
    """Router 决定要说的一句话。"""

    umo: str
    text: str
    thread_key: str = ""


EvalFn = Callable[[list[Trigger]], Awaitable[list[Reply]]]
"""``一批 Trigger -> 一组回复``。P3 是确定性桩，P4 接模型。"""


@dataclass
class RouterStats:
    evals: int = 0
    triggers: int = 0
    replies: int = 0
    requeues: int = 0
    dropped: int = 0
    errors: int = 0


@dataclass
class Router:
    """批处理调度器。

    Args:
        evaluate: 评估函数。抛异常表示这一批没处理成功。
        max_batch: 单批上限（防止一次塞爆上下文）。
        max_requeues: 失败后最多重投几次。
        sleep: 注入 sleep，便于测试。
    """

    evaluate: EvalFn
    max_batch: int = limits.ROUTER_BATCH_MAX
    max_requeues: int = 2
    coalesce: float = 0.15
    """空闲时的聚合窗口（秒）。

    为什么需要它：单槽 + 只追加能保证"评估期间的 Trigger 合并"，但**第一条
    消息不会等**——它一到就开跑，后面几条只能凑下一批。真实 Router 因为一直在
    忙别的事，天然有延迟；而空闲的 Router 需要一个很短的窗口把同一瞬间到达的
    几条聚成一批。这个窗口只在**从空闲转入忙碌**时生效，评估进行中不需要它
    （那段时间的合并是自动的）。
    """

    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    stats: RouterStats = field(default_factory=RouterStats)
    _pending: dict[str, list[Trigger]] = field(default_factory=dict)
    _requeued: dict[str, int] = field(default_factory=dict)
    _evaluating: set[str] = field(default_factory=set)
    _wake: asyncio.Event | None = None

    # -- 入队 -------------------------------------------------------------

    def enqueue(self, trigger: Trigger) -> bool:
        """追加一条 Trigger。

        Returns:
            ``True`` 表示是新入队；``False`` 表示同一 thread 里已经有这个 id
            （幂等命中，直接忽略）。
        """
        queue = self._pending.setdefault(trigger.thread_key, [])
        if any(t.id == trigger.id for t in queue):
            return False
        queue.append(trigger)
        if self._wake is not None:
            self._wake.set()
        return True

    def pending(self, thread_key: str | None = None) -> int:
        if thread_key is None:
            return sum(len(q) for q in self._pending.values())
        return len(self._pending.get(thread_key, ()))

    def is_evaluating(self, thread_key: str) -> bool:
        return thread_key in self._evaluating

    # -- 取批 -------------------------------------------------------------

    def _take(self, thread_key: str, limit: int) -> list[Trigger]:
        queue = self._pending.get(thread_key)
        if not queue:
            return []
        batch = queue[:limit]
        del queue[:limit]
        if not queue:
            self._pending.pop(thread_key, None)
        return batch

    def _requeue(self, thread_key: str, batch: list[Trigger]) -> bool:
        """把失败的批次放回队首。

        Returns:
            ``True`` 表示还允许重投；``False`` 表示重投预算用尽。
        """
        used = self._requeued.get(thread_key, 0)
        if used >= self.max_requeues:
            self._requeued.pop(thread_key, None)
            return False
        self._requeued[thread_key] = used + 1
        self._pending[thread_key] = batch + self._pending.get(thread_key, [])
        return True

    # -- 执行 -------------------------------------------------------------

    async def run_once(self) -> int:
        """把当前所有可跑的 Thread 各跑一批，返回跑了几次评估。

        同一 Thread 同时只有一个评估（单槽）：已经在评估中的 Thread 直接跳过，
        于是并发调用 ``run_once`` 也不会重复处理同一批。
        """
        ran = 0
        for thread_key in list(self._pending.keys()):
            if thread_key in self._evaluating:
                continue
            batch = self._take(thread_key, self.max_batch)
            if not batch:
                continue

            # 先占槽，再 await —— 保证并发调用看到的是"已在评估"
            self._evaluating.add(thread_key)
            try:
                replies = await self.evaluate(batch)
                self.stats.evals += 1
                self.stats.triggers += len(batch)
                self.stats.replies += len(replies or ())
                self._requeued.pop(thread_key, None)
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                if self._requeue(thread_key, batch):
                    self.stats.requeues += 1
                else:
                    self.stats.dropped += len(batch)
            finally:
                self._evaluating.discard(thread_key)
            ran += 1
        return ran

    async def drain(self, max_rounds: int = 20) -> int:
        """反复 run_once 直到没有待处理（测试用；上限防死循环）。"""
        total = 0
        for _ in range(max_rounds):
            if await self.run_once() == 0:
                break
            total += 1
            await self.sleep(0)
        return total

    # -- 常驻循环 ---------------------------------------------------------

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        idle_wait: float = 5.0,
    ) -> None:
        """常驻：有活就干，没活就等 ``_wake``（最长等 ``idle_wait`` 秒）。"""
        self._wake = asyncio.Event()
        try:
            while not stop.is_set():
                # 从空闲转入忙碌时，先给一个很短的聚合窗口，让同一瞬间到达的
                # 几条消息落进同一批。评估进行中的合并不需要这个窗口。
                if self.coalesce > 0 and self.pending():
                    await self.sleep(self.coalesce)

                ran = await self.run_once()
                if ran:
                    await self.sleep(0)  # 让出控制权，再取下一批
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=idle_wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._wake = None

    # -- 观察 -------------------------------------------------------------

    def snapshot(self) -> dict[str, int]:
        return {
            "evals": self.stats.evals,
            "triggers": self.stats.triggers,
            "replies": self.stats.replies,
            "requeues": self.stats.requeues,
            "dropped": self.stats.dropped,
            "errors": self.stats.errors,
            "pending": self.pending(),
            "evaluating": len(self._evaluating),
        }
