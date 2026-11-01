"""端到端冒烟脚本：真 AstrBot + 假模型 + 假 OneBot。

跑法::

    python -m tests.e2e.smoke

它会依次验证：

1. AstrBot 能起来，我们的插件能被加载（日志里有 ``[super-bot] initialized``）
2. 群里普通聊天（无 @）→ 插件记到 gate=route，但**不回复**
3. ``/help`` 指令 → 放行给原生流程，有回复
4. ``@机器人 你好`` → 主 agent 被唤醒 → 假模型被调用 → 有回复
5. 假模型自报家门（``FAKE-LLM-OK``），证明链路真的通到模型

任何一步失败都会打印 AstrBot 的日志尾部，方便定位。
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

    llm = FakeLLM()
    await llm.start()
    print(f"fake-llm: http://127.0.0.1:{llm.port}/v1")

    env = AstrBotInstance()
    bot: FakeOneBot | None = None
    try:
        print("启动 AstrBot ...")
        await env.start()
        print(f"AstrBot 就绪，OneBot 端口 {env.onebot_port}")

        loaded = await env.wait_log("[super-bot] initialized", timeout=15)
        check(loaded, "插件已加载并初始化", env.log_tail(20))

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()
        print(f"假 OneBot 已连上：{bot.url}")

        # --- 1. 普通群聊（无 @）：插件看到，但不该有任何回复 -----------------
        bot.reset()
        llm.reset()
        await bot.send_group("今天天气不错", user_id=20002)
        await asyncio.sleep(3.0)
        check(
            any("gate=route" in line for line in env.log_text().splitlines()),
            "普通群聊：Gate 判定为 route 并记录",
            env.log_tail(10),
        )
        check(bot.actions == [], "普通群聊：没有产生任何发送动作", str(bot.actions[:2]))
        check(llm.call_count == 0, "普通群聊：没有调用模型", f"calls={llm.call_count}")

        # --- 2. /help 指令：放行，走原生流程 --------------------------------
        bot.reset()
        await bot.send_group("/help", user_id=20002)
        action = await bot.wait_for_action("send_group_msg", timeout=20)
        check(action is not None, "/help 有回复", env.log_tail(20))

        # --- 2.5 /sb：插件自己的状态命令 ------------------------------------
        bot.reset()
        await bot.send_group("/sb", user_id=20002)
        status = await bot.wait_for_text("Super Bot 状态", timeout=20)
        check(status is not None, "/sb 能报状态", env.log_tail(20))
        if status:
            for word in ("Router", "Worker", "Watchdog"):
                check(word in status, f"状态里有 {word}", status)

        # --- 3. @机器人：唤醒主 agent，模型被调用 ---------------------------
        bot.reset()
        llm.reset()
        await bot.send_group("你好呀", user_id=20002, at=True)
        text = await bot.wait_for_text("FAKE-LLM-OK", timeout=45)
        check(text is not None, "@机器人 触发了主 agent 并拿到模型回复", env.log_tail(30))
        check(llm.call_count >= 1, "模型确实被调用", f"calls={llm.call_count}")

        # --- 4. 自消息不回环 ------------------------------------------------
        bot.reset()
        before = len(env.log_text())
        await bot.send_group("我是机器人自己", user_id=bot.self_id)
        await asyncio.sleep(2.0)
        check(
            "gate=ignore reason=self_message" in env.log_text()[before:],
            "自消息被判为 ignore（防回环）",
            env.log_tail(10),
        )
        check(bot.actions == [], "自消息没有引发任何发送", str(bot.actions[:2]))

    except Exception:
        failures += 1
        print(f"{FAIL} 异常终止")
        traceback.print_exc()
        print("--- AstrBot 日志尾部 ---")
        print(env.log_tail(80))
    finally:
        if bot is not None:
            await bot.close()
        await env.stop()
        await llm.stop()

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    print(f"日志：{env.log_path}")
    return 0 if failures == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
