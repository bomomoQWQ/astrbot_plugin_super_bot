"""覆盖 ``superbot/prompt.py``：把框架那句"去开 Computer Use"真删掉。

这条以前是个真 bug：我们只是**追加**了一句反驳，于是模型同时看到
"你不能执行技能，让用户去开设置"和"你不需要 shell，派活就行"，
信哪句全看运气。现在改成按行删除，这里守住这个行为。
"""

from __future__ import annotations

from superbot.prompt import (
    COMPUTER_USE_MARKER,
    EXEC_HINT,
    SKILL_HINT,
    strip_computer_use_notice,
)

FRAMEWORK_LINE = (
    "User has not enabled the Computer Use feature. "
    "You cannot use shell or Python to perform skills. "
    "If you need to use these capabilities, ask the user to enable "
    "Computer Use in the AstrBot WebUI -> Config."
)


def test_removes_the_framework_sentence():
    prompt = f"你是助手。\n{FRAMEWORK_LINE}\n# 技能\n- pdf\n"
    out, removed = strip_computer_use_notice(prompt)

    assert removed is True
    assert "enable Computer Use" not in out
    assert COMPUTER_USE_MARKER not in out
    # 前后内容都得留着
    assert "你是助手。" in out
    assert "# 技能" in out
    assert "- pdf" in out


def test_noop_when_absent():
    prompt = "你是助手。\n# 技能\n- pdf\n"
    out, removed = strip_computer_use_notice(prompt)

    assert removed is False
    assert out == prompt


def test_handles_empty_prompt():
    assert strip_computer_use_notice("") == ("", False)


def test_only_one_occurrence_left_alone():
    """整段删掉后不该把后面的正常句子一起吞掉。"""
    prompt = f"{FRAMEWORK_LINE}\n这句话必须留下。\n"
    out, removed = strip_computer_use_notice(prompt)

    assert removed is True
    assert "这句话必须留下。" in out


def test_idempotent():
    prompt = f"开头\n{FRAMEWORK_LINE}\n结尾"
    once, _ = strip_computer_use_notice(prompt)
    twice, removed_again = strip_computer_use_notice(once)

    assert twice == once
    assert removed_again is False


def test_exec_hint_says_do_not_wait_and_no_shell():
    """提示词必须写明两件事，否则模型会等、会劝用户开设置。"""
    assert "不要等待" in EXEC_HINT
    assert "superbot_dispatch" in EXEC_HINT
    assert "read_skill" in EXEC_HINT
    assert "Computer Use" in EXEC_HINT  # 明确叫它别去建议那个开关
    assert "没有" in EXEC_HINT


def test_exec_hint_warns_shell_tools_are_gone():
    """线上实测：模型还是伸手去调 astrbot_execute_shell，白费一轮。

    提示词得把"这些工具已经不存在"说死，别只说"你不必用"。
    """
    assert "astrbot_execute_shell" in EXEC_HINT
    assert "不存在" in EXEC_HINT


def test_skill_hint_points_at_read_skill():
    assert "read_skill" in SKILL_HINT
    assert "不用开沙箱" in SKILL_HINT


def test_wake_hint_demands_send_message_to_user():
    """唤醒轮只有 send_message_to_user 一个投递通道，必须说死。

    框架自带的措辞是 "Use ... if needed"（软），模型要是只输出文字，
    用户那边就是静默——这条守住"必须"。
    """
    from superbot.prompt import WAKE_HINT, WAKE_MARKER

    assert WAKE_MARKER.startswith("# BACKGROUND TASK")
    assert "send_message_to_user" in WAKE_HINT
    assert "必须" in WAKE_HINT
    assert "看不到" in WAKE_HINT
