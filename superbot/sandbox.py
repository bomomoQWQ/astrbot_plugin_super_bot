"""沙箱执行环境：``SandboxPort`` 抽象 + Shipyard Neo（Bay）实现。

为什么要有抽象：文章 5.2 的"计算与执行环境分离"要求执行环境可换——
本地调试用进程内执行，上线用隔离容器。抽象住这一层，Worker 就不关心底下是什么。

为什么用 Bay 的 HTTP API 而不是 AstrBot 的 ``get_booter()``：
`get_booter` 的隔离粒度是 **UMO** 而不是 Ticket，参数只能读全局配置、无空闲回收，
还会污染共享的 ``session_booter``（详见 ``docs/调查/05-sandbox.md``）。
自己直连 Bay 才能做到「一个 Ticket 一个沙箱」。

两条必须自己做的事（AstrBot 全仓库从不调用它们）：

1. **保活**：沙箱 TTL 到期后 managed Cargo 会被级联删除 → 工作区没了。
   ``keepalive_loop`` 就是干这个的。
2. **销毁**：``task.cancel()`` 只停了客户端的 await，容器还在跑并占配额。

本模块只依赖 ``httpx``，不 import astrbot。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

import httpx

__all__ = [
    "BaySandbox",
    "ExecResult",
    "SandboxError",
    "SandboxPort",
    "keepalive_loop",
]


class SandboxError(RuntimeError):
    """沙箱操作失败（网络、鉴权、业务错误统一成这个）。"""


@dataclass(frozen=True)
class ExecResult:
    """一次执行的结果。"""

    ok: bool
    output: str = ""
    error: str | None = None
    execution_id: str | None = None
    elapsed_ms: int = 0

    @property
    def failed(self) -> bool:
        return not self.ok


@runtime_checkable
class SandboxPort(Protocol):
    """执行环境抽象。Worker 只依赖这个。"""

    async def create(self, *, ticket_id: str, ttl: int | None = None) -> str: ...

    async def exec_python(self, sandbox_id: str, code: str, timeout: int = 30) -> ExecResult: ...

    async def exec_shell(self, sandbox_id: str, command: str, timeout: int = 60) -> ExecResult: ...

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None: ...

    async def read_file(self, sandbox_id: str, path: str) -> str: ...

    async def keepalive(self, sandbox_id: str) -> None: ...

    async def destroy(self, sandbox_id: str) -> None: ...


@dataclass
class BaySandbox:
    """Shipyard Neo（Bay）的 HTTP 客户端。

    Args:
        endpoint: 例如 ``http://localhost:8114``。
        api_key: ``sk-bay-...``。
        profile: 沙箱 Profile（决定能力集合与资源限额）。
        ttl: 默认生存时间（秒）。``None`` 表示不过期。
        timeout: 单次 HTTP 请求超时。
        client: 可注入的 ``httpx.AsyncClient``（测试用 MockTransport）。
    """

    endpoint: str
    api_key: str
    profile: str = "python-default"
    ttl: int | None = 3600
    timeout: float = 120.0
    _client: httpx.AsyncClient | None = None

    # -- 基础设施 ---------------------------------------------------------

    def _base(self) -> str:
        return self.endpoint.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base()}{path}"
        client = self._client
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, verify=False)
            own_client = True
        try:
            resp = await client.request(
                method, url, headers=self._headers(), json=json, params=params
            )
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"{method} {path} 网络失败: {exc}") from exc
        finally:
            if own_client:
                await client.aclose()

        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                detail = str(resp.json().get("error", {}).get("message") or detail)
            except Exception:  # noqa: BLE001
                pass
            raise SandboxError(f"{method} {path} -> {resp.status_code}: {detail}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"raw": resp.text}

    # -- Sandbox 生命周期 -------------------------------------------------

    async def create(
        self, *, ticket_id: str, ttl: int | None = None, idempotent: bool = False
    ) -> str:
        """创建一个沙箱，返回 sandbox_id。

        Args:
            ticket_id: 用于幂等键（当 ``idempotent=True``）与日志。
            ttl: 覆盖默认 TTL。
            idempotent: 是否带 ``Idempotency-Key``。

        ⚠ ``idempotent`` 默认 **False**，这是踩出来的：

        Bay 的幂等缓存**比沙箱活得久**。用固定的 ticket_id 当幂等键，
        同一条工单第二次开沙箱时会拿到缓存里那个**已经被销毁**的 sandbox_id，
        随后的执行就是 404 ``Sandbox not found``。

        所以这里只在"同一次派工内的重试"场景才开幂等（由调用方传 True），
        默认每次都建新的。
        """
        body: dict[str, Any] = {"profile": self.profile}
        use_ttl = self.ttl if ttl is None else ttl
        if use_ttl:
            body["ttl"] = int(use_ttl)

        url = f"{self._base()}/v1/sandboxes"
        client = self._client
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, verify=False)
            own_client = True
        try:
            headers = self._headers()
            if idempotent:
                headers["Idempotency-Key"] = f"ticket-{ticket_id}"
            try:
                resp = await client.post(url, headers=headers, json=body)
            except Exception as exc:  # noqa: BLE001
                raise SandboxError(f"创建沙箱网络失败: {exc}") from exc
        finally:
            if own_client:
                await client.aclose()

        if resp.status_code >= 400:
            raise SandboxError(f"创建沙箱失败 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        sandbox_id = data.get("id")
        if not sandbox_id:
            raise SandboxError(f"创建沙箱未返回 id: {data}")
        return str(sandbox_id)

    async def keepalive(self, sandbox_id: str) -> None:
        await self._request("POST", f"/v1/sandboxes/{sandbox_id}/keepalive")

    async def extend_ttl(self, sandbox_id: str, ttl: int) -> None:
        await self._request(
            "POST", f"/v1/sandboxes/{sandbox_id}/extend_ttl", json={"ttl": int(ttl)}
        )

    async def destroy(self, sandbox_id: str) -> None:
        """销毁沙箱。**取消任务时必须调它**，否则容器继续占配额。"""
        try:
            await self._request("DELETE", f"/v1/sandboxes/{sandbox_id}")
        except SandboxError as exc:
            # 已经不存在也算销毁成功
            if "404" not in str(exc) and "not_found" not in str(exc):
                raise

    # -- 能力 -------------------------------------------------------------

    async def exec_python(
        self, sandbox_id: str, code: str, timeout: int = 30
    ) -> ExecResult:
        started = time.perf_counter()
        try:
            body = await self._request(
                "POST",
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": code, "timeout": int(timeout)},
            )
        except SandboxError as exc:
            return ExecResult(
                ok=False,
                error=str(exc),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return ExecResult(
            ok=bool(body.get("success")),
            output=body.get("output") or "",
            error=body.get("error"),
            execution_id=body.get("execution_id"),
            elapsed_ms=int(body.get("execution_time_ms") or (time.perf_counter() - started) * 1000),
        )

    async def exec_shell(
        self, sandbox_id: str, command: str, timeout: int = 60
    ) -> ExecResult:
        started = time.perf_counter()
        try:
            body = await self._request(
                "POST",
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": command, "timeout": int(timeout)},
            )
        except SandboxError as exc:
            return ExecResult(
                ok=False,
                error=str(exc),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return ExecResult(
            ok=bool(body.get("success")),
            output=body.get("output") or "",
            error=body.get("error"),
            execution_id=body.get("execution_id"),
            elapsed_ms=int(body.get("execution_time_ms") or (time.perf_counter() - started) * 1000),
        )

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        await self._request(
            "PUT",
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            json={"path": path, "content": content},
        )

    async def read_file(self, sandbox_id: str, path: str) -> str:
        body = await self._request(
            "GET",
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            params={"path": path},
        )
        return str(body.get("content") or "")


async def keepalive_loop(
    sandbox: SandboxPort,
    ids_provider: Any,
    *,
    interval: float = 1200.0,
    stop: asyncio.Event | None = None,
    on_result: Callable[[int, int], None] | None = None,
) -> None:
    """周期性对所有活跃沙箱保活。

    AstrBot 从不调用 keepalive/extend_ttl，而 Bay 的默认 TTL 是 3600s，
    到期后 managed Cargo 会被级联删除——**工作区直接没了**。所以必须自己续。

    Args:
        sandbox: 沙箱端口。
        ids_provider: 无参可调用对象或普通序列，返回当前活跃的 sandbox id 列表。
        interval: 保活间隔（默认 20 分钟，比 1 小时 TTL 安全）。
        stop: 停止信号。
        on_result: 每轮结束回调 ``(成功数, 失败数)``。同步调用，方便调用方打日志
            ——这样本模块仍然不依赖任何 logger。
    """
    while True:
        if stop is not None and stop.is_set():
            return
        try:
            ids = ids_provider() if callable(ids_provider) else list(ids_provider)
        except Exception:  # noqa: BLE001
            ids = []

        ok = fail = 0
        for sid in ids:
            try:
                await sandbox.keepalive(sid)
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
        if ids and on_result is not None:
            try:
                on_result(ok, fail)
            except Exception:  # noqa: BLE001
                pass

        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval)
