"""状态库：`data/plugin_data/<plugin>/state.db`（SQLite）。

为什么是 SQLite 而不是 JSON（docs/调查/02-state.md）：

- 插件更新会 ``remove_dir(target_plugin_path)`` **整目录重放**
  （star_manager.py:2115-2119）→ 放在插件目录里的状态必失；
  ``plugin_data/`` 在备份白名单里，主库自建表反而不在
  （backup/constants.py:43-57,75-83）。
- ``triggers`` / ``dispositions`` 是追加型数据。
- ``aiosqlite`` 是 AstrBot 硬依赖（requirements.txt:4），WAL + busy_timeout
  的参数直接抄自 db/__init__.py:40-52。

两条设计原则：

1. **存事实，算状态**（``ticket_state_from_facts``）。永不出现"状态说 running
   但 worker 早死了"。
2. **幂等由主键保证**：``trigger.id`` / ``ticket.id`` 都是内容哈希，重复写入
   用 ``INSERT OR IGNORE`` 天然去重。

本模块**不 import astrbot**，可在无 AstrBot 环境下单测。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

import aiosqlite

from . import limits

__all__ = [
    "DispositionRow",
    "Store",
    "TicketRow",
    "TriggerRow",
    "ticket_state_from_facts",
]


# ---------------------------------------------------------------------------
# 数据行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerRow:
    id: str
    source: str
    thread_key: str
    umo: str
    actor_id: str
    text: str
    ts: float
    raw_ref: str | None = None


@dataclass(frozen=True)
class TicketRow:
    id: str
    thread_key: str
    mandate_json: str
    created_at: float


@dataclass(frozen=True)
class DispositionRow:
    trigger_id: str | None
    verdict: str
    reason: str
    ts: float


# ---------------------------------------------------------------------------
# 状态计算（纯函数）
# ---------------------------------------------------------------------------

_FACT_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("human_stopped", "stopped"),
    ("delivered", "done"),
    ("blocked", "blocked"),
    ("worker_failed", "retrying"),
    ("accepted", "running"),
)
"""事实 → 状态的优先级表。越靠前越"终局"。"""


def ticket_state_from_facts(kinds: Iterable[str]) -> str:
    """由事实集合算出工单状态。

    参考实现与 Shipyard Neo 都用同一招：状态不是存储字段，而是从事实实时算出
    （Sandbox 状态由当前 Session 状态计算得出）。这样永远不会不一致。

    Returns:
        ``stopped`` / ``done`` / ``blocked`` / ``retrying`` / ``running`` / ``open``
    """
    ks = set(kinds)
    for kind, state in _FACT_PRECEDENCE:
        if kind in ks:
            return state
    return "open"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS trigger (
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  thread_key  TEXT NOT NULL,
  umo         TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  text        TEXT NOT NULL,
  ts          REAL NOT NULL,
  raw_ref     TEXT
);
CREATE INDEX IF NOT EXISTS idx_trigger_thread ON trigger(thread_key, ts);

CREATE TABLE IF NOT EXISTS thread (
  key         TEXT PRIMARY KEY,
  evaluating  TEXT,
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_pending (
  thread_key  TEXT NOT NULL,
  trigger_id  TEXT NOT NULL,
  PRIMARY KEY (thread_key, trigger_id)
);

CREATE TABLE IF NOT EXISTS ticket (
  id           TEXT PRIMARY KEY,
  thread_key   TEXT NOT NULL,
  mandate_json TEXT NOT NULL,
  created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_thread ON ticket(thread_key, created_at);

CREATE TABLE IF NOT EXISTS ticket_fact (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  kind      TEXT NOT NULL,
  payload   TEXT,
  ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_ticket ON ticket_fact(ticket_id, id);

CREATE TABLE IF NOT EXISTS disposition (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id TEXT,
  verdict    TEXT NOT NULL,
  reason     TEXT NOT NULL,
  ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disp_ts ON disposition(ts);
"""


async def _migrate_v0_to_v1(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_V1)


