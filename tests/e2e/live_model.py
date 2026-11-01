"""真实模型端到端：把 AstrBot 接到**真模型**上跑一轮。

跑法::

    python -m tests.e2e.live_model

它回答一个问题：这个插件能不能直接复用本机已有的模型凭据？

凭据来源：``~/.dsh/.credentials.yaml`` 里的 ``DEEPSEEK_API_KEY``
（就是当前 DSH 会话用的那把钥匙），端点换成 DeepSeek 官方
``https://api.deepseek.com/v1``，模型 ``deepseek-flash``。

⚠ 这会真的花掉一点点额度（一次问答，几十 token）。
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance, deepseek_provider, config_with_real_model

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

    provider = deepseek_provider(MODEL)
    check(provider is not None, "从 DSH 凭据里拿到 DEEPSEEK_API_KEY")
    if provider is None:
        print("      （没找到凭据，跳过）")
        return failures
    print(f"      provider={provider['id']} model={provider['model']} base={provider['api_base']}")

    env = AstrBotInstance(config=config_with_real_model(model=MODEL))
    bot: FakeOneBot | None = None
    try:
        await env.start()
        c1 = await env.wait_log("[super-bot] initialized", timeout=15)
        check(c1, "插件就绪（provider 指向真模型）", env.log_tail(20))

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        question = "请只回答一个数字：1+1 等于几？"
        await bot.send_group(question, user_id=20002, at=True)

        deadline_ok = False
        reply = None
        for _ in range(60):  # 最多 ~60s
            texts = bot.sent_texts()
            if texts:
                reply = texts[0]
                deadline_ok = True
                break
            await asyncio.sleep(1.0)

        check(deadline_ok, "真模型在 60 秒内给出了回复", env.log_tail(30))
        if reply is not None:
            print(f"      模型回复：{reply!r}")
            check(len(reply.strip()) > 0, "回复非空")
            check(
                "2" in reply or "二" in reply or "两" in reply,
                "回复内容符合预期（1+1=2）",
                f"reply={reply!r}",
            )

        # 确认这次没有走假模型（假模型固定回 FAKE-LLM-OK / ECHO:…）
        check(
            "FAKE-LLM-OK" not in (reply or ""),
            "确实没走假模型",
            f"reply={reply!r}",
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

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    print(f"日志：{env.log_path}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
