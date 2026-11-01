"""状态库的查询与清理（``/sb status`` 和重启对账要用的那两个）。

``counts()`` 给状态命令看，``open_tickets()`` 给重启对账用，
``prune()`` 负责别让库无限长。
"""

from __future__ import annotations

import time

import pytest

from superbot.store import DispositionRow, Store, TicketRow, TriggerRow


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "state.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _trigger(tid: str, ts: float = 0.0) -> TriggerRow:
    return TriggerRow(
        id=tid, source="aiocqhttp:1", thread_key="T", umo="u",
        actor_id="a", text="hi", ts=ts,
    )


def _ticket(tid: str, created_at: float = 0.0) -> TicketRow:
    return TicketRow(id=tid, thread_key="T", mandate_json="m", created_at=created_at)


# ---------------------------------------------------------------------------
# counts
# ---------------------------------------------------------------------------


async def test_counts_empty(store):
    c = await store.counts()
    assert c["trigger"] == 0 and c["ticket"] == 0 and c["disposition"] == 0


async def test_counts_reflect_writes(store):
    await store.add_trigger(_trigger("t1"))
    await store.add_trigger(_trigger("t2"))
    await store.create_ticket(_ticket("k1"))
    await store.record_disposition(
        DispositionRow(trigger_id=None, verdict="route", reason="ok", ts=1.0)
    )
    c = await store.counts()
    assert c["trigger"] == 2
    assert c["ticket"] == 1
    assert c["disposition"] == 1


# ---------------------------------------------------------------------------
# open_tickets（重启对账）
# ---------------------------------------------------------------------------


async def test_open_tickets_lists_unfinished(store):
    await store.create_ticket(_ticket("k1"))
    await store.add_fact("k1", "accepted")
    assert await store.open_tickets() == ["k1"]


async def test_open_tickets_excludes_terminal(store):
    for tid, kind in (
        ("done", "delivered"),
        ("stopped", "human_stopped"),
        ("blocked", "blocked"),
    ):
        await store.create_ticket(_ticket(tid))
        await store.add_fact(tid, "accepted")
        await store.add_fact(tid, kind)
    assert await store.open_tickets() == []


async def test_open_tickets_keeps_retrying(store):
    """只有 worker_failed 的工单还算"没跑完"（它正在重试）。"""
    await store.create_ticket(_ticket("k1"))
    await store.add_fact("k1", "accepted")
    await store.add_fact("k1", "worker_failed")
    assert await store.open_tickets() == ["k1"]


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


async def test_prune_removes_old_triggers_and_dispositions(store):
    now = time.time()
    old = now - 400 * 86400
    await store.add_trigger(_trigger("old", ts=old))
    await store.add_trigger(_trigger("new", ts=now))
    await store.record_disposition(
        DispositionRow(trigger_id=None, verdict="route", reason="ok", ts=old)
    )
    await store.prune(now=now)
    c = await store.counts()
    assert c["trigger"] == 1  # 只留新的
    assert c["disposition"] == 0


async def test_prune_removes_whole_old_ticket(store):
    """工单和它的事实要一起清，不能留查不到的孤儿事实。"""
    now = time.time()
    await store.create_ticket(_ticket("old", created_at=now - 400 * 86400))
    await store.add_fact("old", "accepted")
    await store.create_ticket(_ticket("new", created_at=now))
    await store.add_fact("new", "accepted")

    await store.prune(now=now)

    c = await store.counts()
    assert c["ticket"] == 1
    assert c["ticket_fact"] == 1  # 只剩 new 的那条
    assert "old" not in await store.open_tickets()


async def test_prune_cleans_orphan_pending(store):
    now = time.time()
    await store.add_trigger(_trigger("gone", ts=now - 400 * 86400))
    await store.push_pending("T", "gone")
    await store.prune(now=now)
    assert (await store.counts())["thread_pending"] == 0
