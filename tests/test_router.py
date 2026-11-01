"""Router（单槽 + 只追加 + 批处理）单测。

重点验证那条核心性质：**合并窗口等于上一次评估的存活时长**，
而且不需要任何定时器或节流参数。
"""

from __future__ import annotations

import asyncio

from superbot.router import Reply, Router, Trigger


def _trig(tid: str, thread: str = "T", text: str = "hi") -> Trigger:
    return Trigger(
        id=tid, thread_key=thread, umo="aiocqhttp:GroupMessage:1",
        actor_id="20001", text=text, ts=0.0,
    )


class Evaluator:
    """可编程的假评估器。"""

    def __init__(self, replies: int = 1, delay: float = 0.0, fail_times: int = 0) -> None:
        self.batches: list[list[Trigger]] = []
        self.replies = replies
        self.delay = delay
        self.fail_times = fail_times
        self._fail_left = fail_times

    async def __call__(self, batch: list[Trigger]) -> list[Reply]:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.batches.append(list(batch))
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("eval boom")
        return [
            Reply(umo=batch[0].umo, text=f"r{i}", thread_key=batch[0].thread_key)
            for i in range(self.replies)
        ]


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------


async def test_batch_same_thread():
    ev = Evaluator()
    r = Router(evaluate=ev)
    for i in range(3):
        r.enqueue(_trig(f"t{i}"))
    ran = await r.run_once()
    assert ran == 1
    assert len(ev.batches) == 1
    assert len(ev.batches[0]) == 3
    assert r.stats.triggers == 3 and r.stats.replies == 1


async def test_max_batch_splits():
    ev = Evaluator()
    r = Router(evaluate=ev, max_batch=2)
    for i in range(5):
        r.enqueue(_trig(f"t{i}"))
    await r.run_once()
    assert len(ev.batches[0]) == 2
    assert r.pending() == 3
    await r.run_once()
    assert len(ev.batches) == 2 and len(ev.batches[1]) == 2
    await r.run_once()
    assert len(ev.batches[2]) == 1


async def test_triggers_during_eval_merge_into_next_batch():
    """评估进行中到达的 Trigger 追加到 pending，下一批自然合并——这就是合并窗口。"""
    r: Router

    async def evaluate(batch: list[Trigger]) -> list[Reply]:
        # 第一批跑的时候，模拟"又有两条消息进来了"
        if not getattr(r, "_seeded", False):
            r._seeded = True  # type: ignore[attr-defined]
            r.enqueue(_trig("late1"))
            r.enqueue(_trig("late2"))
        return []

    r = Router(evaluate=evaluate)
    r.enqueue(_trig("t0"))
    assert await r.run_once() == 1
    assert r.pending() == 2  # 晚到的还在
    r._seeded = True  # type: ignore[attr-defined]
    assert await r.run_once() == 1
    assert r.pending() == 0


async def test_single_slot_blocks_concurrent_eval():
    """同一 Thread 不能被两个并发 run_once 同时评估。"""
    gate = asyncio.Event()
    seen: list[list[Trigger]] = []

    async def evaluate(batch: list[Trigger]) -> list[Reply]:
        seen.append(list(batch))
        await gate.wait()
        return []

    r = Router(evaluate=evaluate)
    r.enqueue(_trig("t0"))

    first = asyncio.create_task(r.run_once())
    await asyncio.sleep(0)  # 让 first 占到槽
    assert r.is_evaluating("T") is True

    # 再入队并并发跑：应被单槽挡下
    r.enqueue(_trig("t1"))
    second = await r.run_once()
    assert second == 0
    assert len(seen) == 1

    gate.set()
    await first
    assert r.is_evaluating("T") is False


async def test_threads_are_independent():
    ev = Evaluator()
    r = Router(evaluate=ev)
    r.enqueue(_trig("a1", thread="A"))
    r.enqueue(_trig("b1", thread="B"))
    assert await r.run_once() == 2
    assert len(ev.batches) == 2


async def test_one_thread_failure_does_not_block_others():
    async def evaluate(batch: list[Trigger]) -> list[Reply]:
        if batch[0].thread_key == "A":
            raise RuntimeError("boom")
        return [Reply(umo="u", text="ok")]

    r = Router(evaluate=evaluate, max_requeues=0)
    r.enqueue(_trig("a1", thread="A"))
    r.enqueue(_trig("b1", thread="B"))
    await r.run_once()
    assert r.pending("A") == 0  # 已丢弃
    assert r.stats.dropped == 1
    assert r.stats.evals == 1  # B 成功
    assert r.stats.errors == 1


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


async def test_enqueue_dedup_same_id():
    ev = Evaluator()
    r = Router(evaluate=ev)
    assert r.enqueue(_trig("same")) is True
    assert r.enqueue(_trig("same")) is False
    assert r.pending() == 1


async def test_same_id_different_thread_not_deduped():
    r = Router(evaluate=Evaluator())
    assert r.enqueue(_trig("x", thread="A")) is True
    assert r.enqueue(_trig("x", thread="B")) is True


