"""真模型 + 真沙箱 端到端。

跑法::

    python -m tests.e2e.live_chain

链路：

    群里说一句话
      → Gate 判 route
      → Router 用**真模型**（DeepSeek）决定怎么回
      → 开 Ticket，交给 Worker
      → Worker 在**真沙箱**（Shipyard Neo / Bay）里执行 Python
      → 结果经 Outbox 发回群里

断言重点是「回复里同时出现模型的话和沙箱的计算结果」——
这一条通过，就说明两条外部依赖都真的接进了主流程。

⚠ 会花一点点模型额度、启动一个沙箱容器（约 3~8 秒）。
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
        },
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        c1 = await env.wait_log("[super-bot] initialized", timeout=20)
        c2 = await env.wait_log("sandbox enabled", timeout=10)
        check(c1, "插件就绪（eval_mode=live）", env.log_tail(20))
        check(c2, "沙箱已启用（Worker 会在真沙箱里执行）", env.log_tail(20))

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        await bot.send_group("大家好，今天有什么新鲜事吗？", user_id=20002)

        reply = None
        for _ in range(90):  # 模型 + 沙箱冷启动，给足 90 秒
            texts = bot.sent_texts()
            if texts:
                reply = texts[0]
                break
            await asyncio.sleep(1.0)

        check(reply is not None, "整条链路给出了回复", env.log_tail(40))
        if reply is not None:
            print(f"      回复：{reply!r}")
            check(len(reply.strip()) > 0, "回复非空")
            check(
                "42" in reply,
                "回复里带上了沙箱算出的 42（证明 Worker 真的在沙箱里跑）",
                f"reply={reply!r}",
            )
            check(
                "REBIND" not in reply.upper() and "ERROR" not in reply.upper(),
                "没有报错字样",
                f"reply={reply!r}",
            )

        log = env.log_text()
        check("sandbox " in log and "created" in log, "日志里有沙箱创建", env.log_tail(20))
        check("sandbox " in log and "destroyed" in log, "日志里有沙箱销毁（不留资源）", env.log_tail(20))
        check("live eval ->" in log, "日志里有真模型的评估结果", env.log_tail(20))

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
