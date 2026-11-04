"""沙箱工具集：把 ``SandboxPort`` 包成模型能调用的 ``FunctionTool``。

这是"骨架"和"能用"的分界线：之前 Worker 在沙箱里只跑一句写死的 ``print(6*7)``
（演示链路通不通），现在模型可以真的**在沙箱里写代码、看结果、再调整**。

依赖 astrbot 的 ``FunctionTool`` / ``ToolSet``（公开 API，见
``astrbot/api/__init__.py:5,76-77``），所以本模块和 ``synthetic.py`` 一样
是 ``superbot/`` 里少数依赖框架的文件。``main.py`` 里对它是**可选导入**：
加载失败就退回桩执行，不影响插件启动。

调用约定（``astr_agent_tool_exec.py:666-672``）：设了 ``handler`` 的工具，
执行器会以 ``handler(event, *args, **kwargs)`` 调它——第一个位置参数是事件，
后面是模型给出的参数。所以每个 handler 的第一个参数都是 ``event``（我们不用）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from astrbot.api import FunctionTool, ToolSet

from .sandbox import ExecResult, SandboxPort

__all__ = ["build_sandbox_tools", "DEFAULT_MAX_OUTPUT"]

DEFAULT_MAX_OUTPUT = 4000
"""工具输出上限（字符）。模型看不到的东西没必要塞进上下文，也省钱。"""


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def _fmt(res: ExecResult, limit: int) -> str:
    """把执行结果转成模型能读的一段文本。"""
    body = _clip((res.output or "").strip(), limit)
    if res.ok:
        return body or "（执行成功，无输出）"
    err = _clip((res.error or "未知错误"), limit)
    return f"执行失败：{err}" + (f"\n已有输出：{body}" if body else "")


def _shell_with_proxy(command: str, proxy: str) -> str:
    """在命令前面加上代理环境变量。

    沙箱本身能上网，但目标站点可能在用户那台机器上被拦掉（实测：宿主机 hosts
    把 github 指到 127.0.0.1，且直连真实 IP 也通不了）。配了 ``proxy`` 就统一
    从代理出口走，省得模型自己在命令里手写 export。
    """
    q = proxy.replace('"', "")
    return (
        f'export http_proxy="{q}" https_proxy="{q}" all_proxy="{q}" '
        f'HTTP_PROXY="{q}" HTTPS_PROXY="{q}"; {command}'
    )


def _python_with_proxy(code: str, proxy: str) -> str:
    """给要执行的 Python 代码前面塞一段设置环境变量的话。"""
    q = proxy.replace('"', "")
    head = (
        "import os as _os\n"
        f'_os.environ.setdefault("http_proxy", "{q}")\n'
        f'_os.environ.setdefault("https_proxy", "{q}")\n'
        f'_os.environ.setdefault("all_proxy", "{q}")\n'
        f'_os.environ.setdefault("HTTP_PROXY", "{q}")\n'
        f'_os.environ.setdefault("HTTPS_PROXY", "{q}")\n'
    )
    return head + code


def build_sandbox_tools(
    sandbox: SandboxPort,
    sandbox_id: str,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    on_call: Callable[[str, bool, int], None] | None = None,
    skill_reader: Callable[[str, str | None], str] | None = None,
    proxy: str | None = None,
) -> ToolSet:
    """给某个沙箱造一组工具。

    Args:
        sandbox: 沙箱端口。
        sandbox_id: 这个工单独占的沙箱。
        max_output: 单次输出上限。
        on_call: ``(工具名, 是否成功, 耗时ms)`` 的回调，方便调用方打日志
            ——本模块不依赖任何 logger。
        skill_reader: ``(技能名, 文件名或None) -> 正文``。给了就多挂一把
            ``read_skill``：技能文件在**宿主**上，不必传进沙箱也能读。
        proxy: 形如 ``http://192.168.1.254:5078``。给了就让每条 shell / python
            都带上 ``http_proxy`` 之类的环境变量，从代理出口走。
    """

    def _report(name: str, ok: bool, started: float) -> None:
        if on_call is not None:
            try:
                on_call(name, ok, int((time.perf_counter() - started) * 1000))
            except Exception:  # noqa: BLE001
                pass

    async def run_python(event: Any, code: str) -> str:
        """在沙箱里执行 Python 代码。"""
        t0 = time.perf_counter()
        payload = _python_with_proxy(code, proxy) if proxy else code
        res = await sandbox.exec_python(sandbox_id, payload, timeout=60)
        _report("run_python", res.ok, t0)
        return _fmt(res, max_output)

    async def run_shell(event: Any, command: str) -> str:
        """在沙箱里执行 shell 命令。"""
        t0 = time.perf_counter()
        payload = _shell_with_proxy(command, proxy) if proxy else command
        res = await sandbox.exec_shell(sandbox_id, payload, timeout=60)
        _report("run_shell", res.ok, t0)
        return _fmt(res, max_output)

    async def write_file(event: Any, path: str, content: str) -> str:
        """往沙箱工作区写文件。"""
        t0 = time.perf_counter()
        try:
            await sandbox.write_file(sandbox_id, path, content)
            ok = True
            msg = f"已写入 {path}（{len(content)} 字符）"
        except Exception as exc:  # noqa: BLE001
            ok = False
            msg = f"写入失败：{exc}"
        _report("write_file", ok, t0)
        return msg

    async def read_file(event: Any, path: str) -> str:
        """读沙箱工作区里的文件。"""
        t0 = time.perf_counter()
        try:
            content = await sandbox.read_file(sandbox_id, path)
            ok = True
            msg = _clip(content, max_output) or "（文件为空）"
        except Exception as exc:  # noqa: BLE001
            ok = False
            msg = f"读取失败：{exc}"
        _report("read_file", ok, t0)
        return msg

    tools = ToolSet()
    tools.add_tool(
        FunctionTool(
            name="run_python",
            description=(
                "在隔离沙箱里执行 Python 代码并返回输出。"
                "需要计算、处理数据、试跑代码时用它——不要凭脑子算。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"}
                },
                "required": ["code"],
            },
            handler=run_python,
        )
    )
    tools.add_tool(
        FunctionTool(
            name="run_shell",
            description="在隔离沙箱里执行 shell 命令（Linux）。查看环境、文件、进程时用。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"}
                },
                "required": ["command"],
            },
            handler=run_shell,
        )
    )
    tools.add_tool(
        FunctionTool(
            name="write_file",
            description="把内容写入沙箱工作区里的文件（相对路径，根目录是 /workspace）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 out.txt"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
    tools.add_tool(
        FunctionTool(
            name="read_file",
            description="读取沙箱工作区里的文件内容。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 out.txt"}
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    if skill_reader is not None:

        async def read_skill(event: Any, name: str, file: str | None = None) -> str:
            """读技能的完整说明。"""
            t0 = time.perf_counter()
            try:
                text = skill_reader(name, file)
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                text = f"读技能失败：{exc}"
            _report("read_skill", ok, t0)
            return _clip(text, max_output) or "（技能是空的）"

        tools.add_tool(
            FunctionTool(
                name="read_skill",
                description=(
                    "读某个技能的完整说明（SKILL.md），可以直接取技能目录里的其它文件。"
                    "技能文件在宿主上，读它不需要动沙箱。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "技能名"},
                        "file": {
                            "type": "string",
                            "description": "技能目录里的相对路径（可选，默认读 SKILL.md）",
                        },
                    },
                    "required": ["name"],
                },
                handler=read_skill,
            )
        )
    return tools
