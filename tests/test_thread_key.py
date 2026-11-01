"""Thread 指纹与幂等键单测。

对应 docs/设计/20-p0-spec.md §4 的第六、七条。
最关键的断言是 ``test_same_group_same_key_regardless_of_unique_session``：
指纹**不能**被 ``unique_session`` 配置改变，否则任务会按人分裂。
"""

from __future__ import annotations

from core.gate import EventView
from core.thread_key import (
    explicit_tag,
    local_date,
    stable_key,
    thread_key,
    ticket_id_from_eval,
    trigger_id,
)


def _view(**kw) -> EventView:
    base = dict(
        platform="aiocqhttp",
        self_id="10000",
        sender_id="20001",
        group_id="123456",
        session_id="123456",
        message_str="今天天气不错",
        umo="aiocqhttp:GroupMessage:123456",
    )
    base.update(kw)
    return EventView(**base)


# ---------------------------------------------------------------------------
# 显式标签提取
# ---------------------------------------------------------------------------


def test_explicit_tag_hash_number():
    assert explicit_tag("#123 这个问题怎么解") == "123"


def test_explicit_tag_strips_leading_zeros():
    """#007 与 #7 必须视为同一件事。"""
    assert explicit_tag("#007") == explicit_tag("#7") == "7"


def test_explicit_tag_pr_style():
    assert explicit_tag("PR #45 已合并") == "45"
    assert explicit_tag("pr#45") == "45"


def test_explicit_tag_issue_style():
    assert explicit_tag("issue 7 还没修") == "7"
    assert explicit_tag("issue #8") == "8"


def test_explicit_tag_bracket_code():
    assert explicit_tag("看下 [ABC-12] 这个") == "ABC-12"
    assert explicit_tag("[T-2026]") == "T-2026"


def test_explicit_tag_none_when_absent():
    assert explicit_tag("今天天气不错") is None
    assert explicit_tag("") is None
    assert explicit_tag("2026 年了") is None  # 裸数字不算


def test_explicit_tag_first_mention_wins():
    assert explicit_tag("#1 和 #2 都看一下") == "1"


# ---------------------------------------------------------------------------
# thread_key 优先级
# ---------------------------------------------------------------------------


def test_tag_beats_reply():
    v = _view(message_str="#123 看下这个")
    assert thread_key(v, reply_ref="r-999") == "aiocqhttp:123456|tag:123"


def test_reply_beats_topic():
    v = _view()
    assert thread_key(v, reply_ref="r-1", topic="t-1") == "aiocqhttp:123456|reply:r-1"


def test_topic_beats_day():
    assert thread_key(_view(), topic="t-9") == "aiocqhttp:123456|topic:t-9"


def test_day_fallback():
    assert thread_key(_view(), today="2026-11-01") == "aiocqhttp:123456|day:2026-11-01"


def test_private_chat_key():
    v = _view(group_id="", session_id="20001")
    assert thread_key(v, today="2026-11-01") == "aiocqhttp:private|day:2026-11-01"


def test_different_groups_different_key():
    a = thread_key(_view(group_id="A"), today="2026-11-01")
    b = thread_key(_view(group_id="B"), today="2026-11-01")
    assert a != b


def test_different_platforms_different_key():
    a = thread_key(_view(platform="aiocqhttp"), today="2026-11-01")
    b = thread_key(_view(platform="telegram"), today="2026-11-01")
    assert a != b


# ---------------------------------------------------------------------------
# 关键不变量：unique_session 不能影响指纹
# ---------------------------------------------------------------------------


def test_same_group_same_key_regardless_of_unique_session():
    """开启 unique_session 后 session_id 变成 ``{sender}_{group}``，umo 随之改变。

    指纹必须仍然按群聚合，否则同一个人在群里的每次发言都会被当成新任务。
    """
    # unique_session = False
    v_off = _view(
        session_id="123456",
        umo="aiocqhttp:GroupMessage:123456",
        session_isolated=False,
    )
    # unique_session = True：session_id 被改写成 sender_group
    v_on = _view(
        session_id="20001_123456",
        umo="aiocqhttp:GroupMessage:20001_123456",
        session_isolated=True,
    )
    assert thread_key(v_off, today="2026-11-01") == thread_key(v_on, today="2026-11-01")


def test_two_senders_same_group_share_key():
    """同群不同人说的、都不带标签的消息 → 同一个 day-bucket 指纹。"""
    a = _view(sender_id="20001")
    b = _view(sender_id="20002")
    assert thread_key(a, today="2026-11-01") == thread_key(b, today="2026-11-01")


def test_umo_never_used_as_base():
    """即使 umo 完全不同，只要平台与群号一致，指纹就一致。"""
    a = _view(umo="aiocqhttp:GroupMessage:zzz")
    b = _view(umo="totally:different:umo")
    assert thread_key(a, today="d") == thread_key(b, today="d")


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------


def test_trigger_id_idempotent():
    a = trigger_id("aiocqhttp", "aiocqhttp:123456|tag:7", "msg-abc")
    b = trigger_id("aiocqhttp", "aiocqhttp:123456|tag:7", "msg-abc")
    assert a == b
    assert len(a) == 32


def test_trigger_id_changes_with_msg_id():
    a = trigger_id("aiocqhttp", "T", "m1")
    b = trigger_id("aiocqhttp", "T", "m2")
    assert a != b


def test_trigger_id_changes_with_thread():
    a = trigger_id("aiocqhttp", "T1", "m")
    b = trigger_id("aiocqhttp", "T2", "m")
    assert a != b


def test_trigger_id_type_prefix_prevents_collision():
    """前缀确保 "1" 与 1 之类的歧义输入不会撞键。"""
    a = stable_key("trigger", "1", "2")
    b = stable_key("trigger", 1, 2)
    assert a != b


def test_ticket_id_from_eval_stable():
    a = ticket_id_from_eval("eval-xyz")
    b = ticket_id_from_eval("eval-xyz")
    assert a == b and len(a) == 32


def test_ticket_and_trigger_namespaces_differ():
    """同一串内容，前缀不同 → 不同键，避免跨类型撞。"""
    assert stable_key("ticket", "x") != stable_key("trigger", "x")


def test_stable_key_order_matters():
    assert stable_key("a", "b") != stable_key("b", "a")


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------


def test_local_date_is_iso():
    d = local_date()
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"
