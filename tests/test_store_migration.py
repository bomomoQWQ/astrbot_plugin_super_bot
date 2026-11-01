"""状态库**迁移**测试：老版本库能不能升到当前 schema。

为什么单独一个文件：``test_store.py`` 测的是 API 语义，这里测的是
"手里已经有一个旧库"这种现实情况——用户升级插件时库就在那儿，不能丢数据。

v0 的形态：只有 ``disposition`` 表、没有 ``schema_version``
（早期版本根本没写版本号）。
"""

from __future__ import annotations

import aiosqlite
import pytest

from superbot import limits
from superbot.store import Store

V0_DISPOSITION = """
CREATE TABLE IF NOT EXISTS disposition (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id TEXT,
  verdict    TEXT NOT NULL,
  reason     TEXT NOT NULL,
  ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disp_ts ON disposition(ts);
"""


async def _make_v0(path, rows: int = 3) -> None:
    """造一个"老版本"的库：有数据、没有版本号。"""
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(V0_DISPOSITION)
        for i in range(rows):
            await conn.execute(
                "INSERT INTO disposition(trigger_id, verdict, reason, ts) VALUES (?,?,?,?)",
                (None, "route", f"old-{i}", 1000.0 + i),
            )
        await conn.commit()
    finally:
        await conn.close()


async def _tables(path) -> set[str]:
    conn = await aiosqlite.connect(path)
    try:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r[0] for r in await cur.fetchall()}
    finally:
        await conn.close()


async def _count(path, table: str) -> int:
    conn = await aiosqlite.connect(path)
    try:
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        row = await cur.fetchone()
        return int(row[0])
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 基本迁移
# ---------------------------------------------------------------------------


async def test_v0_db_migrates_to_current(tmp_path):
    db = tmp_path / "state.db"
    await _make_v0(db, rows=3)

    store = Store(db)
    await store.open()
    try:
        assert await store.schema_version() == limits.SCHEMA_VERSION
    finally:
        await store.close()

    tables = await _tables(db)
    for name in ("schema_version", "trigger", "thread", "thread_pending", "ticket", "ticket_fact", "disposition"):
        assert name in tables, f"迁移后缺少表 {name}"


async def test_v0_data_survives_migration(tmp_path):
    """最要紧的一条：升级不能弄丢历史数据。"""
    db = tmp_path / "state.db"
    await _make_v0(db, rows=5)

    store = Store(db)
    await store.open()
    await store.close()

    assert await _count(db, "disposition") == 5


async def test_fresh_db_gets_version(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    await store.open()
    assert await store.schema_version() == limits.SCHEMA_VERSION
    await store.close()


async def test_migration_is_idempotent(tmp_path):
    """开两次不能重复迁移、也不能把版本号弄乱。"""
    db = tmp_path / "state.db"
    await _make_v0(db, rows=2)

    s1 = Store(db)
    await s1.open()
    v1 = await s1.schema_version()
    await s1.close()

    s2 = Store(db)
    await s2.open()
    v2 = await s2.schema_version()
    await s2.close()

    assert v1 == v2 == limits.SCHEMA_VERSION
    assert await _count(db, "disposition") == 2


async def test_store_usable_right_after_migration(tmp_path):
    """迁移完必须能立刻正常读写（不是"能开不能写"）。"""
    db = tmp_path / "state.db"
    await _make_v0(db, rows=1)

    store = Store(db)
    await store.open()
    try:
        await store.record_disposition(
            __import__("superbot.store", fromlist=["DispositionRow"]).DispositionRow(
                trigger_id=None, verdict="pass", reason="after-migrate", ts=2000.0
            )
        )
        n = await _count(db, "disposition")
        assert n == 2
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# 版本号异常
# ---------------------------------------------------------------------------


async def test_explicit_version_zero_also_migrates(tmp_path):
    """schema_version 表存在但值是 0 —— 也得迁移。"""
    db = tmp_path / "state.db"
    await _make_v0(db, rows=1)
    conn = await aiosqlite.connect(db)
    try:
        await conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        await conn.execute("INSERT INTO schema_version(version) VALUES (0)")
        await conn.commit()
    finally:
        await conn.close()

    store = Store(db)
    await store.open()
    assert await store.schema_version() == limits.SCHEMA_VERSION
    await store.close()


async def test_missing_migration_raises_clearly(tmp_path, monkeypatch):
    """缺迁移函数时要**明确报错**，而不是悄悄用旧 schema 跑下去。"""
    db = tmp_path / "state.db"
    store = Store(db)

    # 假装当前版本是 2，但只存在 0->1 的迁移
    monkeypatch.setattr(limits, "SCHEMA_VERSION", 2)

    with pytest.raises(RuntimeError) as ei:
        await store.open()
    msg = str(ei.value)
    assert "缺少" in msg and "1 -> 2" in msg

    if store._conn is not None:
        await store.close()
