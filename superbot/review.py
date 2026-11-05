"""活动信号与审查者判定（ADR-14：不再数步数，改看"还在不在干活"）。

这里只放**纯函数**：怎么从轨迹里看出"卡住了"、怎么把轨迹写成给模型看的
简报、怎么解析模型给出的判定。真正调模型的部分在 ``main.py``——本模块
不依赖 astrbot，可以单独测。

背景：步数上限是个坏约束。它既说不清"这个任务本来就该走 40 步"，
又挡不住"两个命令之间来回横跳 300 次"。真正该盯的是**活动形态**：

=====================================  ==================================
信号                                   含义
=====================================  ==================================
``silence``                            很久没有任何工具调用，进程像死了
``repeat``                             同一条命令连着跑了好几次，原地打转
``fail_streak``                        连着失败，越试越糟
=====================================  ==================================

三者命中任一 → 叫审查者（一次便宜的模型调用）看轨迹，再由它决定
**继续 / 换思路重派 / 停下**。这样"慢但在推进"的活不会被误杀。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .limits import (
    ACTIVITY_FAIL_STREAK,
    ACTIVITY_REPEAT_THRESHOLD,
    ACTIVITY_SILENCE_SEC,
    REVIEW_MAX_CHARS,
)

__all__ = [
    "Step",
    "classify_activity",
    "format_trace",
    "parse_verdict",
    "VERDICTS",
]


@dataclass(frozen=True)
class Step:
    """一次工具调用的记录。"""

    at: float
    """调用发生的时刻（``time.time()``）。"""

    name: str
    """工具名，如 ``run_shell``。"""

    ok: bool
    ms: int
    digest: str = ""
    """参数摘要。用来判"是不是同一条命令反复跑"。"""


VERDICTS = ("continue", "replan", "stop")


def classify_activity(
    trace: Sequence[Step],
    now: float,
    *,
    started_at: float | None = None,
    silence_sec: float = ACTIVITY_SILENCE_SEC,
    repeat_threshold: int = ACTIVITY_REPEAT_THRESHOLD,
    fail_streak: int = ACTIVITY_FAIL_STREAK,
) -> str | None:
    """看轨迹判断"这次像是卡住了吗"，返回信号名或 ``None``。

    判定顺序按"证据强度"排：连续失败最硬（一定有东西不对），
    重复次之（模型在试同一招），静默最后（也可能是正常的长命令）。

    Args:
        trace: 按时间顺序的调用记录（最新在最后）。
        now: 当前时刻。
        started_at: 任务开始时刻。给了就在"还没有任何调用"时用它算静默。
        silence_sec: 多久没动静算静默。
        repeat_threshold: 同一摘要连续出现几次算打转。
        fail_streak: 连续失败几次算越试越糟。
    """
    if trace:
        streak = 0
        for step in reversed(trace):
            if step.ok:
                break
            streak += 1
        if streak >= fail_streak:
            return "fail_streak"

    if len(trace) >= repeat_threshold:
        tail = [s.digest for s in trace[-repeat_threshold:]]
        if tail[0] and len(set(tail)) == 1:
            return "repeat"

    anchor = trace[-1].at if trace else started_at
    if anchor is not None and (now - anchor) >= silence_sec:
        return "silence"

    return None


def format_trace(
    trace: Iterable[Step],
    *,
    limit: int = REVIEW_MAX_CHARS,
    max_items: int = 60,
) -> str:
    """把轨迹写成给人/模型看的简报。

    只保留头尾：审查者要判断"有没有在推进"，看开头怎么起的、最后卡在哪
    就够了，中间几十条成功的调用没有信息量。
    """
    items = list(trace)
    if not items:
        return "（还没有任何工具调用）"

    lines = [
        f"{i + 1}. [{s.name}] {'ok' if s.ok else 'FAIL'} {s.ms}ms  {s.digest}"
        for i, s in enumerate(items)
    ]
    if len(lines) > max_items:
        head = lines[: max_items // 2]
        tail = lines[-(max_items // 2) :]
        lines = head + [f"...（省略 {len(lines) - max_items} 条）..."] + tail

    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n...（轨迹过长，已截断）"
    return text


def parse_verdict(text: str) -> tuple[str, str]:
    """解析审查者的回答。

    容错优先：模型可能回 ``STOP``、``stop：原因``、``"replan 换用 pip 装"``、
    甚至先讲一段理由再给结论。这里只看开头那个词，其余当理由。

    Returns:
        ``(verdict, reason)``，verdict 必为 ``VERDICTS`` 之一；
        实在认不出来时保守地当 ``continue``（宁可让它继续跑，也不要
        因为解析失败误杀一个正常任务）。
    """
    raw = (text or "").strip()
    if not raw:
        return "continue", ""

    head = raw.lstrip("　 \t\r\n\"'`*#-").lower()
    for word in VERDICTS:
        if head.startswith(word):
            rest = raw.lstrip("　 \t\r\n\"'`*#-")[len(word) :]
            reason = rest.lstrip("：: ,，-—\t").strip()
            if reason.startswith(("（", "(")) and "）" in reason:
                reason = reason[reason.index("）") + 1 :].strip()
            return word, reason

    # 没认出来：看看整段里有没有明确动词，再退到保守默认
    lowered = raw.lower()
    for word in ("stop", "replan", "continue"):
        if word in lowered:
            return word, raw
    return "continue", raw


def summarize_for_review(
    task: str,
    trace: Sequence[Step],
    *,
    elapsed: float,
    signal: str,
    last_text: str = "",
) -> Mapping[str, str]:
    """组装喂给审查者的材料（保持一个 dict，方便测试与日志）。"""
    return {
        "task": (task or "").strip()[:800],
        "signal": signal,
        "elapsed": f"{elapsed:.0f}s",
        "steps": str(len(trace)),
        "trace": format_trace(trace),
        "last_text": (last_text or "").strip()[:600],
    }
