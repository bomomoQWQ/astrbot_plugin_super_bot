"""拿**真实的** e2e 状态库跑一次迁移，确认升级不丢数据。

不是单测（单测用的是新建的 tmp 库），这里用的是
``.runtime/data/plugin_data/astrbot_plugin_super_bot/state.db``
——也就是前面所有 e2e 跑出来的那个库，里面是真数据。

做法：拷一份（连 WAL 一起拷），把 ``schema_version`` 表删掉伪装成 v0，
再用 ``Store`` 打开，看版本是否升到当前、数据是否还在。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REAL = REPO / ".runtime" / "data" / "plugin_data" / "astrbot_plugin_super_bot" / "state.db"

sys.path.insert(0, str(REPO))

from superbot import limits  # noqa: E402
from superbot.store import Store  # noqa: E402


def _copy_db(src: Path, dst_dir: Path) -> Path:
    """连同 -wal/-shm 一起拷，否则可能丢最近写入。"""
    dst = dst_dir / "state.db"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, Path(str(dst) + suffix))
    return dst


def _counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        out = {}
        for table in ("disposition", "trigger"):
            try:
                out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = -1
        return out
    finally:
        con.close()


def _version(path: Path) -> int | None:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT version FROM schema_version").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


async def main() -> int:
    if not REAL.exists():
        print(f"没有真实库：{REAL}（先跑一次 e2e）")
        return 2

    print(f"真实库：{REAL}  ({REAL.stat().st_size/1024:.0f} KB)")
    print(f"当前版本：{_version(REAL)}   行数：{_counts(REAL)}")

    with tempfile.TemporaryDirectory() as td:
        copy = _copy_db(REAL, Path(td))

        # 伪装成 v0：没有 schema_version 表
        con = sqlite3.connect(copy)
        con.execute("DROP TABLE IF EXISTS schema_version")
        con.commit()
        con.close()
        assert _version(copy) is None, "伪装失败，版本表还在"

        before = _counts(copy)
        print(f"伪装成 v0 后：版本={_version(copy)}  行数={before}")

        store = Store(copy)
        await store.open()
        try:
            v = await store.schema_version()
            print(f"迁移后版本：{v}（期望 {limits.SCHEMA_VERSION}）")
            assert v == limits.SCHEMA_VERSION
        finally:
            await store.close()

        after = _counts(copy)
        print(f"迁移后行数：{after}")
        for table, n in before.items():
            assert after[table] == n, f"{table} 数据丢了：{n} -> {after[table]}"
        assert set(after) >= {"disposition", "trigger"}

        # 迁移完还能写
        store = Store(copy)
        await store.open()
        try:
            from superbot.store import DispositionRow

            await store.record_disposition(
                DispositionRow(trigger_id=None, verdict="pass", reason="real-migrate", ts=1.0)
            )
        finally:
            await store.close()
        assert _counts(copy)["disposition"] == before["disposition"] + 1

    print()
    print("真实库迁移验证：通过（版本已升级、数据零丢失、迁移后可写）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
