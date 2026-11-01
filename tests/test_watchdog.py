"""Watchdog 单测。

全部用注入时钟，零等待。覆盖：停滞判定、重试退避、升级、最终失败、
进展清零、以及常驻循环的启停。
"""

from __future__ import annotations

import asyncio

from superbot.watchdog import Action, ActionKind, Watchdog


def _wd(**kw) -> Watchdog:
    base = dict(stall_seconds=100.0, retries=2, backoff=(10.0, 20.0), max_escalation=2)
    base.update(kw)
    return Watchdog(**base)


def _kinds(actions: list[Action]) -> list[ActionKind]:
    return [a.kind for a in actions]


# ---------------------------------------------------------------------------
# 停滞判定
# ---------------------------------------------------------------------------


def test_no_action_before_threshold():
    wd = _wd()
    wd.watch("t1", now=0.0)
    assert wd.tick(now=99.0) == []


def test_stalled_then_retry():
    wd = _wd()
    wd.watch("t1", now=0.0)
    actions = wd.tick(now=100.0)
    # 第一次：报停滞 + 立刻安排第一次重试
    assert _kinds(actions) == [ActionKind.STALLED, ActionKind.RETRY]
    assert actions[1].attempt == 1


def test_retry_waits_for_backoff():
    wd = _wd()
    wd.watch("t1", now=0.0)
    wd.tick(now=100.0)  # stalled + retry#1，next=110
    assert wd.tick(now=105.0) == []  # 退避未到
    actions = wd.tick(now=110.0)
    assert _kinds(actions) == [ActionKind.RETRY]
    assert actions[0].attempt == 2


def test_escalate_after_retries_exhausted():
    wd = _wd()
    wd.watch("t1", now=0.0)
    wd.tick(now=100.0)  # retry#1
    wd.tick(now=110.0)  # retry#2
    actions = wd.tick(now=130.0)  # 重试用完 → 升级
    assert _kinds(actions) == [ActionKind.ESCALATE]
    assert actions[0].level == 1


def test_failed_after_max_escalation():
    wd = _wd()
    wd.watch("t1", now=0.0)
    wd.tick(now=100.0)  # retry#1（next=110）
    wd.tick(now=110.0)  # retry#2（next=130）
    assert _kinds(wd.tick(now=130.0)) == [ActionKind.ESCALATE]  # level 1，next=150
    assert _kinds(wd.tick(now=150.0)) == [ActionKind.ESCALATE]  # level 2，next=170
    actions = wd.tick(now=170.0)
    assert _kinds(actions) == [ActionKind.FAILED]
    assert "t1" in wd.failed
    # 失败后不再产生动作
    assert wd.tick(now=999.0) == []


def test_touch_clears_stall_and_retries():
    wd = _wd()
    item = wd.watch("t1", now=0.0)
    wd.tick(now=100.0)  # retry#1
    assert item.retries == 1
    wd.touch("t1", now=101.0)
    assert item.retries == 0
    assert item.stalled_reported is False
    # 重新计时：从 101 起再等 100 秒
    assert wd.tick(now=150.0) == []
    assert _kinds(wd.tick(now=201.0)) == [ActionKind.STALLED, ActionKind.RETRY]


def test_finish_removes_item():
    wd = _wd()
    wd.watch("t1", now=0.0)
    wd.finish("t1")
    assert wd.active() == 0
    assert wd.tick(now=999.0) == []


def test_multiple_tickets_independent():
    wd = _wd()
    wd.watch("a", now=0.0)
    wd.watch("b", now=50.0)
    actions = wd.tick(now=100.0)
    assert [a.ticket_id for a in actions] == ["a", "a"]  # b 还没到阈值


def test_stalled_reported_only_once():
    """停滞只报一次，之后的 tick 只出 retry/escalate，不重复刷 STALLED。"""
    wd = _wd()
    wd.watch("t1", now=0.0)
    wd.tick(now=100.0)
    actions = wd.tick(now=110.0)
    assert ActionKind.STALLED not in _kinds(actions)


def test_touch_unknown_is_noop():
    wd = _wd()
    wd.touch("nope", now=1.0)  # 不抛
    assert wd.active() == 0


# ---------------------------------------------------------------------------
# 常驻循环
# ---------------------------------------------------------------------------


async def test_run_loop_delivers_actions():
    wd = _wd(stall_seconds=0.05, retries=0, backoff=(0.01,), max_escalation=1)
    wd.watch("t1", now=0.0)
    got: list[ActionKind] = []

    async def on_action(a: Action) -> None:
        got.append(a.kind)

    await wd.run_loop(on_action, interval=0.02, max_ticks=3)
    assert ActionKind.ESCALATE in got or ActionKind.FAILED in got


async def test_run_loop_stops_on_event():
    wd = _wd()
    stop = asyncio.Event()
    ticks = {"n": 0}

    async def on_action(_: Action) -> None:
        ticks["n"] += 1

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        wd.run_loop(on_action, interval=0.02, stop=stop),
        stopper(),
    )
    assert stop.is_set()
