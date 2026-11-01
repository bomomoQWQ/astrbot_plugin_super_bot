"""端到端：只靠 **Dashboard 配置**（不设任何环境变量）让插件工作。

跑法::

    python -m tests.e2e.config_path

为什么单独测这个：环境变量是我们开发时的快捷方式，但用户是在 Dashboard 里
点表单的。那条路走的是 ``_conf_schema.json`` + ``data/config/<插件>_config.json``，
和读环境变量完全是两条代码路径——必须单独验。

断言：
1. 插件就绪，且日志显示 ``eval_mode`` 来自配置（不是默认 ignore）
2. 发一条 ``sb:`` 消息能收到回复（说明全链路按配置生效）
3. 配置里的 rate_limit 真的起作用（第 N+1 条被限流丢弃）
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import (
    AstrBotInstance,
    clear_plugin_config,
    write_plugin_config,
)

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

    # 只写插件配置，环境变量一个都不设
    write_plugin_config(
        {
            "agent": {"eval_mode": "echo", "sandbox": False, "agent_steps": 3},
            "gate": {"rate_limit": 2, "rate_window_sec": 60.0},
        }
    )

    env = AstrBotInstance(extra_env={})
    bot: FakeOneBot | None = None
    try:
        await env.start()
        ok = await env.wait_log("[super-bot] initialized", timeout=20)
        check(ok, "插件就绪", env.log_tail(30))

        log = env.log_tail(200)
        check(
            "eval_mode=echo" in log,
            "eval_mode 来自配置文件（环境变量一个都没设）",
            log,
        )

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # --- 配置生效：能回话 -------------------------------------------
        await bot.send_group("sb: 配置生效了吗", user_id=20002)
        text = await bot.wait_for_text("ECHO-BATCH:", timeout=25)
        check(text is not None, "按配置走通了全链路（能回话）", env.log_tail(30))

        # --- 配置生效：限流阈值是 2 --------------------------------------
        bot.reset()
        before = env.log_text().count("gate=ignore reason=rate_limited")
        for i in range(6):
            await bot.send_group(f"随便聊聊 {i}", user_id=20002)
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.5)
        after = env.log_text().count("gate=ignore reason=rate_limited")
        check(
            after > before,
            f"配置里的 rate_limit=2 真的生效了（限流丢弃 {after - before} 条）",
            env.log_tail(40),
        )

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
        clear_plugin_config()

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    print(f"日志：{env.log_path}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
