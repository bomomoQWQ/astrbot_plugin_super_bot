"""Watchdog —— 任务级停滞检测与升级（对应《为人民服务》的国家监委）。

为什么需要它：进程活着不等于任务在进展。模型陷入无效循环、Worker 卡在某个
工具上、消息发不出去，都表现为"看起来在跑"。文章第 6 节把这类监控放在主系统
**之外**，用定时消息驱动一个独立的控制回路。

本实现的分工与边界：

- **只做判定**：``tick()`` 是纯函数式的——给它一个时刻，返回一组
  :class:`Action`，不直接产生副作用。这样能被单测覆盖到每一个分支。
- **由外部执行动作**：真正的重试/升级由调用方（Router）接到 ``Action`` 后执行。
- **管不了进程假死**：本模块与 Router 同进程，进程挂了它一起挂。进程级自愈
  归 AstrBot 自己的 ``process_restart.py``。

阈值常量来自 ``superbot/limits.py``（原 ``STALL_CHECK_INTERVAL_MS=60_000``、
``STALL_THRESHOLD_MS=600_000``、``MAX_RETRIES=3``、``RETRY_BACKOFF=[30,60,120]``、
``MAX_ESCALATION=3``）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from . import limits

__all__ = ["Action", "ActionKind", "Watchdog", "WatchItem"]


class ActionKind(str, Enum):
    STALLED = "stalled"
    """刚判为停滞（首次发现）。"""

    RETRY = "retry"
    """还在重试预算内，应重跑。"""

    ESCALATE = "escalate"
    """重试耗尽，升一级处理。"""

    FAILED = "failed"
    """升级也到顶了，标记失败。"""


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    ticket_id: str
    detail: str = ""
    attempt: int = 0
    level: int = 0


@dataclass
class WatchItem:
    """被监控的一个任务。"""

    ticket_id: str
    started_at: float
    last_progress: float
    retries: int = 0
    """已经重试过几次。"""

    escalations: int = 0
    """已经升过几级。"""

    stalled_reported: bool = False
    """是否已经报过一次停滞（避免每 tick 都刷）。"""

    next_retry_at: float = 0.0
    """下一次允许重试的时刻（退避）。"""


@dataclass
class Watchdog:
    """停滞检测器。

    Args:
        stall_seconds: 多久没进展算停滞。
        retries: 允许的自动重试次数（不含升级）。
        backoff: 重试退避序列。
        max_escalation: 最大升级层数，超过即 ``failed``。
        clock: 注入时钟。
    """

    stall_seconds: float = limits.STALL_THRESHOLD_SEC
    retries: int = limits.MAX_RETRIES
    backoff: tuple[float, ...] = limits.RETRY_BACKOFF_SEC
    max_escalation: int = limits.MAX_ESCALATION
    clock: Callable[[], float] = time.time

    items: dict[str, WatchItem] = field(default_factory=dict)
    _failed: set[str] = field(default_factory=set)

    # -- 登记 / 进展 / 结束 ------------------------------------------------

    def watch(self, ticket_id: str, now: float | None = None) -> WatchItem:
        ts = self.clock() if now is None else now
        item = WatchItem(
            ticket_id=ticket_id, started_at=ts, last_progress=ts
        )
        self.items[ticket_id] = item
        return item

    def touch(self, ticket_id: str, now: float | None = None) -> None:
        """报告一次实质进展。会清掉停滞标记与重试计数。"""
        item = self.items.get(ticket_id)
        if item is None:
            return
        ts = self.clock() if now is None else now
        item.last_progress = ts
        item.stalled_reported = False
        item.retries = 0
        item.next_retry_at = 0.0

    def finish(self, ticket_id: str) -> None:
        self.items.pop(ticket_id, None)
        self._failed.discard(ticket_id)

    # -- 扫描 -------------------------------------------------------------

    def tick(self, now: float | None = None) -> list[Action]:
        """扫描一遍，返回本 tick 应执行的动作（不产生副作用）。"""
        ts = self.clock() if now is None else now
        actions: list[Action] = []

        for ticket_id, item in list(self.items.items()):
            if ticket_id in self._failed:
                continue
            idle = ts - item.last_progress
            if idle < self.stall_seconds:
                continue

            # 刚发现停滞
            if not item.stalled_reported:
                item.stalled_reported = True
                actions.append(
                    Action(
                        ActionKind.STALLED,
                        ticket_id,
                        f"no progress for {idle:.0f}s",
                        attempt=item.retries,
                        level=item.escalations,
                    )
                )

            # 退避未到，等下一个 tick
            if item.next_retry_at and ts < item.next_retry_at:
                continue

            if item.retries < self.retries:
                item.retries += 1
                delay = self.backoff[min(item.retries - 1, len(self.backoff) - 1)]
                item.next_retry_at = ts + delay
                # 注意：重试是 watchdog 自己的动作，**不算进展**，
                # 所以不动 last_progress ——只有真的推进了才该 reset。
                actions.append(
                    Action(
                        ActionKind.RETRY,
                        ticket_id,
                        f"retry #{item.retries} after {delay:.0f}s backoff",
                        attempt=item.retries,
                        level=item.escalations,
                    )
                )
                continue

            if item.escalations < self.max_escalation:
                item.escalations += 1
                # 升级也要按退避节奏走，否则一次 tick 会连升多级
                item.next_retry_at = ts + self.backoff[-1]
                actions.append(
                    Action(
                        ActionKind.ESCALATE,
                        ticket_id,
                        f"escalate to level {item.escalations}",
                        attempt=item.retries,
                        level=item.escalations,
                    )
                )
                continue

            self._failed.add(ticket_id)
            actions.append(
                Action(
                    ActionKind.FAILED,
                    ticket_id,
                    f"gave up after {item.retries} retries and "
                    f"{item.escalations} escalations",
                    attempt=item.retries,
                    level=item.escalations,
                )
            )

        return actions

    @property
    def failed(self) -> set[str]:
        return set(self._failed)

    def active(self) -> int:
        return len([t for t in self.items if t not in self._failed])

    # -- 常驻循环 ---------------------------------------------------------

    async def run_loop(
        self,
        on_action: Callable[[Action], Awaitable[None]],
        *,
        interval: float = limits.WATCHDOG_TICK_SEC,
        stop: asyncio.Event | None = None,
        max_ticks: int | None = None,
    ) -> None:
        """按 ``interval`` 周期扫描并把动作丢给 ``on_action``。

        ``max_ticks`` 仅用于测试；``stop`` 用于优雅退出。
        """
        ticks = 0
        while True:
            for action in self.tick():
                await on_action(action)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return  # 收到停止信号
                except asyncio.TimeoutError:
                    continue
            await asyncio.sleep(interval)
