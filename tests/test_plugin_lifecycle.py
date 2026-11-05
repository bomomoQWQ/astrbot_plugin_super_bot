r"""插件生命周期测试：``initialize()`` / ``terminate()`` 的真实行为。

这类测试没法用"打一遍 e2e"覆盖：Windows 上 ``Popen.terminate()`` 是
``TerminateProcess``（硬杀），插件的 ``terminate()`` 根本不会跑。所以这里
**在进程内**直接构造插件对象、走一遍生命周期。

前置：能 import 到 AstrBot 源码（用 ``ASTRBOT_SRC`` 指向那份 checkout，
默认找当前目录下的 ``AstrBot/``）。做不到就跳过。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _find_astrbot_src() -> Path | None:
    """找 AstrBot 源码：``ASTRBOT_SRC`` 优先，否则试几个常见位置。"""
    env = os.environ.get("ASTRBOT_SRC")
    if env:
        p = Path(env)
        return p if (p / "astrbot").is_dir() else None
    for cand in (REPO.parent / "AstrBot", REPO / "AstrBot", Path.home() / "AstrBot"):
        if (cand / "astrbot").is_dir():
            return cand
    return None


ASTRBOT_SRC = _find_astrbot_src() or Path("__missing__")

pytestmark = pytest.mark.skipif(
    ASTRBOT_SRC == Path("__missing__"),
    reason="没有 AstrBot 源码：设置 ASTRBOT_SRC 指向 AstrBot 仓库根目录",
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


# ---------------------------------------------------------------------------
# 配置优先级：Dashboard 配置 > 环境变量 > 默认值
# ---------------------------------------------------------------------------


def test_env_beats_config_for_eval_mode(plugin_mod, monkeypatch):
    """环境变量压过 Dashboard 配置。

    这条是被真事逼出来的：AstrBot 装完插件就按 schema 生成一份默认配置
    （``eval_mode: "ignore"``）。若配置优先，任何用环境变量控制行为的部署
    （容器编排、CI、我们的 e2e）都会被这份默认值悄悄盖掉——排查了很久。
    """
    monkeypatch.setenv("SUPERBOT_EVAL_MODE", "live")
    plugin_mod.configure({"agent": {"eval_mode": "echo"}})
    assert plugin_mod._eval_mode() == "live"
    assert plugin_mod._eval_mode_source() == "env"


def test_config_used_when_env_absent(plugin_mod, monkeypatch):
    """用户正常用法：不设环境变量，只在 Dashboard 里改。"""
    monkeypatch.delenv("SUPERBOT_EVAL_MODE", raising=False)
    plugin_mod.configure({"agent": {"eval_mode": "echo"}})
    assert plugin_mod._eval_mode() == "echo"
    assert plugin_mod._eval_mode_source() == "config"

    plugin_mod.configure({})
    assert plugin_mod._eval_mode() == "ignore"
    assert plugin_mod._eval_mode_source() == "default"


def test_config_sandbox_flag(plugin_mod, monkeypatch):
    monkeypatch.delenv("SUPERBOT_SANDBOX", raising=False)
    plugin_mod.configure({"agent": {"sandbox": True}})
    assert plugin_mod._sandbox_enabled() is True
    plugin_mod.configure({"agent": {"sandbox": False}})
    assert plugin_mod._sandbox_enabled() is False
    plugin_mod.configure({})
    assert plugin_mod._sandbox_enabled() is False  # 默认关


def test_agent_steps_is_only_a_fuse(plugin_mod):
    """步数不再是约束（ADR-14）：配什么都不会低于保险丝。"""
    ceiling = 999
    plugin_mod.configure({"agent": {"agent_steps": 0}})
    assert plugin_mod._agent_max_steps() == ceiling
    plugin_mod.configure({"agent": {"agent_steps": 6}})
    assert plugin_mod._agent_max_steps() == ceiling
    plugin_mod.configure({})
    assert plugin_mod._agent_max_steps() == ceiling


def test_task_budget_config(plugin_mod, monkeypatch):
    """真正的边界是时间预算，配置里能调。"""
    monkeypatch.delenv("SUPERBOT_TASK_BUDGET_SEC", raising=False)
    plugin_mod.configure({})
    assert plugin_mod._task_budget_sec() == 1200.0
    plugin_mod.configure({"agent": {"task_budget": 600}})
    assert plugin_mod._task_budget_sec() == 600.0
    # 太小会被抬到下限，避免配错了把任务秒杀
    plugin_mod.configure({"agent": {"task_budget": 1}})
    assert plugin_mod._task_budget_sec() == 60.0


def test_config_bay_settings(plugin_mod, monkeypatch):
    monkeypatch.delenv("BAY_API_KEY", raising=False)
    plugin_mod.configure(
        {"agent": {"bay_endpoint": "http://bay:9999", "bay_api_key": "sk-bay-from-config"}}
    )
    assert plugin_mod._bay_endpoint() == "http://bay:9999"
    assert plugin_mod._bay_key() == "sk-bay-from-config"
    plugin_mod.configure({})


def test_gate_conf_reads_gate_section(plugin, plugin_mod):
    """``gate`` 段里的阈值要真的进到 GateConfig 里。"""
    plugin_mod.configure({"gate": {"rate_limit": 7, "rate_window_sec": 12.5}})
    conf = plugin._build_gate_conf()
    assert conf.rate_limit == 7
    assert conf.rate_window_sec == 12.5
    plugin_mod.configure({})


# ---------------------------------------------------------------------------
# Watchdog 接线 / 落盘 / 状态命令
# ---------------------------------------------------------------------------


async def test_watchdog_and_housekeeping_started(plugin):
    await plugin.initialize()
    try:
        assert plugin._watchdog is not None
        assert plugin._watchdog_task is not None
        assert plugin._watchdog_task.get_name() == "superbot-watchdog"
        assert not plugin._watchdog_task.done()
        assert plugin._housekeeping_task is not None
        assert not plugin._housekeeping_task.done()
    finally:
        await plugin.terminate()
    assert plugin._watchdog is None
    assert plugin._watchdog_task is None
    assert plugin._housekeeping_task is None
    assert _superbot_tasks() == []


async def test_open_ticket_records_and_watches(plugin):
    """开工单要落库、记 accepted、并且被 watchdog 盯上。"""
    await plugin.initialize()
    try:
        await plugin._open_ticket("tk-1", "T", "live batch=1")
        assert plugin._watchdog is not None
        assert plugin._watchdog.active() == 1

        assert plugin._store is not None
        assert (await plugin._store.counts())["ticket"] == 1
        # 只有 accepted，还没有终态 → 算"没跑完"
        assert await plugin._store.open_tickets() == ["tk-1"]
        assert await plugin._store.ticket_state("tk-1") == "running"
    finally:
        await plugin.terminate()


async def test_delivered_fact_closes_ticket(plugin, plugin_mod):
    await plugin.initialize()
    try:
        await plugin._open_ticket("tk-1", "T", "m")
        await plugin._fact("tk-1", "delivered", "ok")
        assert plugin._store is not None
        assert await plugin._store.open_tickets() == []
        assert await plugin._store.ticket_state("tk-1") == "done"
    finally:
        await plugin.terminate()


async def test_reconcile_marks_interrupted_as_blocked(plugin):
    """重启对账：上一轮没跑完的工单如实记 blocked(restart)。"""
    await plugin.initialize()
    try:
        assert plugin._store is not None
        # 模拟"上一轮留下的"未完成工单
        from superbot.store import TicketRow

        await plugin._store.create_ticket(
            TicketRow(id="old", thread_key="T", mandate_json="m", created_at=0.0)
        )
        await plugin._store.add_fact("old", "accepted")

        await plugin._reconcile_after_restart()

        assert await plugin._store.open_tickets() == []
        assert await plugin._store.ticket_state("old") == "blocked"
    finally:
        await plugin.terminate()


async def test_watchdog_retry_resubmits_ticket(plugin, plugin_mod):
    """watchdog 判重试时要真的把工单重新提交一次。"""
    await plugin.initialize()
    try:
        workers = plugin._workers
        assert workers is not None

        async def hang(_ticket):
            await asyncio.sleep(30)

        workers.run = hang
        plugin._ticket_ctx["tk-1"] = {"umo": "aiocqhttp:GroupMessage:1", "thread_key": "T"}
        await plugin._open_ticket("tk-1", "T", "m")
        old_id = await workers.submit(
            plugin_mod.Ticket(id="tk-1", thread_key="T", mandate="m", created_at=0.0)
        )
        plugin._assignment_ids["tk-1"] = old_id
        await asyncio.sleep(0.05)

        await plugin._on_watchdog_action(
            plugin_mod.Action(kind=plugin_mod.ActionKind.RETRY, ticket_id="tk-1", attempt=1)
        )

        new_id = plugin._assignment_ids["tk-1"]
        assert new_id != old_id, "应该提交了一次新的执行"
        assert workers.get(old_id) is not None
        assert workers.get(old_id).outcome is plugin_mod.Outcome.CANCELLED
    finally:
        await plugin.terminate()


async def test_watchdog_failed_blocks_and_notifies(plugin, plugin_mod):
    sent: list[str] = []

    async def fake_send(umo: str, text: str) -> bool:
        sent.append(text)
        return True

    await plugin.initialize()
    try:
        plugin._outbox.send = fake_send  # type: ignore[union-attr]
        plugin._ticket_ctx["tk-1"] = {"umo": "aiocqhttp:GroupMessage:1", "thread_key": "T"}
        await plugin._open_ticket("tk-1", "T", "m")

        await plugin._on_watchdog_action(
            plugin_mod.Action(kind=plugin_mod.ActionKind.FAILED, ticket_id="tk-1")
        )

        assert plugin._watchdog is not None
        assert plugin._watchdog.active() == 0
        assert plugin._store is not None
        assert await plugin._store.ticket_state("tk-1") == "blocked"
        assert plugin._ticket_ctx.get("tk-1") is None, "失败后要清掉上下文"
        assert any("放弃" in t for t in sent), f"应该通知用户，实际 {sent}"
    finally:
        await plugin.terminate()


async def test_status_text_has_sections(plugin):
    await plugin.initialize()
    try:
        text = await plugin._status_text()
        for word in ("Super Bot 状态", "Router", "Worker", "Watchdog", "库"):
            assert word in text, f"状态里缺 {word}：\n{text}"
    finally:
        await plugin.terminate()


async def test_prune_once_cleans_old_rows(plugin, plugin_mod):
    await plugin.initialize()
    try:
        assert plugin._store is not None
        old = time.time() - 400 * 86400
        await plugin._store.record_disposition(
            plugin_mod.DispositionRow(
                trigger_id=None, verdict="route", reason="old", ts=old
            )
        )
        assert (await plugin._store.counts())["disposition"] == 1
        await plugin._prune_once()
        assert (await plugin._store.counts())["disposition"] == 0
    finally:
        await plugin.terminate()


# ---------------------------------------------------------------------------
# /sb 子命令与直通派活
# ---------------------------------------------------------------------------


class _FakeCommandEvent:
    """只够 /sb 用的最小事件替身。"""

    def __init__(self, text: str, umo: str = "aiocqhttp:GroupMessage:1"):
        self.message_str = text
        self.unified_msg_origin = umo

    def plain_result(self, text: str) -> str:
        return text


async def _run_sb(plugin, text: str) -> str:
    ev = _FakeCommandEvent(text)
    out = ""
    async for r in plugin.sb_command(ev):
        out += r
    return out


async def test_sb_default_is_status(plugin):
    await plugin.initialize()
    try:
        assert "Super Bot 状态" in await _run_sb(plugin, "/sb")
        assert "Super Bot 状态" in await _run_sb(plugin, "/sb status")
    finally:
        await plugin.terminate()


async def test_sb_help_is_not_status(plugin):
    """这俩必须不一样——线上旧版就是因为没解析参数才一模一样。"""
    await plugin.initialize()
    try:
        help_text = await _run_sb(plugin, "/sb help")
        status_text = await _run_sb(plugin, "/sb")
        assert "/sb do" in help_text
        assert "Super Bot 状态" not in help_text
        assert help_text != status_text
    finally:
        await plugin.terminate()


async def test_sb_unknown_subcommand_shows_help(plugin):
    await plugin.initialize()
    try:
        text = await _run_sb(plugin, "/sb 乱写")
        assert "没有这个子命令" in text
        assert "/sb do" in text
    finally:
        await plugin.terminate()


async def test_sb_do_without_task_shows_usage(plugin):
    await plugin.initialize()
    try:
        assert "用法" in await _run_sb(plugin, "/sb do")
    finally:
        await plugin.terminate()


async def test_dispatch_outbox_mode_speaks(plugin):
    sent: list[str] = []

    async def fake_send(umo: str, text: str) -> bool:
        sent.append(text)
        return True

    await plugin.initialize()
    try:
        plugin._outbox.send = fake_send  # type: ignore[union-attr]
        tid = await plugin._dispatch_task(
            "6*7", umo="aiocqhttp:GroupMessage:1", thread="T", deliver="outbox"
        )
        assert tid is not None
        done = await plugin._workers.wait(plugin._assignment_ids[tid], timeout=10)
        assert done is not None
        assert sent, "outbox 模式应该自己发消息"
        assert plugin._store is not None
        assert await plugin._store.ticket_state(tid) == "done"
    finally:
        await plugin.terminate()


async def test_dispatch_tool_mode_stays_silent(plugin):
    """deliver=tool：结果交给模型，插件不许抢话（否则用户收两条）。"""
    sent: list[str] = []

    async def fake_send(umo: str, text: str) -> bool:
        sent.append(text)
        return True

    await plugin.initialize()
    try:
        plugin._outbox.send = fake_send  # type: ignore[union-attr]
        tid = await plugin._dispatch_task(
            "6*7", umo="aiocqhttp:GroupMessage:1", thread="T", deliver="tool"
        )
        assert tid is not None
        done = await plugin._workers.wait(plugin._assignment_ids[tid], timeout=10)
        assert done is not None and done.text
        assert sent == [], f"tool 模式不该自己发消息，实际 {sent}"
        assert plugin._store is not None
        assert await plugin._store.ticket_state(tid) == "done"
    finally:
        await plugin.terminate()


# ---------------------------------------------------------------------------
# 技能注入
# ---------------------------------------------------------------------------


def _make_skill(tmp_path, name: str = "demo", desc: str = "做演示") -> None:
    d = tmp_path / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n照这个做。\n",
        encoding="utf-8",
    )


async def test_initialize_loads_skills(plugin, tmp_path):
    _make_skill(tmp_path)
    await plugin.initialize()
    try:
        assert plugin._skills is not None
        names = [s.name for s in plugin._skills.list()]
        assert "demo" in names, f"没扫到自己放的技能：{names}"
        status = await plugin._status_text()
        assert "技能：" in status
    finally:
        await plugin.terminate()


async def test_worker_prompt_gets_skill_index(plugin, tmp_path):
    """Worker 是插件自己开的一次 agent，框架管不到——必须自己注入清单。"""
    _make_skill(tmp_path, "demo", "做演示用")
    await plugin.initialize()
    try:
        prompt = plugin._worker_system_prompt()
        assert "demo: 做演示用" in prompt
        assert "read_skill" in prompt
    finally:
        await plugin.terminate()


async def test_read_skill_tool_reads_body(plugin, tmp_path):
    _make_skill(tmp_path)
    await plugin.initialize()
    try:
        text = await plugin.read_skill(None, "demo", "")
        assert "照这个做" in text
    finally:
        await plugin.terminate()
