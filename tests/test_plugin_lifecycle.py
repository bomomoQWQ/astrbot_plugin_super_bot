r"""插件生命周期测试：``initialize()`` / ``terminate()`` 的真实行为。

这类测试没法用"打一遍 e2e"覆盖：Windows 上 ``Popen.terminate()`` 是
``TerminateProcess``（硬杀），插件的 ``terminate()`` 根本不会跑。所以这里
**在进程内**直接构造插件对象、走一遍生命周期。

前置：能 import 到 AstrBot 源码（默认 ``F:\理解痞老板\AstrBot``，可用
``ASTRBOT_SRC`` 覆盖）。做不到就跳过。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
ASTRBOT_SRC = Path(os.environ.get("ASTRBOT_SRC", r"F:\理解痞老板\AstrBot"))

pytestmark = pytest.mark.skipif(
    not (ASTRBOT_SRC / "astrbot").is_dir(),
    reason=f"没有 AstrBot 源码：{ASTRBOT_SRC}",
)


def _load_plugin_module():
    """按文件路径加载 main.py，避免和 AstrBot 自己的 main.py 撞名。"""
    if str(ASTRBOT_SRC) not in sys.path:
        sys.path.insert(0, str(ASTRBOT_SRC))
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    spec = importlib.util.spec_from_file_location(
        "sb_plugin_under_test", REPO / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plugin_mod():
    try:
        return _load_plugin_module()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"加载插件模块失败：{exc}")


@pytest.fixture
def plugin(plugin_mod, tmp_path, monkeypatch):
    """造一个只带最少依赖的 context —— terminate 只会碰 shared_preferences。"""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    ctx = SimpleNamespace(
        shared_preferences=None,
        send_message=lambda *a, **k: None,
    )
    return plugin_mod.SuperBotPlugin(ctx, {})


def _superbot_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("superbot")]


# ---------------------------------------------------------------------------
# 基本生命周期
# ---------------------------------------------------------------------------


async def test_initialize_starts_everything(plugin):
    await plugin.initialize()
    try:
        assert plugin._store is not None, "状态库没打开"
        assert plugin._router is not None, "Router 没建"
        assert plugin._outbox is not None, "Outbox 没建"
        assert plugin._workers is not None, "Worker 池没建"
        assert plugin._router_task is not None
        assert not plugin._router_task.done(), "Router 循环应该还在跑"

        # 数据落在 plugin_data 下（不是插件目录，插件更新会被清）
        db = plugin._store._path
        assert Path(db).exists(), f"数据库文件不存在：{db}"
        assert "plugin_data" in str(db), f"数据库位置不对：{db}"
    finally:
        await plugin.terminate()


async def test_terminate_cleans_up(plugin):
    await plugin.initialize()
    store = plugin._store
    router_task = plugin._router_task
    assert store is not None and router_task is not None

    await plugin.terminate()

    assert plugin._store is None
    assert plugin._router_task is None
    assert plugin._outbox is None
    assert plugin._workers is None
    assert router_task.done(), "Router 任务应该已经结束"
    assert getattr(store, "_conn", None) is None, "数据库连接应该已关闭"
    assert _superbot_tasks() == [], f"还有残留任务：{_superbot_tasks()}"


async def test_terminate_is_idempotent(plugin):
    """重复 terminate 不能抛（框架在某些路径上会多调一次）。"""
    await plugin.initialize()
    await plugin.terminate()
    await plugin.terminate()


async def test_terminate_without_initialize_is_safe(plugin):
    await plugin.terminate()  # 没初始化过也不能炸


# ---------------------------------------------------------------------------
# 有活在跑的时候 terminate
# ---------------------------------------------------------------------------


async def test_terminate_with_pending_triggers(plugin, plugin_mod):
    await plugin.initialize()
    router = plugin._router
    assert router is not None

    # 塞几条 Trigger 但不让它们跑完
    for i in range(3):
        router.enqueue(
            plugin_mod.Trigger(
                id=f"t{i}",
                thread_key="T",
                umo="aiocqhttp:GroupMessage:1",
                actor_id="1",
                text=f"m{i}",
                ts=0.0,
            )
        )
    assert router.pending() == 3

    await plugin.terminate()
    assert _superbot_tasks() == []


async def test_terminate_with_running_worker(plugin, plugin_mod):
    """Worker 正在跑（且会被取消）时 terminate 也不该抛或留下任务。"""
    await plugin.initialize()
    workers = plugin._workers
    assert workers is not None

    async def slow(_ticket):
        await asyncio.sleep(30)
        return plugin_mod.WorkerResult(text="never", ok=True)

    workers.run = slow  # 换成一个真的会卡住的执行器
    aid = await workers.submit(
        plugin_mod.Ticket(id="tk-1", thread_key="T", mandate="m", created_at=0.0)
    )
    await asyncio.sleep(0.05)
    assert workers.running() == 1

    await plugin.terminate()

    a = workers.get(aid)
    assert a is not None and a.done, "工单应该已被取消并落终态"
    assert _superbot_tasks() == []


async def test_terminate_unregisters_stop_hooks(plugin, plugin_mod):
    """terminate 之后 registry 里不该还留着我们的合成事件。"""
    await plugin.initialize()
    registry = plugin_mod.active_event_registry
    if registry is None:
        pytest.skip("没有 ActiveEventRegistry")

    umo = "aiocqhttp:GroupMessage:42"
    plugin._register_stop_hook(umo, "ticket-x", "assign-x")
    assert umo in plugin._stop_events
    assert plugin._stop_events[umo] in registry._events.get(umo, set())

    await plugin.terminate()

    assert plugin._stop_events == {}
    leftover = [
        e for e in registry._events.get(umo, set()) if e in plugin._stop_events.values()
    ]
    assert leftover == []


# ---------------------------------------------------------------------------
# 沙箱保活
# ---------------------------------------------------------------------------


class FakeSandboxPort:
    """只实现 keepalive 的假端口。"""

    def __init__(self) -> None:
        self.kept: list[str] = []
        self.fail_for: set[str] = set()

    async def keepalive(self, sandbox_id: str) -> None:
        if sandbox_id in self.fail_for:
            raise RuntimeError("boom")
        self.kept.append(sandbox_id)


async def test_keepalive_task_starts_when_sandbox_enabled(
    plugin, monkeypatch
):
    """开了沙箱就必须起保活循环 —— 否则 TTL 到期工作区会被级联删除。"""
    monkeypatch.setenv("SUPERBOT_SANDBOX", "1")
    monkeypatch.setenv("BAY_API_KEY", "sk-bay-test")
    await plugin.initialize()
    try:
        assert plugin._sandbox is not None
        assert plugin._keepalive_task is not None, "没有起保活任务"
        assert plugin._keepalive_task.get_name() == "superbot-keepalive"
        assert not plugin._keepalive_task.done()
    finally:
        await plugin.terminate()

    assert plugin._keepalive_task is None
    assert _superbot_tasks() == []


async def test_keepalive_task_not_started_without_sandbox(plugin):
    await plugin.initialize()
    try:
        assert plugin._sandbox is None
        assert plugin._keepalive_task is None, "没沙箱不该起保活任务"
    finally:
        await plugin.terminate()


async def test_keepalive_really_calls_the_port(plugin, monkeypatch):
    """保活循环必须真的按间隔去调 keepalive。"""
    monkeypatch.setenv("SUPERBOT_KEEPALIVE_SEC", "0.05")
    await plugin.initialize()
    fake = FakeSandboxPort()
    try:
        plugin._sandbox = fake  # type: ignore[assignment]
        plugin._sandbox_ids.add("sb-1")
        plugin._start_keepalive()

        await asyncio.sleep(0.3)

        assert len(fake.kept) >= 2, f"应该被保活多次，实际 {fake.kept}"
        assert set(fake.kept) == {"sb-1"}
    finally:
        await plugin.terminate()

    assert _superbot_tasks() == []


async def test_keepalive_survives_failing_sandbox(plugin, monkeypatch):
    """一个沙箱保活失败不能拖垮循环，也不能影响别的沙箱。"""
    monkeypatch.setenv("SUPERBOT_KEEPALIVE_SEC", "0.05")
    await plugin.initialize()
    fake = FakeSandboxPort()
    fake.fail_for = {"bad"}
    try:
        plugin._sandbox = fake  # type: ignore[assignment]
        plugin._sandbox_ids.update({"bad", "good"})
        plugin._start_keepalive()

        await asyncio.sleep(0.3)
        assert "good" in fake.kept, "好沙箱应该照常保活"
    finally:
        await plugin.terminate()


async def test_no_sandbox_ids_means_no_calls(plugin, monkeypatch):
    monkeypatch.setenv("SUPERBOT_KEEPALIVE_SEC", "0.05")
    await plugin.initialize()
    fake = FakeSandboxPort()
    try:
        plugin._sandbox = fake  # type: ignore[assignment]
        plugin._start_keepalive()
        await asyncio.sleep(0.2)
        assert fake.kept == [], "没有活跃沙箱时不该发请求"
    finally:
        await plugin.terminate()
