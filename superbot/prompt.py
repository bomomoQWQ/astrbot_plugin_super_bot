"""主 agent 的执行能力提示词（覆盖框架自带那段）。

为什么要有这个模块：``computer_use_runtime=none`` 时框架会往 system prompt
里追加一段

    User has not enabled the Computer Use feature.
    You cannot use shell or Python to perform skills.
    If you need to use these capabilities, ask the user to enable
    Computer Use in the AstrBot WebUI -> Config.
    （astr_main_agent.py:593-598）

而我们是**刻意**关掉主 agent 的沙箱的（ADR-16：执行一律派给 Worker）。
那句提示会让模型反过来劝用户去改设置，跟我们的设计直接冲突。

**追加一句压不住它**——两句话同时存在时模型信哪句没准。所以这里的做法是
把那段**整行删掉**再补上我们自己的说明。

钩子 ``on_llm_request`` 在 ``_decorate_llm_request`` 之后触发、拿到的是同一个
可变的 ``ProviderRequest``，所以替换有效。上游改文案是必然的，因此
``strip_computer_use_notice`` 返回 ``removed`` 标志，调用方据此告警。
"""

from __future__ import annotations

__all__ = [
    "COMPUTER_USE_MARKER",
    "EXEC_HINT",
    "SKILL_HINT",
    "strip_computer_use_notice",
]

COMPUTER_USE_MARKER = "User has not enabled the Computer Use feature."
"""框架那段话的开头。删除时以它为锚点。"""

_END_ANCHOR = "Computer Use"
"""结束锚点：那段话最后落在 ``...enable Computer Use in the AstrBot WebUI``。"""

EXEC_HINT = (
    "\n[执行能力]\n"
    "你**没有** shell，也不需要。你自己不用跑任何命令：\n"
    "- 读技能正文 → `read_skill`（直接返回文本，零开销）\n"
    "- 读技能目录里的脚本 → `read_skill(name, file=...)`（能读，不能跑）\n"
    "- 算数/跑代码/看环境/下载/处理文件 → `superbot_dispatch` 派给沙箱子代理\n"
    "派出去之后**立刻继续跟用户说话**，不要等待、不要轮询、不要说"
    "\"我看看\"\"稍等\"。结果会自己回来。\n"
    "不要建议用户去打开 Computer Use——那是别的插件的功能，这里不需要。\n"
)

SKILL_HINT = (
    "\n[技能读取]\n"
    "需要看某个技能的 SKILL.md 时，**优先调用 `read_skill` 工具**"
    "（直接返回正文，不用开沙箱，也不要有 shell）。\n"
)


def strip_computer_use_notice(prompt: str) -> tuple[str, bool]:
    """删掉框架塞的那段"去开 Computer Use"。

    按**整行**删除：从标记所在行的行首，切到结束锚点所在行的行尾。
    找不到结束锚点时退化为只删标记那一行（不整段吞掉后面的正常内容）。

    Returns:
        ``(新 prompt, 是否删掉了东西)``。匹配不到时原样返回、``False``。
    """
    if not prompt or COMPUTER_USE_MARKER not in prompt:
        return prompt, False

    out = prompt
    for _ in range(4):  # 防御性上限：最多切四段
        start = out.find(COMPUTER_USE_MARKER)
        if start == -1:
            break
        end = -1
        idx = out.find(_END_ANCHOR, start + len(COMPUTER_USE_MARKER))
        if idx != -1:
            end = idx + len(_END_ANCHOR)
        else:
            end = start + len(COMPUTER_USE_MARKER)

        line_start = out.rfind("\n", 0, start) + 1
        line_end = out.find("\n", end)
        if line_end == -1:
            line_end = len(out)
        out = out[:line_start] + out[line_end:]

    return out, out != prompt
