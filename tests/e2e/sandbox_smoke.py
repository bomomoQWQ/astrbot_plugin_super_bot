"""沙箱冒烟：直接打 Bay API，验证「部署成功 + 能力可用」。

跑法::

    python -m tests.e2e.sandbox_smoke

前置：`.runtime/shipyard-neo/deploy/docker` 下已 ``docker compose up -d``，
且 API key 写在 ``.runtime/bay_key.txt``（或环境变量 ``BAY_API_KEY``）。

它验证的正是插件将来要用的那条链路：
**创建沙箱 → 执行 Python → 写文件 → 读文件 → 销毁**。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp

RUNTIME_DIR = Path(__file__).resolve().parents[2] / ".runtime"
KEY_FILE = RUNTIME_DIR / "bay_key.txt"
BASE = os.environ.get("BAY_ENDPOINT", "http://localhost:8114")

PASS = "[PASS]"
FAIL = "[FAIL]"


def _api_key() -> str:
    """读 API key。

    ⚠ PowerShell 5.1 的 ``Set-Content -Encoding utf8`` 会写 **BOM**，
    带着 BOM 的 key 会让 Bay 返回 401（BOM 被当成 key 的一部分）。
    所以这里统一 strip 掉 BOM 与空白。
    """
    env = os.environ.get("BAY_API_KEY")
    if env:
        return env.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8-sig").strip()
    raise RuntimeError(f"找不到 API key：既没有 BAY_API_KEY，也没有 {KEY_FILE}")


def _hdr(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def run() -> int:
    key = _api_key()
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"{PASS} {label}")
        else:
            failures += 1
            print(f"{FAIL} {label}  {detail}")

    async with aiohttp.ClientSession() as s:
        # --- 0. 健康 ------------------------------------------------------
        async with s.get(f"{BASE}/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
            body = await r.json()
        check(r.status == 200 and body.get("status") == "ok", f"Bay 健康（v{body.get('version')}）", str(body))

        # --- 1. 创建沙箱 --------------------------------------------------
        sandbox_id = None
        async with s.post(
            f"{BASE}/v1/sandboxes",
            headers=_hdr(key),
            json={"profile": "python-default", "ttl": 600},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as r:
            body = await r.json()
        if r.status in (200, 201):
            sandbox_id = body.get("id")
        check(sandbox_id is not None, "创建沙箱", f"HTTP {r.status} {body}")
        if not sandbox_id:
            return failures

        print(f"      sandbox_id = {sandbox_id}  status={body.get('status')}  caps={body.get('capabilities')}")

        try:
            # --- 2. 执行 Python（首次会自动拉起容器会话） ------------------
            async with s.post(
                f"{BASE}/v1/sandboxes/{sandbox_id}/python/exec",
                headers=_hdr(key),
                json={"code": "import sys, platform\nprint('PY', 6*7)\nprint(sys.version.split()[0])"},
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                body = await r.json()
            out = (body.get("output") or "")
            check(
                r.status == 200 and body.get("success") and "PY 42" in out,
                "执行 Python（首次调用含容器冷启动）",
                f"HTTP {r.status} {str(body)[:300]}",
            )
            check("execution_id" in body, "返回了 execution_id（执行证据）", str(body)[:200])

            # --- 3. 写文件（工作区固定 /workspace） ------------------------
            async with s.put(
                f"{BASE}/v1/sandboxes/{sandbox_id}/filesystem/files",
                headers=_hdr(key),
                json={"path": "hello.txt", "content": "hello from astrbot-plugin"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                wbody = await r.text()
            check(r.status in (200, 201), "写入工作区文件", f"HTTP {r.status} {wbody[:200]}")

            # --- 4. 读回来 -------------------------------------------------
            async with s.get(
                f"{BASE}/v1/sandboxes/{sandbox_id}/filesystem/files",
                headers=_hdr(key),
                params={"path": "hello.txt"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                rbody = await r.json()
            content = rbody.get("content") if isinstance(rbody, dict) else None
            check(
                content == "hello from astrbot-plugin",
                "读回文件内容一致",
                f"HTTP {r.status} {str(rbody)[:200]}",
            )

            # --- 5. Shell -------------------------------------------------
            async with s.post(
                f"{BASE}/v1/sandboxes/{sandbox_id}/shell/exec",
                headers=_hdr(key),
                json={"command": "uname -s && pwd"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                body = await r.json()
            check(
                r.status == 200 and body.get("success"),
                "执行 Shell 命令",
                f"HTTP {r.status} {str(body)[:200]}",
            )
            print(f"      shell out = {(body.get('output') or '').strip()!r}")

        finally:
            # --- 6. 销毁 ---------------------------------------------------
            async with s.delete(
                f"{BASE}/v1/sandboxes/{sandbox_id}",
                headers=_hdr(key),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                await r.text()
            check(r.status in (200, 204), "销毁沙箱（不留资源）", f"HTTP {r.status}")

    print()
    print("=" * 60)
    print("全部通过" if failures == 0 else f"失败 {failures} 项")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
