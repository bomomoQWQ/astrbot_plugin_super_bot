"""端到端场景：Router 批处理 + Outbox 投递。

跑法::

    python -m tests.e2e.scenarios            # 全部场景
    python -m tests.e2e.scenarios batching   # 只跑某一个

与 ``smoke.py`` 的分工：smoke 验原生链路（指令 / @ / 不回环），
这里验我们自己那条链路（Gate → Router 批处理 → Outbox → send_message）。

桩评估器用 ``SUPERBOT_EVAL_MODE=echo`` + ``SUPERBOT_EVAL_DELAY=1.0``：
1 秒的假延迟用来模拟真实模型耗时——**评估进行期间的合并窗口就是它**；
空闲时的聚合由 Router 自己的 ``coalesce=0.15`` 负责。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import traceback

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance

PASS = "[PASS]"
FAIL = "[FAIL]"

EVAL_DELAY = 1.0
SETTLE = EVAL_DELAY + 1.5  # 等评估 + 投递落地


class Checker:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            print(f"{PASS} {label}")
        else:
            self.failures += 1
            print(f"{FAIL} {label}  {detail}")


def _batch_sizes(texts: list[str]) -> list[int]:
    return [int(m.group(1)) for t in texts if (m := re.search(r"ECHO-BATCH:(\d+)", t))]


async def _wait_count(env: AstrBotInstance, needle: str, expect: int, timeout: float) -> bool:
    """等到日志里某个串出现至少 ``expect`` 次。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if env.log_text().count(needle) >= expect:
            return True
        await asyncio.sleep(0.3)
    return False


