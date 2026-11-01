"""Worker —— 隔离执行 + 并发上限 + 可取消。

对应文章 2.2 的 Worker 层：每个任务在自己的上下文里跑，不与用户直接对话，
只把结果交回 Router。这里只实现**执行骨架**——

- ``run`` 由外部注入（P4 里它会是一个调 ``tool_loop_agent`` 的函数）；
- 并发用信号量限流（真实瓶颈是宿主资源与沙箱配额，不是这个数字）；
- 取消走 asyncio 的协作式取消：``task.cancel()`` → 被取消方在
  ``except CancelledError`` 里收尾（记终态、向 Router 汇报）→ **必须再 raise**，
  否则取消会被吞掉、任务变成僵尸；
- 结束时一定产出 outcome（``ok`` / ``failed`` / ``cancelled``），交给 ``on_done``。

本模块**不 import astrbot**。
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import limits

__all__ = [
    "Assignment",
    "AssignmentState",
    "Outcome",
    "Ticket",
    "WorkerPool",
    "run_ok",
]


class Outcome(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssignmentState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(frozen=True)
class Ticket:
    """一个受权的工作单元。``mandate`` 不可变，范围变大要新开 Ticket。"""

    id: str
    thread_key: str
    mandate: str = ""
    created_at: float = 0.0


@dataclass
class Assignment:
    """一次派工。状态由事实推进：pending → running → ended。"""

    id: str
    ticket: Ticket
    state: AssignmentState = AssignmentState.PENDING
    outcome: Outcome | None = None
    text: str = ""
    error: str | None = None
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def done(self) -> bool:
        return self.state is AssignmentState.ENDED


@dataclass(frozen=True)
class WorkerResult:
    """执行函数返回什么。"""

    text: str = ""
    ok: bool = True

    @staticmethod
    def fail(reason: str = "") -> "WorkerResult":
        return WorkerResult(text=reason, ok=False)


def run_ok(text: str = "") -> WorkerResult:
    return WorkerResult(text=text, ok=True)


RunFn = Callable[[Ticket], Awaitable[WorkerResult]]
DoneFn = Callable[[Assignment], Awaitable[None]]


@dataclass
class WorkerPool:
    """跑 Assignment 的池子。

    Args:
        run: 真正的执行函数（注入）。
        max_concurrency: 同时运行上限。
        on_done: 每个 Assignment 结束时回调（把结果交给 Router / Outbox）。
        clock: 注入时钟。
    """

    run: RunFn
    max_concurrency: int = limits.WORKER_MAX_CONCURRENCY
    on_done: DoneFn | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _sem: asyncio.Semaphore | None = None
    _assignments: dict[str, Assignment] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    # -- 提交 -------------------------------------------------------------

    async def submit(self, ticket: Ticket) -> str:
        """提交一个任务，立刻返回 assignment id（不阻塞）。"""
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, self.max_concurrency))

        assignment = Assignment(
            id=uuid.uuid4().hex[:12],
            ticket=ticket,
        )
        self._assignments[assignment.id] = assignment
        self._tasks[assignment.id] = asyncio.create_task(
            self._run_assignment(assignment), name=f"worker-{assignment.id}"
        )
        return assignment.id

    async def _run_assignment(self, a: Assignment) -> None:
        assert self._sem is not None
        async with self._sem:
            a.state = AssignmentState.RUNNING
            a.started_at = self.clock()
            try:
                result = await self.run(a.ticket)
                a.outcome = Outcome.OK if result.ok else Outcome.FAILED
                a.text = result.text
                if not result.ok:
                    a.error = result.text or "run returned not-ok"
            except asyncio.CancelledError:
                # 协作式取消：先落终态，再**必须** re-raise
                a.outcome = Outcome.CANCELLED
                a.error = "cancelled"
                a.ended_at = self.clock()
                a.state = AssignmentState.ENDED
                await self._notify(a)
                raise
            except Exception as exc:  # noqa: BLE001
                a.outcome = Outcome.FAILED
                a.error = f"{type(exc).__name__}: {exc}"
            finally:
                a.ended_at = self.clock()
                a.state = AssignmentState.ENDED

        await self._notify(a)

    async def _notify(self, a: Assignment) -> None:
        if self.on_done is None:
            return
        try:
            await self.on_done(a)
        except Exception:  # noqa: BLE001
            pass

    # -- 取消 -------------------------------------------------------------

    async def cancel(self, assignment_id: str) -> bool:
        """取消一个任务。返回是否真的发出了取消。"""
        task = self._tasks.get(assignment_id)
        a = self._assignments.get(assignment_id)
        if task is None or a is None or a.done:
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return True

    async def cancel_all(self) -> int:
        ids = [aid for aid, a in self._assignments.items() if not a.done]
        for aid in ids:
            await self.cancel(aid)
        return len(ids)

    # -- 等待 -------------------------------------------------------------

    async def join(self, timeout: float | None = None) -> bool:
        """等所有任务结束。返回是否在超时内全部完成。"""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False

    # -- 观察 -------------------------------------------------------------

    def get(self, assignment_id: str) -> Assignment | None:
        return self._assignments.get(assignment_id)

    async def wait(self, assignment_id: str, timeout: float | None = None) -> Assignment | None:
        """等某个 assignment 结束，返回它（超时返回 None）。

        用途：把 Worker 当成"同步工具"用——模型调一个工具，工具等任务跑完再把
        结果交回去。注意超时**不会**取消任务，调用方要自己决定后续怎么办
        （我们这边是把它改成"跑完直接发消息"）。
        """
        task = self._tasks.get(assignment_id)
        a = self._assignments.get(assignment_id)
        if task is None or a is None:
            return None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            # 任务被 /stop 取消时，await 这边也会收到 CancelledError；
            # 只有 assignment 真的落了终态才算"等到了"
            if a.done:
                return a
            raise
        except Exception:  # noqa: BLE001
            pass
        return a

    def running(self) -> int:
        return len([a for a in self._assignments.values() if a.state is AssignmentState.RUNNING])

    def outcomes(self) -> dict[str, str]:
        return {
            aid: (a.outcome.value if a.outcome else a.state.value)
            for aid, a in self._assignments.items()
        }

    async def close(self) -> None:
        await self.cancel_all()
