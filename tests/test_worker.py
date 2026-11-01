"""Worker（隔离执行 + 并发上限 + 可取消）单测。

重点验证取消语义：``cancelled`` 必须落成终态、必须有人收到通知、
而且被取消的 task 不能让事件循环里留下未处理异常。
"""

from __future__ import annotations

import asyncio

from superbot.worker import (
    AssignmentState,
    Outcome,
    Ticket,
    WorkerPool,
    WorkerResult,
    run_ok,
)


def _ticket(tid: str = "tk1") -> Ticket:
    return Ticket(id=tid, thread_key="T", mandate="m", created_at=0.0)


class Runner:
    """可编程的执行器。"""

    def __init__(self, delay: float = 0.0, ok: bool = True, fail: bool = False) -> None:
        self.delay = delay
        self.ok = ok
        self.fail = fail
        self.started: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str] = []

    async def __call__(self, ticket: Ticket) -> WorkerResult:
        self.started.append(ticket.id)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("boom")
            self.finished.append(ticket.id)
            return run_ok(f"done:{ticket.id}") if self.ok else WorkerResult.fail("nope")
        except asyncio.CancelledError:
            self.cancelled.append(ticket.id)
            raise


# ---------------------------------------------------------------------------
# 基本执行
# ---------------------------------------------------------------------------


async def test_submit_runs_and_reports():
    got = []

    async def on_done(a):
        got.append((a.ticket.id, a.outcome, a.text))

    pool = WorkerPool(run=Runner(), on_done=on_done)
    await pool.submit(_ticket("a"))
    assert await pool.join(timeout=2)
    assert got == [("a", Outcome.OK, "done:a")]


async def test_failure_outcome():
    got = []
    runner = Runner(fail=True)

    async def on_done(a):
        got.append(a.outcome)

    pool = WorkerPool(run=runner, on_done=on_done)
    await pool.submit(_ticket("a"))
    assert await pool.join(timeout=2)
    assert got == [Outcome.FAILED]


async def test_not_ok_result_is_failed():
    got = []

    async def on_done(a):
        got.append(a.outcome)

    pool = WorkerPool(run=Runner(ok=False), on_done=on_done)
    await pool.submit(_ticket("a"))
    await pool.join(timeout=2)
    assert got == [Outcome.FAILED]


async def test_state_transitions():
    pool = WorkerPool(run=Runner(delay=0.05))
    aid = await pool.submit(_ticket())
    a = pool.get(aid)
    assert a is not None
    await asyncio.sleep(0.01)
    assert a.state is AssignmentState.RUNNING
    await pool.join(timeout=2)
    assert a.state is AssignmentState.ENDED


# ---------------------------------------------------------------------------
# 并发上限
# ---------------------------------------------------------------------------


async def test_concurrency_limit():
    peak = {"n": 0, "cur": 0}

    async def run(t: Ticket):
        peak["cur"] += 1
        peak["n"] = max(peak["n"], peak["cur"])
        await asyncio.sleep(0.05)
        peak["cur"] -= 1
        return run_ok("x")

    pool = WorkerPool(run=run, max_concurrency=2)
    for i in range(5):
        await pool.submit(_ticket(f"t{i}"))
    assert await pool.join(timeout=3)
    assert peak["n"] <= 2
    assert peak["n"] == 2  # 确实并发跑了 2 个，不是串行


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


async def test_cancel_sets_outcome_and_notifies():
    runner = Runner(delay=5.0)
    got: list[Outcome | None] = []

    async def on_done(a):
        got.append(a.outcome)

    pool = WorkerPool(run=runner, on_done=on_done)
    aid = await pool.submit(_ticket("a"))
    await asyncio.sleep(0.05)  # 让它先进入 RUNNING

    assert await pool.cancel(aid) is True
    a = pool.get(aid)
    assert a is not None
    assert a.state is AssignmentState.ENDED
    assert a.outcome is Outcome.CANCELLED
    assert runner.cancelled == ["a"]
    assert got == [Outcome.CANCELLED]


async def test_cancel_unknown_returns_false():
    pool = WorkerPool(run=Runner())
    assert await pool.cancel("nope") is False


async def test_cancel_finished_returns_false():
    pool = WorkerPool(run=Runner())
    aid = await pool.submit(_ticket())
    await pool.join(timeout=2)
    assert await pool.cancel(aid) is False


async def test_cancel_all_cancels_running():
    runner = Runner(delay=5.0)
    pool = WorkerPool(run=runner, max_concurrency=3)
    for i in range(3):
        await pool.submit(_ticket(f"t{i}"))
    await asyncio.sleep(0.05)

    n = await pool.cancel_all()
    assert n == 3
    assert pool.running() == 0
    assert sorted(runner.cancelled) == ["t0", "t1", "t2"]


async def test_cancelled_task_does_not_leak_exception():
    """取消后不能留下 "Task exception was never retrieved"。"""
    pool = WorkerPool(run=Runner(delay=5.0))
    aid = await pool.submit(_ticket())
    await asyncio.sleep(0.02)
    await pool.cancel(aid)
    task = pool._tasks[aid]
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# 观察
# ---------------------------------------------------------------------------


async def test_join_returns_false_on_timeout():
    pool = WorkerPool(run=Runner(delay=5.0))
    await pool.submit(_ticket())
    assert await pool.join(timeout=0.05) is False
    await pool.close()


async def test_join_with_no_tasks():
    pool = WorkerPool(run=Runner())
    assert await pool.join(timeout=1) is True


async def test_outcomes_map():
    pool = WorkerPool(run=Runner())
    await pool.submit(_ticket("a"))
    await pool.join(timeout=2)
    assert set(pool.outcomes().values()) == {"ok"}


async def test_close_cancels_everything():
    runner = Runner(delay=5.0)
    pool = WorkerPool(run=runner)
    for i in range(2):
        await pool.submit(_ticket(f"t{i}"))
    await asyncio.sleep(0.02)
    await pool.close()
    assert pool.running() == 0