async def scenario_batching(c: Checker) -> None:
    env = AstrBotInstance(
        extra_env={
            "SUPERBOT_EVAL_MODE": "echo",
            "SUPERBOT_EVAL_DELAY": str(EVAL_DELAY),
        }
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        c.check(
            await env.wait_log("[super-bot] initialized", timeout=15),
            "插件就绪（eval_mode=echo）",
            env.log_tail(20),
        )
        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # =================================================================
        # 1. 一个 Thread 突发三条 → 只评估一次、只回一条 batch=3
        # =================================================================
        bot.reset()
        for i in range(3):
            await bot.send_group(f"sb: 第 {i + 1} 条", user_id=20002)
            await asyncio.sleep(0.02)

        text = await bot.wait_for_text("ECHO-BATCH:", timeout=25)
        await asyncio.sleep(SETTLE)  # 等可能存在的后续批次落定
        sizes = _batch_sizes(bot.sent_texts())
        c.check(text is not None, "Router 经 Outbox 发出了回复", env.log_tail(30))
        c.check(sizes == [3], f"三条合并成一批（实际 {sizes}）", str(bot.sent_texts()))

        evals = [ln for ln in env.log_text().splitlines() if "router eval" in ln]
        c.check(len(evals) == 1, f"只发生一次评估（实际 {len(evals)}）", str(evals[-3:]))
        c.check(
            any("batch=3" in ln for ln in evals),
            "该次评估 batch=3",
            str(evals[-1:]),
        )

        # 回复是 Worker 产的，不是 Router 直接回的（证明链路完整）
        log = env.log_text()
        c.check("ticket " in log and "dispatched" in log, "Router 开了工单并派工", env.log_tail(15))
        c.check("worker done" in log, "Worker 真的执行完并回话", env.log_tail(15))

        # =================================================================
        # 2. 不带标记的消息 → 不回复（不刷屏）
        # =================================================================
        bot.reset()
        before = len(env.log_text())
        for i in range(3):
            await bot.send_group(f"随便聊聊 {i}", user_id=20002)
            await asyncio.sleep(0.02)
        await asyncio.sleep(SETTLE)
        c.check(bot.actions == [], "不带 sb: 标记不回复", str(bot.actions[:2]))
        c.check(
            "batch=3" in env.log_text()[before:],
            "但这三条同样被合并成一批（评估了，只是不说话）",
            env.log_tail(12),
        )

        # =================================================================
        # 3. 两个 Thread（不同群）各自成批，互不合并
        # =================================================================
        bot.reset()
        await bot.send_group("sb: 群一的事", user_id=20002)
        await asyncio.sleep(0.02)
        bot.group_id = 20002  # 换群 = 换 Thread
        await bot.send_group("sb: 群二的事", user_id=20002)
        await asyncio.sleep(SETTLE * 2)

        sizes = _batch_sizes(bot.sent_texts())
        groups = sorted(
            int(a.get("params", {}).get("group_id", 0)) for a in bot.actions
        )
        c.check(len(sizes) == 2, f"两个 Thread 各回一次（实际 {len(sizes)}）", str(bot.sent_texts()))
        c.check(sizes == [1, 1], f"两次都是单条成批（实际 {sizes}）", str(bot.sent_texts()))
        c.check(groups == [10001, 20002], f"回复发到了各自的群（实际 {groups}）", str(bot.actions))

    except Exception:
        c.failures += 1
        print(f"{FAIL} 异常终止")
        traceback.print_exc()
        print("--- AstrBot 日志尾部 ---")
        print(env.log_tail(80))
    finally:
        if bot is not None:
            await bot.close()
        await env.stop()


async def scenario_stop(c: Checker) -> None:
    """``/stop`` 必须能停到插件自己的 Worker —— 不是只停框架的 agent。

    做法：把每个会话的活跃任务登记进 ``ActiveEventRegistry``，
    框架内置的 ``/stop`` 顺着它就能找到我们（不用自己拦截命令，
    回执里的任务计数也因此是对的）。
    """
    env = AstrBotInstance(
        extra_env={
            "SUPERBOT_EVAL_MODE": "echo",
            "SUPERBOT_EVAL_DELAY": "0.1",
            "SUPERBOT_WORKER_DELAY": "30",  # 让 Worker 慢慢跑，好被取消
        }
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        c.check(
            await env.wait_log("[super-bot] initialized", timeout=15),
            "插件就绪",
            env.log_tail(20),
        )
        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # --- 1. 起一个长任务 ----------------------------------------------
        await bot.send_group("sb: 长任务", user_id=20002)
        started = await env.wait_log("dispatched", timeout=20)
        c.check(started, "工单已派给 Worker（任务在跑）", env.log_tail(25))

        before = env.log_text().count("dispatched")

        # --- 2. 发 /stop --------------------------------------------------
        bot.reset()
        await bot.send_group("/stop", user_id=20002)
        text = await bot.wait_for_text("stop", timeout=20)
        c.check(text is not None, "/stop 有回执", env.log_tail(25))
        if text:
            print(f"      回执：{text!r}")
            c.check(
                "1" in text,
                "回执里的任务计数是 1（说明框架找到了我们登记的任务）",
                f"reply={text!r}",
            )

        # --- 3. 插件侧确实执行了取消 --------------------------------------
        c.check(await env.wait_log("stop umo=", timeout=10), "插件记录了停止动作", env.log_tail(20))
        c.check(
            await env.wait_log("cancelled=1", timeout=10),
            "确实取消了 1 个正在跑的工单",
            env.log_tail(20),
        )
        c.check(
            await env.wait_log("outcome=cancelled", timeout=10),
            "被取消的工单落成 cancelled 终态",
            env.log_tail(20),
        )

        # --- 4. 停了之后不能自己又跑起来 ----------------------------------
        await asyncio.sleep(3.0)
        after = env.log_text().count("dispatched")
        c.check(after == before, f"停止后没有再开新工单（{before} -> {after}）", env.log_tail(20))

        # --- 5. 空转时 /stop 也不该炸 -------------------------------------
        bot.reset()
        await bot.send_group("/stop", user_id=20002)
        text2 = await bot.wait_for_text("No running tasks", timeout=15)
        c.check(text2 is not None, "没有任务时 /stop 仍有回执（且说没人跑）", env.log_tail(20))
        if text2:
            print(f"      回执：{text2!r}")

        # --- 6. 停完之后还能继续干活 --------------------------------------
        # 注意 Worker 桩的耗时被设成 30s（为了能被取消），所以这里只验
        # "Router 又开出了新工单"，不等它回话。
        before2 = env.log_text().count("dispatched")
        await bot.send_group("sb: 停完之后的新任务", user_id=20002)
        c.check(
            await _wait_count(env, "dispatched", before2 + 1, 20),
            "停止后 Router 仍能接新活（没被停死）",
            env.log_tail(20),
        )

    except Exception:
        c.failures += 1
        print(f"{FAIL} 异常终止")
        traceback.print_exc()
        print("--- AstrBot 日志尾部 ---")
        print(env.log_tail(80))
    finally:
        if bot is not None:
            await bot.close()
        await env.stop()


SCENARIOS = {"batching": scenario_batching, "stop": scenario_stop}


async def main(which: str | None) -> int:
    names = [which] if which else list(SCENARIOS)
    total = Checker()
    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"未知场景：{name}（可选：{', '.join(SCENARIOS)}）")
            return 2
        print(f"\n=== 场景：{name} ===")
        await fn(total)
    print()
    print("=" * 60)
    print("全部通过" if total.failures == 0 else f"失败 {total.failures} 项")
    return 0 if total.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
