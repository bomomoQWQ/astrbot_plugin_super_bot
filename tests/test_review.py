"""覆盖 ``superbot/review.py``：活动信号与审查者判定。

背景（ADR-14）：步数上限说不清"一次任务该多长"，改成看活动形态。
这里守住三件事：

1. 三种信号各自认得出来；
2. 判不出来时**不瞎报**（正常推进不能天天叫审查者）；
3. 解析审查者的回答要能扛住各种格式，认不出来时保守放行。
"""

from __future__ import annotations

from superbot.review import (
    Step,
    classify_activity,
    format_trace,
    parse_verdict,
    summarize_for_review,
)


def _steps(*specs, start=1000.0):
    return [
        Step(at=start + i * 10, name=name, ok=ok, ms=12, digest=digest)
        for i, (name, ok, digest) in enumerate(specs)
    ]


# -- classify_activity ------------------------------------------------------


def test_nothing_when_working_normally():
    trace = _steps(
        ("run_shell", True, "ls"),
        ("run_python", True, "print(1)"),
        ("run_shell", True, "cat a.txt"),
    )
    assert classify_activity(trace, now=1040.0) is None


def test_silence_when_no_recent_activity():
    trace = _steps(("run_shell", True, "ls"))
    # 最后一次调用在 1000，现在 1200 → 静默 200s
    assert classify_activity(trace, now=1200.0, silence_sec=180) == "silence"


def test_silence_uses_started_at_when_no_calls_yet():
    assert (
        classify_activity([], now=1300.0, started_at=1000.0, silence_sec=180)
        == "silence"
    )


def test_no_silence_without_anchor():
    assert classify_activity([], now=9999.0) is None


def test_repeat_when_same_command_three_times():
    trace = _steps(
        ("run_shell", True, "curl -s https://x"),
        ("run_shell", True, "curl -s https://x"),
        ("run_shell", True, "curl -s https://x"),
    )
    assert classify_activity(trace, now=1040.0) == "repeat"


def test_repeat_needs_same_digest():
    trace = _steps(
        ("run_shell", True, "curl a"),
        ("run_shell", True, "curl b"),
        ("run_shell", True, "curl c"),
    )
    assert classify_activity(trace, now=1040.0) is None


def test_repeat_ignores_empty_digest():
    """没有摘要时不能把"三个空字符串"判成重复。"""
    trace = _steps(
        ("run_shell", True, ""),
        ("run_shell", True, ""),
        ("run_shell", True, ""),
    )
    assert classify_activity(trace, now=1040.0) is None


def test_fail_streak_wins_over_others():
    trace = _steps(
        ("run_python", False, "boom"),
        ("run_python", False, "boom"),
        ("run_python", False, "boom"),
        ("run_python", False, "boom"),
        ("run_python", False, "boom"),
    )
    assert classify_activity(trace, now=1040.0, fail_streak=5) == "fail_streak"


def test_success_breaks_fail_streak():
    trace = _steps(
        ("run_python", False, "a"),
        ("run_python", False, "b"),
        ("run_python", True, "c"),
        ("run_python", False, "d"),
    )
    assert classify_activity(trace, now=1040.0, fail_streak=3) is None


# -- format_trace -----------------------------------------------------------


def test_format_trace_basic():
    trace = _steps(("run_shell", True, "ls -la"))
    text = format_trace(trace)
    assert "run_shell" in text
    assert "ok" in text
    assert "ls -la" in text


def test_format_trace_empty():
    assert "还没有" in format_trace([])


def test_format_trace_truncates_middle():
    trace = [
        Step(at=1000 + i, name="run_shell", ok=True, ms=1, digest=f"cmd{i}")
        for i in range(40)
    ]
    text = format_trace(trace, max_items=10)
    assert "省略" in text
    # 头尾都在，中间被省略
    assert "cmd0" in text
    assert "cmd39" in text


def test_format_trace_respects_char_limit():
    trace = [
        Step(at=1000 + i, name="run_shell", ok=True, ms=1, digest="x" * 200)
        for i in range(30)
    ]
    text = format_trace(trace, limit=300)
    assert len(text) < 600
    assert "截断" in text


# -- parse_verdict ----------------------------------------------------------


def test_parse_plain_words():
    assert parse_verdict("continue")[0] == "continue"
    assert parse_verdict("replan 用 pip 装")[0] == "replan"
    assert parse_verdict("stop 它在死循环")[0] == "stop"


def test_parse_case_and_punctuation():
    assert parse_verdict("STOP：没救了")[0] == "stop"
    assert parse_verdict("Replan: 换接口")[0] == "replan"
    assert parse_verdict("**stop**")[0] == "stop"


def test_parse_keeps_reason():
    verdict, reason = parse_verdict("replan 换成 requests 重试一次")
    assert verdict == "replan"
    assert "requests" in reason


def test_parse_empty_defaults_to_continue():
    """认不出来时保守放行——宁可多跑一会儿，也别误杀正常任务。"""
    assert parse_verdict("")[0] == "continue"
    assert parse_verdict("   ")[0] == "continue"


def test_parse_verbose_answer():
    text = "看起来它一直在重复同一条命令，所以 stop 吧"
    assert parse_verdict(text)[0] == "stop"


def test_parse_unknown_text_falls_back_to_continue():
    assert parse_verdict("我不知道该说什么")[0] == "continue"


# -- summarize_for_review ---------------------------------------------------


def test_summary_contains_essentials():
    trace = _steps(("run_shell", True, "ls"))
    brief = summarize_for_review(
        "算 1+1", trace, elapsed=42.0, signal="silence", last_text="还没结论"
    )
    assert brief["task"] == "算 1+1"
    assert brief["signal"] == "silence"
    assert brief["steps"] == "1"
    assert "run_shell" in brief["trace"]
    assert "42" in brief["elapsed"]
