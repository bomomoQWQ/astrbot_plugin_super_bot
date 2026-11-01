"""Thread 指纹与幂等键 —— 全是纯函数。

设计约束（docs/设计/20-p0-spec.md §2）：
- **不 import astrbot**；
- 只读 :class:`~core.gate.EventView` 与适配层抽出的两个可选线索
  （``reply_ref`` / ``topic``）；
- 同一件事在任何时候算出同一个 key。

为什么要单独一个模块：Thread 指纹是本项目**唯一需要经验**的设计，
也是最容易静默出错的地方（算错了会让任务串味，比不做直通更糟）。
放在这里可以反复调、反复测，不影响其他部分。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone, timedelta

from .gate import EventView

__all__ = [
    "explicit_tag",
    "stable_key",
    "thread_key",
    "ticket_id_from_eval",
    "trigger_id",
]

#: 显式标签：``#123`` / ``PR #45`` / ``issue 7`` / ``[T-2026]`` / ``[ABC-12]``
#: 刻意不匹配裸数字，避免把 "2026"、"300 人" 之类误判成事编号。
_TAG_RE = re.compile(
    r"(?:#|\bPR\s*#?|\bissue\s*#?)(?P<num>\d{1,6})\b"
    r"|\[(?P<code>[A-Z]{1,6}-\d{2,})\]",
    re.IGNORECASE,
)

#: 本地时区（中国标准时间，无夏令时）。Thread 的日粒度兜底用它。
_LOCAL_TZ = timezone(timedelta(hours=8))


def explicit_tag(message_str: str) -> str | None:
    """从消息里提取显式的事编号；没有则返回 ``None``。

    取**第一个**命中：一条消息里出现多个编号时，通常第一个才是话题主体。
    """
    if not message_str:
        return None
    m = _TAG_RE.search(message_str)
    if not m:
        return None
    num = m.group("num")
    if num:
        return str(int(num))  # 去掉前导零，"#007" 与 "#7" 视为同一个
    code = m.group("code")
    return code.upper().lstrip("[").rstrip("]")


def thread_key(
    view: EventView,
    *,
    reply_ref: str | None = None,
    topic: str | None = None,
    today: str | None = None,
) -> str:
    """算出这条消息所属的 Thread key。

    优先级（从强到弱）：显式标签 → 回复链 → 平台话题 → 日粒度兜底。

    Args:
        view: 事件视图。基座用 ``view.chat_key``（平台 + 群号），
            **不用** ``umo``——它会被 ``unique_session`` 改写。
        reply_ref: 被回复消息的稳定引用（由适配层从 Reply 消息段提取）。
        topic: 平台话题 id（如 Telegram 的 forum topic）。
        today: 覆盖日期，便于测试。

    Returns:
        形如 ``aiocqhttp:123456|tag:42`` 的字符串。
    """
    base = view.chat_key
    if tag := explicit_tag(view.message_str):
        return f"{base}|tag:{tag}"
    if reply_ref:
        return f"{base}|reply:{reply_ref}"
    if topic:
        return f"{base}|topic:{topic}"
    return f"{base}|day:{today or local_date()}"


def local_date(now: datetime | None = None) -> str:
    """本地日期字符串 ``YYYY-MM-DD``。"""
    dt = now or datetime.now(_LOCAL_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOCAL_TZ)
    return dt.astimezone(_LOCAL_TZ).date().isoformat()


def stable_key(*parts: object) -> str:
    """由内容派生的稳定短键（32 位十六进制）。

    照参考实现的 ``key(*parts) = sha256(json)[:32]``：父 id 参与子 id 计算，
    于是「一个评估只产生一个工单」是**结构性保证**，不是靠状态判断。

    ⚠ 参与计算的字段必须**稳定**：不能用时间戳、不能用自增序号，
    否则同一件事每次都会得到新 key，幂等立刻失效。
    """
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def trigger_id(source: str, thread_key_value: str, platform_msg_id: str) -> str:
    """Trigger 的幂等键。同一平台消息无论投递几次，结果相同。"""
    return stable_key("trigger", source, thread_key_value, platform_msg_id)


def ticket_id_from_eval(eval_id: str) -> str:
    """由 eval 派生的工单 id。同一 eval 重跑多次，工单 id 相同。"""
    return stable_key("ticket", eval_id)
