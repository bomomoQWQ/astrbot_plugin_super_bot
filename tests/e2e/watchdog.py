"""端到端：**watchdog 真的在盯任务**（卡住 → 重试 → 升级 → 判死）。

跑法::

    python -m tests.e2e.watchdog

做法：把 Worker 的桩耗时调到 30 秒（等于故意卡住），把停滞阈值压到 1 秒、
退避压到 0.5 秒，于是几十秒内就能完整走一遍：

    卡住 → 判停滞 → 重试（取消旧的、提交新的）→ 升级 → 判死 → 通知用户

这是"卡住不会自愈"那条缺口的真实验证。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import traceback

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance, RUNTIME_DIR

PASS = "[PASS]"
FAIL = "[FAIL]"
DB = RUNTIME_DIR / "data" / "plugin_data" / "astrbot_plugin_super_bot" / "state.db"


def _ticket_states() -> dict[str, str]:
    """直接读库看工单状态（跨进程验证，比读日志硬）。"""
    if not DB.exists():
        return {}
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "SELECT t.id, "
            " (SELECT f.kind FROM ticket_fact f WHERE f.ticket_id = t.id "
            "  AND f.kind IN ('human_stopped','delivered','blocked') "
            "  ORDER BY f.id LIMIT 1) AS terminal "
            "FROM ticket t"
        ).fetchall()
        return {r[0]: (r[1] or "running") for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


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
            "SUPERBOT_WORKER_DELAY": "30",   # 故意卡住
            "SUPERBOT_STALL_SEC": "1",
            "SUPERBOT_WATCHDOG_TICK_SEC": "0.2",
            "SUPERBOT_RETRY_BACKOFF_SEC": "0.5,0.5,0.5",
        }
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        check(
            await env.wait_log("[super-bot] initialized", timeout=20),
            "插件就绪",
            env.log_tail(25),
        )
        check(
            await env.wait_log("watchdog started", timeout=10),
            "watchdog 循环已启动（以前这个模块根本没接）",
            env.log_tail(25),
        )

        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()

        # 起一个会卡住的工单
        await bot.send_group("sb: 会卡住的任务", user_id=20002)
        check(
            await env.wait_log("dispatched", timeout=20),
            "工单已派出（随后会被卡住）",
            env.log_tail(30),
        )

        # 卡住 → 判停滞
        check(
            await env.wait_log("watchdog stalled", timeout=15),
            "watchdog 发现任务停滞了",
            env.log_tail(40),
        )
        # 重试
        check(
            await env.wait_log("watchdog retry", timeout=15),
            "watchdog 触发了重试",
            env.log_tail(40),
        )
        check(
            await env.wait_log("retried ticket", timeout=15),
            "真的重新提交了工单（取消旧的 + 起新的）",
            env.log_tail(40),
        )
        check(
            await env.wait_log("outcome=cancelled", timeout=15),
            "卡住的那次被执行被取消掉了",
            env.log_tail(40),
        )
        # 升级
        check(
            await env.wait_log("watchdog escalate", timeout=20),
            "重试用尽后升级",
            env.log_tail(50),
        )
        # 判死
        check(
            await env.wait_log("watchdog failed", timeout=25),
            "升级也到顶后判死",
            env.log_tail(50),
        )
        check(
            await env.wait_log("stalled-to-death", timeout=10),
            "库里记了 blocked(stalled-to-death)",
            env.log_tail(30),
        )

        # 用户被通知了
        texts = bot.sent_texts()
        check(
            any("卡住" in t or "放弃" in t for t in texts),
            f"用户收到了说明（实际 {texts}）",
            str(texts),
        )

        # 跨进程读库确认状态
        await asyncio.sleep(1.0)
        states = _ticket_states()
        check(
            any(v == "blocked" for v in states.values()),
            f"工单在库里是 blocked（实际 {states}）",
            str(states),
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
