"""端到端：**真模型 + 真沙箱 + 真工具调用**（Worker 真的在干活）。

跑法::

    python -m tests.e2e.live_chain

链路：

    群里说一句需要动手算的话
      → Gate 判 route
      → Router 用真模型判断"要不要理"
      → 开 Ticket，交给 Worker
      → Worker 起真沙箱，把 run_python 等工具交给模型
      → 模型**调用工具**、在沙箱里算出结果
      → 结果经 Outbox 发回群里

断言的关键是那个乘积——模型很难光靠脑子算对 987654321×123456789，
而且日志里必须出现 ``tool run_python``，证明工具真的被调用了。

⚠ 会花一点模型额度，并启动一个沙箱容器。
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance, config_with_real_model

PASS = "[PASS]"
FAIL = "[FAIL]"

MODEL = "deepseek-flash"
EXPECTED_PRODUCT = "121932631112635269"  # 987654321 * 123456789
QUESTION = "帮我用 python 精确算一下 987654321 乘 123456789 等于多少，要准确结果。"


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
        config=config_with_real_model(model=MODEL),
        extra_env={
            "SUPERBOT_EVAL_MODE": "live",
            "SUPERBOT_EVAL_DELAY": "0",
            "SUPERBOT_SANDBOX": "1",
            # 保活间隔调小，好在一次 e2e 里看到它真的发请求
            "SUPERBOT_KEEPALIVE_SEC": "0.5",
        },
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        c1 = await env.wait_log("[super-bot] initialized", timeout=20)
        c2 = await env.wait_log("sandbox enabled", timeout=10)
        check(c1, "插件就绪（eval_mode=live）", env.log_tail(20))
        check(c2, "沙箱已启用", env.log_tail(20))

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        await bot.send_group(QUESTION, user_id=20002)

        reply = None
        for _ in range(120):  # 模型 + 沙箱冷启动 + 工具循环，给足 120 秒
            texts = bot.sent_texts()
            if texts:
                reply = texts[0]
                break
            await asyncio.sleep(1.0)

        check(reply is not None, "整条链路给出了回复", env.log_tail(50))
        if reply is not None:
            print(f"      回复：{reply!r}")
            check(len(reply.strip()) > 0, "回复非空")

        log = env.log_text()
        check(
            "tool run_python" in log,
            "模型真的调用了 run_python（工具链通了）",
            env.log_tail(40),
        )
        check(
            EXPECTED_PRODUCT in (reply or ""),
            f"回复里有沙箱算出的精确结果 {EXPECTED_PRODUCT}",
            f"reply={reply!r}",
        )
        check("sandbox " in log and "created" in log, "沙箱已创建", env.log_tail(20))
        check(
            "sandbox " in log and "destroyed" in log,
            "沙箱已销毁（不留资源）",
            env.log_tail(20),
        )
        check("keepalive started" in log, "保活循环已启动", env.log_tail(20))
        check("live eval ->" in log, "日志里有 Router 的评估结果", env.log_tail(20))
        check(
            "agent done" in log,
            "Worker 的工具循环正常结束",
            env.log_tail(30),
        )

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

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    print(f"日志：{env.log_path}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