_MIGRATIONS: dict[int, Callable[[aiosqlite.Connection], Any]] = {
    0: _migrate_v0_to_v1,
}
"""``from_version -> 迁移函数``。新增表/列时在这里加一条并把 limits.SCHEMA_VERSION +1。"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """状态库封装。

    写操作全部串行化（进程内 ``asyncio.Lock``）；跨进程另有 ``filelock`` 可用，
    但当前部署形态是单实例，暂不需要。

    用法::

        store = Store(StarTools.get_data_dir() / limits.DB_FILENAME)
        await store.open()
        ...
        await store.close()
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = None  # 惰性创建，避免在 import 时绑定事件循环

    # -- 生命周期 ---------------------------------------------------------

    async def open(self) -> None:
        import asyncio

        self._write_lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(f"PRAGMA busy_timeout={limits.SQLITE_BUSY_TIMEOUT_MS}")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._conn is None:
            raise RuntimeError("Store 未 open()")
        yield self._conn

    # -- 迁移 -------------------------------------------------------------

    async def _read_version(self) -> int:
        async with self._db() as db:
            cur = await db.execute("SELECT version FROM schema_version LIMIT 1")
            row = await cur.fetchone()
            return int(row["version"]) if row else 0

    async def _migrate(self) -> None:
        async with self._db() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            await db.commit()
        version = await self._read_version()
        while version < limits.SCHEMA_VERSION:
            step = _MIGRATIONS.get(version)
            if step is None:
                raise RuntimeError(
                    f"缺少 {version} -> {version + 1} 的迁移；"
                    "请补 _MIGRATIONS 并确认 limits.SCHEMA_VERSION"
                )
            async with self._db() as db:
                await step(db)
                await db.execute("DELETE FROM schema_version")
                await db.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (version + 1,)
                )
                await db.commit()
            version += 1

    async def schema_version(self) -> int:
        return await self._read_version()

    # -- trigger ----------------------------------------------------------

    async def add_trigger(self, row: TriggerRow) -> bool:
        """写入一条 Trigger。

        Returns:
            ``True`` 表示新插入；``False`` 表示已存在（幂等命中）。
        """
        async with self._write_lock:
            async with self._db() as db:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO trigger"
                    "(id, source, thread_key, umo, actor_id, text, ts, raw_ref) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        row.id,
                        row.source,
                        row.thread_key,
                        row.umo,
                        row.actor_id,
                        row.text,
                        row.ts,
                        row.raw_ref,
                    ),
                )
                await db.commit()
                return cur.rowcount > 0

    async def get_trigger(self, trigger_id: str) -> TriggerRow | None:
        async with self._db() as db:
            cur = await db.execute("SELECT * FROM trigger WHERE id = ?", (trigger_id,))
            r = await cur.fetchone()
        if r is None:
            return None
        return TriggerRow(
            id=r["id"],
            source=r["source"],
            thread_key=r["thread_key"],
            umo=r["umo"],
            actor_id=r["actor_id"],
            text=r["text"],
            ts=r["ts"],
            raw_ref=r["raw_ref"],
        )

    # -- thread -----------------------------------------------------------

    async def ensure_thread(self, key: str, now: float | None = None) -> None:
        async with self._write_lock:
            async with self._db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO thread(key, created_at) VALUES (?,?)",
                    (key, now if now is not None else time.time()),
                )
                await db.commit()

    async def push_pending(self, thread_key: str, trigger_id: str) -> None:
        """把 Trigger 追加进该 Thread 的待评估队列（只追加，不去重之外做任何事）。"""
        async with self._write_lock:
            async with self._db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO thread_pending(thread_key, trigger_id) "
                    "VALUES (?,?)",
                    (thread_key, trigger_id),
                )
                await db.commit()

    async def pending_count(self, thread_key: str) -> int:
        async with self._db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM thread_pending WHERE thread_key = ?",
                (thread_key,),
            )
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def begin_eval(self, thread_key: str, eval_id: str) -> bool:
        """抢占该 Thread 的评估单槽。

        Returns:
            ``True`` 表示抢到（此前空闲）；``False`` 表示已有评估在跑。
        """
        async with self._write_lock:
            async with self._db() as db:
                cur = await db.execute(
                    "UPDATE thread SET evaluating = ? "
                    "WHERE key = ? AND (evaluating IS NULL OR evaluating = '')",
                    (eval_id, thread_key),
                )
                await db.commit()
                return cur.rowcount > 0

    async def end_eval(self, thread_key: str, eval_id: str) -> None:
        async with self._write_lock:
            async with self._db() as db:
                await db.execute(
                    "UPDATE thread SET evaluating = NULL "
                    "WHERE key = ? AND evaluating = ?",
                    (thread_key, eval_id),
                )
                await db.commit()

    async def drain_pending(self, thread_key: str, limit: int) -> list[str]:
        """一次取出（并删除）至多 ``limit`` 条待评估 Trigger。

        这就是"单槽 + 只追加"里的排空动作：合并窗口自动等于上一次评估的存活
        时长，不需要任何定时器。
        """
        async with self._write_lock:
            async with self._db() as db:
                cur = await db.execute(
                    "SELECT trigger_id FROM thread_pending WHERE thread_key = ? "
                    "ORDER BY rowid LIMIT ?",
                    (thread_key, limit),
                )
                rows = await cur.fetchall()
                ids = [r["trigger_id"] for r in rows]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    await db.execute(
                        f"DELETE FROM thread_pending WHERE thread_key = ? "
                        f"AND trigger_id IN ({placeholders})",
                        (thread_key, *ids),
                    )
                    await db.commit()
                return ids

    # -- ticket -----------------------------------------------------------

    async def create_ticket(self, row: TicketRow) -> bool:
        async with self._write_lock:
            async with self._db() as db:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO ticket(id, thread_key, mandate_json, created_at) "
                    "VALUES (?,?,?,?)",
                    (row.id, row.thread_key, row.mandate_json, row.created_at),
                )
                await db.commit()
                return cur.rowcount > 0

    async def add_fact(
        self,
        ticket_id: str,
        kind: str,
        payload: str | None = None,
        now: float | None = None,
    ) -> None:
        """追加一条事实。状态由事实算出，永不直接写状态字段。"""
        async with self._write_lock:
            async with self._db() as db:
                await db.execute(
                    "INSERT INTO ticket_fact(ticket_id, kind, payload, ts) VALUES (?,?,?,?)",
                    (ticket_id, kind, payload, now if now is not None else time.time()),
                )
                await db.commit()

    async def ticket_facts(self, ticket_id: str) -> list[str]:
        async with self._db() as db:
            cur = await db.execute(
                "SELECT kind FROM ticket_fact WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            )
            rows = await cur.fetchall()
        return [r["kind"] for r in rows]

    async def ticket_state(self, ticket_id: str) -> str:
        return ticket_state_from_facts(await self.ticket_facts(ticket_id))

    # -- disposition ------------------------------------------------------

    async def record_disposition(self, row: DispositionRow) -> None:
        """记录一条"去向"：谁被丢弃、放行或路由，以及为什么。

        这张表是排查"消息去哪了"唯一可靠的依据——没有它，你会分不清是
        Gate 判错了、还是 Router 没收到。
        """
        async with self._write_lock:
            async with self._db() as db:
                await db.execute(
                    "INSERT INTO disposition(trigger_id, verdict, reason, ts) "
                    "VALUES (?,?,?,?)",
                    (row.trigger_id, row.verdict, row.reason, row.ts),
                )
                await db.commit()

    async def recent_dispositions(self, limit: int = 50) -> list[DispositionRow]:
        async with self._db() as db:
            cur = await db.execute(
                "SELECT trigger_id, verdict, reason, ts FROM disposition "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            DispositionRow(
                trigger_id=r["trigger_id"],
                verdict=r["verdict"],
                reason=r["reason"],
                ts=r["ts"],
            )
            for r in rows
        ]

    # -- 清理 -------------------------------------------------------------

    async def prune(self, now: float | None = None) -> None:
        """按保留期清理追加型数据，防止无限增长。"""
        ts = now if now is not None else time.time()
        trigger_before = ts - limits.TRIGGER_RETENTION_DAYS * 86400
        disp_before = ts - limits.DISPOSITION_RETENTION_DAYS * 86400
        async with self._write_lock:
            async with self._db() as db:
                await db.execute("DELETE FROM trigger WHERE ts < ?", (trigger_before,))
                await db.execute(
                    "DELETE FROM disposition WHERE ts < ?", (disp_before,)
                )
                await db.commit()
