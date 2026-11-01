"""沙箱工具集单测。

直接调 handler，不经过模型——验证的是「工具真的打到沙箱上了」，
以及输出截断、失败信息这些边界。

需要 astrbot 才能构造 ``FunctionTool``，没有就跳过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _ensure_astrbot() -> bool:
    env = os.environ.get("ASTRBOT_SRC")
    cands = [Path(env)] if env else []
    cands += [REPO.parent / "AstrBot", REPO / "AstrBot", Path.home() / "AstrBot"]
    for c in cands:
        if (c / "astrbot").is_dir():
            sys.path.insert(0, str(c))
            return True
    return False


if not _ensure_astrbot():
    pytestmark = pytest.mark.skip(reason="没有 AstrBot 源码，无法构造 FunctionTool")
else:
    from superbot.toolset import build_sandbox_tools


class FakeSandbox:
    """记录调用并返回可控结果的假沙箱。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.python_result = ("ok", "42\n", None)
        self.shell_result = ("ok", "Linux\n", None)
        self.files: dict[str, str] = {}

    async def exec_python(self, sid, code, timeout=30):
        self.calls.append(("python", code))
        ok, out, err = self.python_result
        from superbot.sandbox import ExecResult

        return ExecResult(ok=(ok == "ok"), output=out, error=err, execution_id="exe_1")

    async def exec_shell(self, sid, cmd, timeout=60):
        self.calls.append(("shell", cmd))
        ok, out, err = self.shell_result
        from superbot.sandbox import ExecResult

        return ExecResult(ok=(ok == "ok"), output=out, error=err)

    async def write_file(self, sid, path, content):
        self.calls.append(("write", (path, content)))
        self.files[path] = content

    async def read_file(self, sid, path):
        self.calls.append(("read", path))
        if path not in self.files:
            raise RuntimeError(f"没找到 {path}")
        return self.files[path]


def _tool(tools, name):
    for t in tools.tools:
        if t.name == name:
            return t
    raise AssertionError(f"没有工具 {name}，实际有 {[t.name for t in tools.tools]}")


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


async def test_exposes_four_tools():
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    assert {t.name for t in tools.tools} == {
        "run_python",
        "run_shell",
        "write_file",
        "read_file",
    }


async def test_every_tool_has_valid_json_schema():
    """parameters 要能过 JSON Schema 元校验（构造时就校过了，这里再确认必填项）。"""
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    for t in tools.tools:
        assert t.parameters["type"] == "object"
        assert t.parameters["required"], f"{t.name} 没有必填参数"
        assert all(
            p in t.parameters["properties"] for p in t.parameters["required"]
        ), f"{t.name} 的 required 和 properties 不一致"


# ---------------------------------------------------------------------------
# 真的打到沙箱
# ---------------------------------------------------------------------------


async def test_run_python_hits_sandbox():
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "run_python").handler(None, code="print(6*7)")
    assert sb.calls == [("python", "print(6*7)")]
    assert out.strip() == "42"


async def test_run_shell_hits_sandbox():
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "run_shell").handler(None, command="uname -s")
    assert sb.calls == [("shell", "uname -s")]
    assert "Linux" in out


async def test_write_then_read():
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    w = await _tool(tools, "write_file").handler(None, path="a.txt", content="hi")
    assert "已写入" in w and sb.files["a.txt"] == "hi"
    r = await _tool(tools, "read_file").handler(None, path="a.txt")
    assert r == "hi"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


async def test_output_is_truncated():
    sb = FakeSandbox()
    sb.python_result = ("ok", "x" * 10_000, None)
    tools = build_sandbox_tools(sb, "sb-1", max_output=100)
    out = await _tool(tools, "run_python").handler(None, code="print('x')")
    assert len(out) < 300
    assert "已截断" in out


async def test_failure_is_reported_to_model():
    sb = FakeSandbox()
    sb.python_result = ("err", "", "ZeroDivisionError: division by zero")
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "run_python").handler(None, code="1/0")
    assert "执行失败" in out and "ZeroDivisionError" in out


async def test_failure_with_partial_output():
    sb = FakeSandbox()
    sb.python_result = ("err", "打印了一半\n", "KeyError: 'x'")
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "run_python").handler(None, code="...")
    assert "打印了一半" in out and "KeyError" in out


async def test_empty_success_output():
    sb = FakeSandbox()
    sb.python_result = ("ok", "", None)
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "run_python").handler(None, code="x = 1")
    assert "无输出" in out


async def test_read_missing_file_is_reported():
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1")
    out = await _tool(tools, "read_file").handler(None, path="nope.txt")
    assert "读取失败" in out


async def test_on_call_callback_reports():
    seen: list[tuple[str, bool]] = []
    sb = FakeSandbox()
    tools = build_sandbox_tools(sb, "sb-1", on_call=lambda n, ok, ms: seen.append((n, ok)))
    await _tool(tools, "run_python").handler(None, code="print(1)")
    sb.python_result = ("err", "", "boom")
    await _tool(tools, "run_python").handler(None, code="1/0")
    assert seen == [("run_python", True), ("run_python", False)]
