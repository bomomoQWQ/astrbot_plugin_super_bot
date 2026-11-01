"""端到端：``/sb`` 子命令、直通派活、以及模型走工具派活。

验证三件新增的事：

1. ``/sb`` 不带参数 = 状态；``/sb help`` 必须是**另一段文字**（线上旧版
   就是因为没解析参数，两者一模一样）
2. ``/sb do <任务>`` 跳过 Router 的模型判断，直接开票给 Worker
3. 给原生 agent 注册的 ``superbot_dispatch`` 工具真的能被模型调起来，
   并且 Worker 的结果真的回到了模型上下文里（假模型会把工具结果念出来）

用的是 ``echo`` 模式 + 不开沙箱：Worker 走桩，返回 ``ECHO-BATCH:n``。
这样不用真模型也能把"入口 → 工单 → Worker → 回话"整条链跑实。
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from tests.e2e.fake_llm import FakeLLM
from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance

PASS = "[PASS]"
FAIL = "[FAIL]"


async def run() -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"{PASS} {label}")
        else:
            failures += 1
            print(f"{FAIL} {label}  {detail}")

    env = AstrBotInstance(
        extra_env={
            "SUPERBOT_EVAL_MODE": "echo",
            "SUPERBOT_EVAL_DELAY": "0.05",
            "SUPERBOT_WORKER_DELAY": "0.05",
        }
    )
    llm = FakeLLM()
    await llm.start()  # 模型工具那条链要用它
    bot: FakeOneBot | None = None
    try:
        await env.start()
        check(
            await env.wait_log("[super-bot] initialized", timeout=20),
            "插件就绪（eval_mode=echo）",
            env.log_tail(20),
        )
        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # --- 1. /sb 与 /sb status 是同一个，且是状态 -----------------------
        bot.reset()
        await bot.send_group("/sb", user_id=20002)
        status = await bot.wait_for_text("Super Bot 状态", timeout=20)
        check(status is not None, "/sb 报状态", env.log_tail(20))

        bot.reset()
        await bot.send_group("/sb status", user_id=20002)
        check(
            await bot.wait_for_text("Super Bot 状态", timeout=20) is not None,
            "/sb status 同样报状态",
            env.log_tail(20),
        )

        # --- 2. /sb help 必须是另一段文字 ---------------------------------
        bot.reset()
        await bot.send_group("/sb help", user_id=20002)
        help_text = await bot.wait_for_text("/sb do", timeout=20)
        check(help_text is not None, "/sb help 给出帮助", env.log_tail(20))
        check(
            help_text is not None and "Super Bot 状态" not in help_text,
            "help 和 status 不是同一个东西",
            str(help_text),
        )

        # --- 3. 未知子命令不能静默 ----------------------------------------
        bot.reset()
        await bot.send_group("/sb 乱写的", user_id=20002)
        unknown = await bot.wait_for_text("没有这个子命令", timeout=20)
        check(unknown is not None, "未知子命令会提示", env.log_tail(20))

        # --- 4. /sb do 直通 Worker（跳过 Router 的模型判断） ---------------
        bot.reset()
        await bot.send_group("/sb do 算一下 6*7", user_id=20002)
        ack = await bot.wait_for_text("去办了", timeout=20)
        check(ack is not None, "/sb do 立刻回执", env.log_tail(25))

        result = await bot.wait_for_text("ECHO-BATCH:", timeout=30)
        check(result is not None, "Worker 干完把结果发回来了", env.log_tail(30))

        log = env.log_text()
        check("direct dispatch" in log, "日志记了直通派工", env.log_tail(15))
        check(
            "direct dispatch ticket=" in log and "deliver=outbox" in log,
            "直通走的是 outbox 投递模式",
            env.log_tail(15),
        )

        # 直通不该经过 Router 的评估（Router 一次都不该醒）
        evals_before = log.count("router eval")
        bot.reset()
        await bot.send_group("/sb do 再算一次", user_id=20002)
        await bot.wait_for_text("ECHO-BATCH:", timeout=30)
        check(
            env.log_text().count("router eval") == evals_before,
            "/sb do 完全没惊动 Router",
            env.log_tail(20),
        )

        # --- 5. 模型通过工具派活 ------------------------------------------
        bot.reset()
        await bot.send_group(
            "[[tool:superbot_dispatch]] 帮我算一下 6*7", user_id=20002, at=True
        )
        echoed = await bot.wait_for_text("工具说：", timeout=45)
        check(echoed is not None, "模型调用了 superbot_dispatch", env.log_tail(40))
        check(
            echoed is not None and "ECHO-BATCH" in echoed,
            "Worker 的结果真的回到了模型上下文",
            str(echoed),
        )

        log = env.log_text()
        check("deliver=tool" in log, "工具路径用的是 tool 投递模式", env.log_tail(20))
        # tool 模式插件自己不许抢话，否则用户会收到两条
        replies = [t for t in bot.sent_texts() if "ECHO-BATCH" in t and "工具说" not in t]
        check(not replies, f"tool 模式没有重复发 Worker 原文（实际 {replies}）", str(replies))

    except Exception:
        failures += 1
        print(f"{FAIL} 异常终止")
        traceback.print_exc()
        print("--- AstrBot 日志尾部 ---")
        print(env.log_tail(100))
    finally:
        if bot is not None:
            await bot.close()
        await env.stop()
        await llm.stop()

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    print(f"日志：{env.log_path}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
