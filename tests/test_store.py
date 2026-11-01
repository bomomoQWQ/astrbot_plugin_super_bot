"""状态库单测。

覆盖 docs/设计/20-p0-spec.md §4 的第九、十、十一、十二与十三条：
迁移、幂等、状态由事实算出、并发写入、清理。
"""

from __future__ import annotations

import asyncio

import pytest

from core import limits
from core.store import (
    DispositionRow,
    Store,
    TicketRow,
    TriggerRow,
    ticket_state_from_facts,
)


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "state.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _trigger(tid: str = "t1", thread: str = "g|day:2026-11-01", **kw) -> TriggerRow:
    base = dict(
        id=tid,
        source="aiocqhttp",
        thread_key=thread,
        umo="aiocqhttp:GroupMessage:123",
        actor_id="20001",
        text="看下这个",
        ts=1000.0,
    )
    base.update(kw)
    return TriggerRow(**base)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


async def test_migrate_creates_schema(store):
    assert await store.schema_version() == limits.SCHEMA_VERSION


async def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "state.db"
    s1 = Store(path)
    await s1.open()
    await s1.close()

    s2 = Store(path)
    await s2.open()
    try:
        assert await s2.schema_version() == limits.SCHEMA_VERSION
    finally:
        await s2.close()


async def test_migration_preserves_data(tmp_path):
    path = tmp_path / "state.db"
    s1 = Store(path)
    await s1.open()
    await s1.add_trigger(_trigger())
    await s1.close()

    s2 = Store(path)
    await s2.open()
    try:
        assert await s2.get_trigger("t1") is not None
    finally:
        await s2.close()


async def test_open_is_idempotent_on_same_dir(tmp_path):
    """目录不存在时自动创建。"""
    s = Store(tmp_path / "deep" / "nested" / "state.db")
    await s.open()
    try:
        assert (tmp_path / "deep" / "nested" / "state.db").exists()
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Trigger 幂等
# ---------------------------------------------------------------------------


async def test_add_trigger_returns_true_first_time(store):
    assert await store.add_trigger(_trigger()) is True


async def test_add_trigger_is_idempotent(store):
    assert await store.add_trigger(_trigger()) is True
    assert await store.add_trigger(_trigger()) is False
    row = await store.get_trigger("t1")
    assert row is not None and row.text == "看下这个"


async def test_get_trigger_missing(store):
    assert await store.get_trigger("nope") is None


# ---------------------------------------------------------------------------
# Thread：单槽 + 只追加
# ---------------------------------------------------------------------------


async def test_begin_eval_single_slot(store):
    await store.ensure_thread("T")
    assert await store.begin_eval("T", "e1") is True
    # 同一 Thread 第二个评估抢不到槽
    assert await store.begin_eval("T", "e2") is False
    await store.end_eval("T", "e1")
    assert await store.begin_eval("T", "e2") is True


async def test_begin_eval_unknown_thread(store):
    assert await store.begin_eval("missing", "e1") is False


async def test_end_eval_only_by_owner(store):
    await store.ensure_thread("T")
    await store.begin_eval("T", "e1")
    await store.end_eval("T", "someone-else")
    # 别人的 end 不应释放槽位
    assert await store.begin_eval("T", "e2") is False


async def test_pending_drain_respects_limit(store):
    await store.ensure_thread("T")
    for i in range(5):
        await store.push_pending("T", f"t{i}")
    assert await store.pending_count("T") == 5
    batch = await store.drain_pending("T", 3)
    assert len(batch) == 3
    assert await store.pending_count("T") == 2


async def test_pending_dedup(store):
    await store.ensure_thread("T")
    await store.push_pending("T", "t1")
    await store.push_pending("T", "t1")
    assert await store.pending_count("T") == 1


async def test_pending_isolated_per_thread(store):
    await store.ensure_thread("A")
    await store.ensure_thread("B")
    await store.push_pending("A", "t1")
    await store.push_pending("B", "t2")
    assert await store.drain_pending("A", 10) == ["t1"]
    assert await store.pending_count("B") == 1


async def test_drain_empty_returns_list(store):
    await store.ensure_thread("T")
    assert await store.drain_pending("T", 10) == []