# ---------------------------------------------------------------------------
# 失败重投
# ---------------------------------------------------------------------------


async def test_requeue_then_succeed():
    ev = Evaluator(fail_times=1)
    r = Router(evaluate=ev, max_requeues=2)
    r.enqueue(_trig("t0"))
    await r.run_once()
    assert r.stats.requeues == 1
    assert r.pending() == 1  # 放回队列
    await r.run_once()
    assert r.stats.evals == 1
    assert r.pending() == 0


async def test_requeue_budget_exhausted_drops():
    ev = Evaluator(fail_times=99)
    r = Router(evaluate=ev, max_requeues=1)
    r.enqueue(_trig("t0"))
    await r.run_once()  # 第 1 次失败 → 重投
    assert r.stats.requeues == 1
    await r.run_once()  # 第 2 次失败 → 预算用尽 → 丢弃
    assert r.stats.dropped == 1
    assert r.pending() == 0


async def test_requeue_puts_batch_at_head():
    """失败批次要放回**队首**，保证顺序不被打乱。"""
    calls: list[list[str]] = []

    async def evaluate(batch: list[Trigger]) -> list[Reply]:
        calls.append([t.id for t in batch])
        if len(calls) == 1:
            raise RuntimeError("first fails")
        return []

    r = Router(evaluate=evaluate, max_batch=1, max_requeues=1)
    r.enqueue(_trig("t0"))
    r.enqueue(_trig("t1"))
    await r.drain()
    # 第一次 t0 失败 → 放回队首 → 再跑 t0 → 然后 t1
    assert calls == [["t0"], ["t0"], ["t1"]]


# ---------------------------------------------------------------------------
# 常驻循环
# ---------------------------------------------------------------------------


async def test_run_forever_processes_and_stops():
    ev = Evaluator()
    r = Router(evaluate=ev, coalesce=0.02)
    stop = asyncio.Event()

    r.enqueue(_trig("t0"))
    task = asyncio.create_task(r.run_forever(stop, idle_wait=0.05))
    await asyncio.sleep(0.15)  # 第一批已经跑完
    r.enqueue(_trig("t1"))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert r.stats.evals >= 2


async def test_run_forever_coalesces_burst_into_one_batch():
    """空闲时连发 5 条 → 聚合窗口把它们并成一批。"""
    ev = Evaluator()
    r = Router(evaluate=ev, coalesce=0.05)
    stop = asyncio.Event()

    task = asyncio.create_task(r.run_forever(stop, idle_wait=0.05))
    await asyncio.sleep(0.02)  # 让循环进入空闲等待
    for i in range(5):
        r.enqueue(_trig(f"t{i}"))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(ev.batches) == 1
    assert len(ev.batches[0]) == 5


async def test_snapshot():
    r = Router(evaluate=Evaluator(replies=2))
    r.enqueue(_trig("t0"))
    await r.run_once()
    snap = r.snapshot()
    assert snap["evals"] == 1
    assert snap["triggers"] == 1
    assert snap["replies"] == 2
    assert snap["pending"] == 0
    assert snap["evaluating"] == 0


# ---------------------------------------------------------------------------
# clear（给 /stop 用）
# ---------------------------------------------------------------------------


async def test_clear_drops_pending():
    r = Router(evaluate=Evaluator())
    for i in range(3):
        r.enqueue(_trig(f"t{i}"))
    assert r.clear() == 3
    assert r.pending() == 0


async def test_clear_one_thread_only():
    r = Router(evaluate=Evaluator())
    r.enqueue(_trig("a1", thread="A"))
    r.enqueue(_trig("a2", thread="A"))
    r.enqueue(_trig("b1", thread="B"))
    assert r.clear("A") == 2
    assert r.pending("A") == 0
    assert r.pending("B") == 1


async def test_clear_unknown_thread_returns_zero():
    r = Router(evaluate=Evaluator())
    assert r.clear("nope") == 0


async def test_clear_does_not_touch_running_batch():
    """已经出队、正在评估的那一批不受 clear 影响（由 Worker 取消负责）。"""
    gate = asyncio.Event()

    async def evaluate(batch: list[Trigger]) -> list[Reply]:
        await gate.wait()
        return []

    r = Router(evaluate=evaluate, coalesce=0)
    r.enqueue(_trig("t0"))
    task = asyncio.create_task(r.run_once())
    await asyncio.sleep(0)

    r.enqueue(_trig("t1"))
    assert r.clear() == 1  # 只清掉排队的那条
    gate.set()
    await task
    assert r.stats.evals == 1
    assert r.pending() == 0


async def test_clear_resets_requeue_budget():
    ev = Evaluator(fail_times=99)
    r = Router(evaluate=ev, max_requeues=5)
    r.enqueue(_trig("t0"))
    await r.run_once()  # 失败 → 重投，预算用掉 1
    assert r.pending() == 1
    r.clear()
    assert r.pending() == 0
    assert r._requeued == {}  # 预算也清掉，下次重新算
