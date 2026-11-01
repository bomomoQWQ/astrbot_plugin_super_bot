"""Outbox（可靠投递）单测。

要点：同一 UMO 串行、失败重试、幂等入队、失败计数。
用注入的 ``sleeper`` 做到零等待。
"""

from __future__ import annotations

import asyncio

import pytest

from superbot.outbox import Outbox


async def _no_sleep(_: float) -> None:
    return None


class Recorder:
    """可编程的假发送器。"""

    def __init__(self, fail_times: int = 0, delay: float = 0.0) -> None:
        self.fail_times = fail_times
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self._fail_left = fail_times

    async def __call__(self, umo: str, text: str) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((umo, text))
        if self._fail_left > 0:
            self._fail_left -= 1
            return False
        return True


# ---------------------------------------------------------------------------
# 基本投递
# ---------------------------------------------------------------------------


async def test_send_once():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "hello")
    await ob.drain()
    assert rec.calls == [("umo:1", "hello")]
    assert ob.sent == 1 and ob.failed == 0
    assert ob.pending() == 0


async def test_retry_until_success():
    rec = Recorder(fail_times=2)
    ob = Outbox(send=rec, max_attempts=3, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "hi")
    await ob.drain()
    assert len(rec.calls) == 3
    assert ob.sent == 1 and ob.failed == 0


async def test_give_up_after_max_attempts():
    rec = Recorder(fail_times=99)
    ob = Outbox(send=rec, max_attempts=3, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "hi")
    await ob.drain()
    assert len(rec.calls) == 3
    assert ob.failed == 1 and ob.sent == 0


async def test_sender_exception_is_retried():
    calls = {"n": 0}

    async def boom(umo: str, text: str) -> bool:
        calls["n"] += 1
        raise RuntimeError("network down")

    ob = Outbox(send=boom, max_attempts=2, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "hi")
    await ob.drain()
    assert calls["n"] == 2
    assert ob.failed == 1


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


async def test_dedup_by_key():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    a = await ob.enqueue("umo:1", "hi", key="k1")
    b = await ob.enqueue("umo:1", "hi", key="k1")
    assert a != "" and b == ""
    await ob.drain()
    assert len(rec.calls) == 1


async def test_dedup_by_content_when_no_key():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "same")
    await ob.enqueue("umo:1", "same")
    await ob.drain()
    assert len(rec.calls) == 1


async def test_same_text_different_umo_not_deduped():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "same")
    await ob.enqueue("umo:2", "same")
    await ob.drain()
    assert len(rec.calls) == 2


# ---------------------------------------------------------------------------
# 串行与并发
# ---------------------------------------------------------------------------


async def test_same_umo_keeps_order():
    """同一会话的 5 条消息必须按入队顺序发出。"""
    order: list[str] = []

    async def slow(umo: str, text: str) -> bool:
        await asyncio.sleep(0.01)
        order.append(text)
        return True

    ob = Outbox(send=slow, sleeper=_no_sleep)
    for i in range(5):
        await ob.enqueue("umo:1", f"m{i}")
    await ob.drain()
    assert order == [f"m{i}" for i in range(5)]


async def test_different_umo_run_in_parallel():
    """两个会话的消息互不阻塞（并发执行，总耗时远小于串行）。"""
    started: list[str] = []

    async def slow(umo: str, text: str) -> bool:
        started.append(umo)
        await asyncio.sleep(0.05)
        return True

    ob = Outbox(send=slow, sleeper=_no_sleep)
    await ob.enqueue("umo:A", "a1")
    await ob.enqueue("umo:B", "b1")

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await ob.drain()
    elapsed = loop.time() - t0
    # 串行至少 0.1s；并行应接近 0.05s
    assert elapsed < 0.09, f"疑似串行，耗时 {elapsed:.3f}s"
    assert set(started) == {"umo:A", "umo:B"}


async def test_pending_counts():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.enqueue("umo:A", "1")
    await ob.enqueue("umo:A", "2")
    await ob.enqueue("umo:B", "3")
    assert ob.pending() == 3
    assert ob.pending("umo:A") == 2
    await ob.drain("umo:A")
    assert ob.pending("umo:A") == 0
    assert ob.pending("umo:B") == 1


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_closed_outbox_rejects_enqueue():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.close()
    with pytest.raises(RuntimeError):
        await ob.enqueue("umo:1", "hi")


async def test_drain_empty_is_noop():
    rec = Recorder()
    ob = Outbox(send=rec, sleeper=_no_sleep)
    await ob.drain()
    assert rec.calls == []


async def test_failed_message_does_not_block_queue():
    """第一条一直失败，也不能把后面的堵死（失败即丢弃并计数）。"""
    state = {"n": 0}

    async def picky(umo: str, text: str) -> bool:
        state["n"] += 1
        return text != "bad"

    ob = Outbox(send=picky, max_attempts=2, sleeper=_no_sleep)
    await ob.enqueue("umo:1", "bad")
    await ob.enqueue("umo:1", "good")
    await ob.drain()
    assert ob.failed == 1
    assert ob.sent == 1
    assert ob.pending("umo:1") == 0