# ---------------------------------------------------------------------------
# Ticket：状态由事实算出
# ---------------------------------------------------------------------------


def test_state_from_facts_pure():
    assert ticket_state_from_facts([]) == "open"
    assert ticket_state_from_facts(["accepted"]) == "running"
    assert ticket_state_from_facts(["accepted", "worker_failed"]) == "retrying"
    assert ticket_state_from_facts(["worker_failed", "blocked"]) == "blocked"
    assert ticket_state_from_facts(["delivered", "accepted"]) == "done"
    assert ticket_state_from_facts(["delivered", "human_stopped"]) == "stopped"


async def test_ticket_state_from_db(store):
    await store.create_ticket(TicketRow("tk1", "T", '{"scope":"x"}', 1000.0))
    assert await store.ticket_state("tk1") == "open"
    await store.add_fact("tk1", "accepted")
    assert await store.ticket_state("tk1") == "running"
    await store.add_fact("tk1", "human_stopped", now=1001.0)
    assert await store.ticket_state("tk1") == "stopped"


async def test_create_ticket_idempotent(store):
    row = TicketRow("tk1", "T", "{}", 1000.0)
    assert await store.create_ticket(row) is True
    assert await store.create_ticket(row) is False


async def test_one_eval_one_ticket(store):
    """同一 eval 派生的 ticket id 相同 → 重复创建只会命中一次。"""
    from core.thread_key import ticket_id_from_eval

    tid = ticket_id_from_eval("eval-1")
    assert await store.create_ticket(TicketRow(tid, "T", "{}", 1.0)) is True
    assert await store.create_ticket(TicketRow(tid, "T", "{}", 1.0)) is False


async def test_unknown_ticket_state_is_open(store):
    assert await store.ticket_state("nope") == "open"


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------


async def test_record_and_list_dispositions(store):
    await store.record_disposition(DispositionRow("t1", "ignore", "self_message", 1.0))
    await store.record_disposition(DispositionRow("t2", "route", "ok", 2.0))
    rows = await store.recent_dispositions(10)
    assert len(rows) == 2
    assert rows[0].reason == "ok"  # 倒序：最新在前


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


async def test_concurrent_writes_no_error(store):
    """20 个并发写不应报 database is locked。"""
    await store.ensure_thread("T")
    await asyncio.gather(
        *(store.add_trigger(_trigger(f"t{i}")) for i in range(20))
    )
    for i in range(20):
        assert await store.get_trigger(f"t{i}") is not None


async def test_concurrent_trigger_dedup(store):
    """同一 id 并发写 10 次，只有一次返回 True。"""
    results = await asyncio.gather(*(store.add_trigger(_trigger("same")) for _ in range(10)))
    assert results.count(True) == 1


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


async def test_prune_removes_old_rows(store):
    """两种保留期不同（trigger 14 天 / disposition 30 天），取较长者来构造"很久以后"。"""
    old = 1000.0
    keep_days = max(limits.TRIGGER_RETENTION_DAYS, limits.DISPOSITION_RETENTION_DAYS)
    new = old + (keep_days + 1) * 86400
    await store.add_trigger(_trigger("old", ts=old))
    await store.add_trigger(_trigger("new", ts=new))
    await store.record_disposition(DispositionRow("old", "ignore", "x", old))
    await store.record_disposition(DispositionRow("new", "route", "ok", new))
    await store.prune(now=new)
    assert await store.get_trigger("old") is None
    assert await store.get_trigger("new") is not None
    rows = await store.recent_dispositions(10)
    assert [r.trigger_id for r in rows] == ["new"]


async def test_prune_keeps_recent_disposition(store):
    """保留期内的去向记录不能被误删——它是排查"消息去哪了"的唯一依据。"""
    await store.record_disposition(DispositionRow("t1", "ignore", "rate_limited", 1000.0))
    await store.prune(now=1000.0 + 86400)
    assert len(await store.recent_dispositions(10)) == 1


async def test_close_twice_is_safe(tmp_path):
    s = Store(tmp_path / "state.db")
    await s.open()
    await s.close()
    await s.close()


async def test_use_before_open_raises(tmp_path):
    s = Store(tmp_path / "state.db")
    with pytest.raises(RuntimeError):
        await s.get_trigger("x")
