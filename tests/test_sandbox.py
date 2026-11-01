"""沙箱端口单测。

两层：

1. **MockTransport**：不联网，验证请求路径 / 头 / body 是否正确
   —— 这是接外部 API 最容易写错的地方。
2. **live**：Bay 真的在跑时才执行（``pytest -m live``），验证端到端能力。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from superbot.sandbox import BaySandbox, ExecResult, SandboxError, keepalive_loop

KEY = "sk-bay-test"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# MockTransport：协议形状
# ---------------------------------------------------------------------------


async def test_create_sandbox_default_has_no_idempotency_key():
    """默认不带幂等键 —— Bay 的幂等缓存比沙箱活得久，复用会拿到已销毁的 id。"""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("authorization")
        seen["idem"] = req.headers.get("idempotency-key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "sandbox-abc", "status": "idle"})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY, ttl=600)
    sb._client = _client(handler)
    sid = await sb.create(ticket_id="tk-1")
    await sb._client.aclose()

    assert sid == "sandbox-abc"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/sandboxes"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["idem"] is None, "默认不该带幂等键"
    assert seen["body"] == {"profile": "python-default", "ttl": 600}


async def test_create_sandbox_can_opt_into_idempotency():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["idem"] = req.headers.get("idempotency-key")
        return httpx.Response(201, json={"id": "s1"})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY, ttl=600)
    sb._client = _client(handler)
    await sb.create(ticket_id="tk-1", idempotent=True)
    await sb._client.aclose()
    assert seen["idem"] == "ticket-tk-1"


async def test_create_without_ttl_omits_field():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "s1"})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY, ttl=None)
    sb._client = _client(handler)
    await sb.create(ticket_id="t")
    await sb._client.aclose()
    assert "ttl" not in seen["body"]


async def test_python_exec_parses_result():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/sandboxes/s1/python/exec"
        body = json.loads(req.content)
        assert body["code"] == "print(1)"
        return httpx.Response(
            200,
            json={
                "success": True,
                "output": "1\n",
                "error": None,
                "execution_id": "exe_1",
                "execution_time_ms": 12,
            },
        )

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    res = await sb.exec_python("s1", "print(1)")
    await sb._client.aclose()
    assert isinstance(res, ExecResult)
    assert res.ok and res.output == "1\n" and res.execution_id == "exe_1"
    assert res.elapsed_ms == 12


async def test_exec_failure_returns_result_not_raise():
    """执行失败（沙箱里代码报错）不应该抛异常，要返回 ExecResult。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "ZeroDivisionError"})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    res = await sb.exec_python("s1", "1/0")
    await sb._client.aclose()
    assert res.failed and "ZeroDivisionError" in (res.error or "")


async def test_http_error_raises_sandbox_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    with pytest.raises(SandboxError) as ei:
        await sb.create(ticket_id="t")
    await sb._client.aclose()
    assert "401" in str(ei.value) and "Invalid API key" in str(ei.value)


async def test_filesystem_roundtrip_paths():
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "PUT":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"content": "hi"})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    await sb.write_file("s1", "a.txt", "hi")
    got = await sb.read_file("s1", "a.txt")
    await sb._client.aclose()

    assert got == "hi"
    assert calls == [
        ("PUT", "/v1/sandboxes/s1/filesystem/files"),
        ("GET", "/v1/sandboxes/s1/filesystem/files"),
    ]


async def test_destroy_tolerates_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    await sb.destroy("gone")  # 不应抛
    await sb._client.aclose()


async def test_keepalive_path():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    sb = BaySandbox(endpoint="http://bay:8114", api_key=KEY)
    sb._client = _client(handler)
    await sb.keepalive("s1")
    await sb._client.aclose()
    assert seen == [("POST", "/v1/sandboxes/s1/keepalive")]


async def test_keepalive_loop_survives_single_failure():
    """一个沙箱保活失败不能拖垮整个循环。"""
    calls: list[str] = []

    class FakePort:
        async def keepalive(self, sid: str) -> None:
            calls.append(sid)
            if sid == "bad":
                raise RuntimeError("boom")

    stop = asyncio.Event()
    task = asyncio.create_task(
        keepalive_loop(FakePort(), lambda: ["good", "bad", "good2"], interval=0.05, stop=stop)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.12)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert "good" in calls and "good2" in calls and "bad" in calls


# ---------------------------------------------------------------------------
# live：Bay 真在跑时才验
# ---------------------------------------------------------------------------


def _bay_key() -> str | None:
    if k := os.environ.get("BAY_API_KEY"):
        return k.strip()
    # tests/test_sandbox.py -> parents[0]=tests, parents[1]=仓库根
    f = Path(__file__).resolve().parents[1] / ".runtime" / "bay_key.txt"
    return f.read_text(encoding="utf-8-sig").strip() if f.exists() else None


async def _bay_alive() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get("http://localhost:8114/health")
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.live
async def test_live_sandbox_full_cycle():
    key = _bay_key()
    if not key or not await _bay_alive():
        pytest.skip("Bay 未运行或没有 API key")

    sb = BaySandbox(endpoint="http://localhost:8114", api_key=key, ttl=120)
    sid = await sb.create(ticket_id="live-test-1")
    try:
        res = await sb.exec_python(sid, "print(6*7)")
        assert res.ok and "42" in res.output
        assert res.execution_id

        await sb.write_file(sid, "x.txt", "ok")
        assert await sb.read_file(sid, "x.txt") == "ok"

        sh = await sb.exec_shell(sid, "pwd")
        assert sh.ok and "/workspace" in sh.output
    finally:
        await sb.destroy(sid)


@pytest.mark.live
async def test_live_sandbox_isolation_between_sandboxes():
    """两个沙箱互不可见——这正是「一个 Ticket 一个沙箱」要的隔离。"""
    key = _bay_key()
    if not key or not await _bay_alive():
        pytest.skip("Bay 未运行或没有 API key")

    sb = BaySandbox(endpoint="http://localhost:8114", api_key=key, ttl=120)
    a = await sb.create(ticket_id="iso-a")
    b = await sb.create(ticket_id="iso-b")
    try:
        assert a != b
        await sb.write_file(a, "secret.txt", "from-a")
        # B 里读不到 A 的文件（路径不存在 → 抛错或空内容）
        missed = False
        try:
            content = await sb.read_file(b, "secret.txt")
            missed = content == ""
        except SandboxError:
            missed = True
        assert missed, "两个沙箱居然共享了工作区"
    finally:
        await sb.destroy(a)
        await sb.destroy(b)
