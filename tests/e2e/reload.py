"""端到端：走 Dashboard 触发**插件重载**，验证 ``terminate()`` 真的跑了。

跑法::

    python -m tests.e2e.reload

为什么需要它：Windows 上 ``Popen.terminate()`` 是 ``TerminateProcess``（硬杀），
插件生命周期回调根本不会触发，所以"关掉进程"这种方式验不到 ``terminate()``。
而框架的插件重载（``POST /api/plugin/reload``）会**先 terminate 再 initialize**，
这是唯一能在真实例里跑到的路径。

断言：
1. 第一次 initialized 出现
2. 登录 Dashboard 拿到 token
3. 调 reload
4. 日志里出现 terminated
5. 日志里出现**第二次** initialized（说明重载成功）
6. 重载后插件仍然工作（发一条消息还能路由）
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import traceback

import aiohttp

from tests.e2e.fake_onebot import FakeOneBot
from tests.e2e.harness import AstrBotInstance

PASS = "[PASS]"
FAIL = "[FAIL]"
DASH = "http://127.0.0.1:6185"
PLUGIN_ID = "astrbot_plugin_super_bot"


async def _login(s: aiohttp.ClientSession, password: str) -> tuple[str | None, str]:
    """用给定的口令登录 Dashboard。

    口令从哪来：AstrBot 首次启动会把**随机生成的强口令**打进启动日志
    （``➜  Initial password: xxxx``）。所以这里先从日志里把它捞出来。

    Returns:
        ``(token, 诊断信息)``。
    """
    diag: list[str] = []
    try:
        async with s.post(
            f"{DASH}/api/auth/login",
            json={"username": "astrbot", "password": password},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            status = r.status
            raw = await r.text()
    except Exception as exc:  # noqa: BLE001
        return None, f"请求失败 {exc}"

    diag.append(f"HTTP {status} {raw[:160]}")
    if status != 200:
        return None, "; ".join(diag)
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, "; ".join(diag)

    # 框架的约定是 HTTP 200 + body.status，token 可能在 data 或顶层
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    token = (
        data.get("token")
        or data.get("access_token")
        or body.get("token")
        or body.get("access_token")
    )
    return (str(token) if token else None), "; ".join(diag)


async def _wait_initial_password(env: AstrBotInstance, timeout: float = 30) -> str | None:
    """从启动日志里捞随机初始口令。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        m = re.search(r"Initial password:\s*(\S+)", env.log_text())
        if m:
            return m.group(1)
        await asyncio.sleep(0.4)
    return None


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
        extra_env={"SUPERBOT_EVAL_MODE": "echo", "SUPERBOT_EVAL_DELAY": "0.1"}
    )
    bot: FakeOneBot | None = None
    try:
        await env.start()
        check(await env.wait_log("[super-bot] initialized", timeout=20), "第一次初始化完成", env.log_tail(20))

        password = await _wait_initial_password(env)
        check(password is not None, "从启动日志里拿到 Dashboard 随机初始口令", env.log_tail(25))
        if password is None:
            return failures

        async with aiohttp.ClientSession() as s:
            token, diag = await _login(s, password)
            check(token is not None, "登录 Dashboard", diag)
            if token is None:
                print(f"      诊断：{diag}")
                return failures

            headers = {"Authorization": f"Bearer {token}"}
            before = env.log_text().count("[super-bot] initialized")

            async with s.post(
                f"{DASH}/api/plugin/reload",
                headers=headers,
                json={"name": PLUGIN_ID},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                body = await r.text()
            check(r.status < 400, f"调用插件重载（HTTP {r.status}）", body[:300])

            # 等 terminated + 第二次 initialized
            ok_term = await env.wait_log("[super-bot] terminated", timeout=30)
            check(ok_term, "插件 terminate() 真的执行了", env.log_tail(40))

            deadline = asyncio.get_running_loop().time() + 40
            after = before
            while asyncio.get_running_loop().time() < deadline:
                after = env.log_text().count("[super-bot] initialized")
                if after > before:
                    break
                await asyncio.sleep(0.5)
            check(after > before, f"重载后重新初始化（{before} -> {after}）", env.log_tail(40))

        # 重载后还能干活
        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()
        await bot.send_group("sb: 重载之后", user_id=20002)
        text = await bot.wait_for_text("ECHO-BATCH:", timeout=30)
        check(text is not None, "重载后插件仍能正常工作", env.log_tail(30))

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
