"""端到端：本轮五件事（异步派活 / 去沙箱 / 预算 / 订阅直通 / 接管提示词）。

这五条改动单独看都不复杂，但互相咬合——主 agent 没手了就必须派活，
派活必须异步才不堵对话，异步了就必须有别的东西管住跑疯的任务。
所以它们得一起验，光靠单测看不出问题。

验证方式都是在**真 AstrBot 进程**里发消息、看日志、看假模型收到的请求：

1. 启动日志里 ``superbot_dispatch -> 后台任务模式``（①）
2. 主 agent 的工具表里**没有** ``astrbot_execute_shell``，但有我们的工具（②）
3. 主 agent 的 system prompt 里**没有** "enable Computer Use"，有 ``[执行能力]``（⑤）
4. ``/sb ls`` 与 ``/sb log`` 都能回话（②）
5. 模型派活时拿到的是 "Background task submitted"，结果随后自己回来（①）
6. 同一 Thread 有活跃工单时，后续消息只观测不决策（④）

运行（需要 ASTRBOT_SRC 指向 AstrBot 源码）::

    python -m tests.e2e.five
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


def _system_prompt(req: dict) -> str:
    """从假模型收到的请求里取 system prompt。"""
    for msg in req.get("messages") or []:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""


def _request_text(req: dict) -> str:
    """把一次模型请求的所有消息拼成一坨，方便找某段文字。"""
    parts: list[str] = []
    for msg in req.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict):
                    parts.append(str(p.get("text", "")))
    return "\n".join(parts)


def _tool_names(req: dict) -> set[str]:
    names: set[str] = set()
    for tool in req.get("tools") or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return names


async def _wait_until(predicate, *, timeout: float, interval: float = 0.5) -> bool:
    """轮询等一个条件成立（比固定 sleep 稳）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def run() -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"{PASS} {label}")
        else:
            failures += 1
            print(f"{FAIL} {label}  {detail}")

    # Worker 慢一点，才留得出"工单在跑"的窗口来验订阅直通
    env = AstrBotInstance(
        extra_env={
            "SUPERBOT_EVAL_MODE": "echo",
            "SUPERBOT_EVAL_DELAY": "0.05",
            "SUPERBOT_WORKER_DELAY": "3.0",
            "SUPERBOT_SANDBOX": "0",
        }
    )
    llm = FakeLLM()
    await llm.start()

    bot: FakeOneBot | None = None
    try:
        await env.start()
        check(
            await env.wait_log("[super-bot] initialized", timeout=20),
            "插件就绪",
            env.log_tail(20),
        )

        # --- ① 派活工具被标成后台任务 -----------------------------------
        check(
            "superbot_dispatch -> 后台任务模式" in env.log_text(),
            "① superbot_dispatch 已切换成后台任务",
            env.log_tail(25),
        )

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # --- ② + ⑤ 看主 agent 实际拿到的工具表和 system prompt ----------
        bot.reset()
        await bot.send_group("[[echo:probe]] 你好", user_id=20002, at=True)
        await bot.wait_for_text("ECHO:probe", timeout=30)

        req = llm.requests[-1] if llm.requests else {}
        tools = _tool_names(req)
        prompt = _system_prompt(req)

        check(
            "superbot_dispatch" in tools,
            "② 主 agent 有派活工具",
            f"实际 {sorted(tools)[:12]}",
        )
        check(
            "read_skill" in tools,
            "② 主 agent 有 read_skill（读技能不依赖沙箱）",
            f"实际 {sorted(tools)[:12]}",
        )
        check(
            "astrbot_execute_shell" not in tools
            and "astrbot_execute_python" not in tools,
            "② 主 agent 没有 shell / python（沙箱已关）",
            f"实际 {sorted(tools)[:12]}",
        )
        check(
            "enable Computer Use" not in prompt,
            "⑤ 框架那句「去开 Computer Use」被删掉了",
            prompt[-400:],
        )
        check(
            "[执行能力]" in prompt,
            "⑤ 换成了我们的执行说明",
            prompt[-400:],
        )
        check(
            "不要等待" in prompt or "不要等" in prompt,
            "① 提示词写明别等结果",
            prompt[-400:],
        )

        # --- ② /sb ls 与 /sb log ----------------------------------------
        bot.reset()
        await bot.send_group("/sb ls", user_id=20002)
        listing = await bot.wait_for_text("在跑：", timeout=20)
        check(listing is not None, "/sb ls 给出工单视图", env.log_tail(20))

        bot.reset()
        await bot.send_group("/sb log", user_id=20002)
        log_view = await bot.wait_for_text("工单", timeout=20)
        check(log_view is not None, "/sb log 能回话（没有工单也不报错）", env.log_tail(20))

        # --- ① 模型派活：先拿到 "Background task submitted"，结果随后回来 --
        bot.reset()
        await bot.send_group(
            "[[tool:superbot_dispatch]] 帮我算一下 6*7", user_id=20002, at=True
        )
        ack = await bot.wait_for_text("工具说：", timeout=60)
        check(ack is not None, "① 派活这一轮立刻有回应（没堵住对话）", env.log_tail(40))
        check(
            ack is not None and "Background task submitted" in ack,
            "① 这一轮拿到的是后台受理回执",
            str(ack),
        )

        # 异步的真凭据：结果**后来**被送回主 agent（框架的唤醒轮）。
        # 注意：唤醒轮里"要不要发给用户"由框架的提示词约束（它要求模型调用
        # send_message_to_user），假模型不会调工具，所以这里断言的是
        # "结果回到了主 agent"，不是"用户收到了最终消息"。
        got = await _wait_until(
            lambda: any("ECHO-BATCH" in _request_text(r) for r in llm.requests),
            timeout=60,
        )
        check(
            got,
            "① Worker 的结果随后被送回主 agent（异步投递）",
            f"共 {len(llm.requests)} 次模型请求",
        )
        check(
            any(
                "Background task submitted" in _request_text(r)
                for r in llm.requests
            ),
            "① 模型上下文里确实有过受理回执",
            f"共 {len(llm.requests)} 次模型请求",
        )

        # --- ④ 订阅直通：工单在跑时，同 Thread 的消息不再过 Router --------
        bot.reset()
        await bot.send_group("sb: 第一个任务", user_id=20003)
        # Worker 故意慢（3 秒），这里只等它开跑，别等它跑完
        await asyncio.sleep(1.0)

        evals_before = env.log_text().count("router eval")
        await bot.send_group("sb: 同一线程的第二条", user_id=20003)
        await asyncio.sleep(1.5)
        after_text = env.log_text()

        check(
            "+subscribed" in after_text,
            "④ 第二条被识别为已订阅（观测但短路决策）",
            env.log_tail(30),
        )
        check(
            after_text.count("router eval") == evals_before,
            f"④ 订阅期间没有再跑 Router 判断（前 {evals_before} 次）",
            env.log_tail(30),
        )

    except Exception:
        failures += 1
        print(f"{FAIL} 异常终止")
        traceback.print_exc()
        print("--- AstrBot 日志尾部 ---")
        print(env.log_tail(120))
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
