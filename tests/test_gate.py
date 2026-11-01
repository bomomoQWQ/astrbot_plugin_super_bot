"""Gate（准入层）单测。

对应 docs/设计/20-p0-spec.md §4 的前五条与第八条。
全部纯逻辑，不需要 AstrBot 在场。
"""

from __future__ import annotations

from core.gate import EventView, GateConfig, RateLimiter, Verdict, decide


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
# 规则 1：自消息
# ---------------------------------------------------------------------------


def test_self_message_dropped_by_self_id():
    """sender 就是自己 → 丢弃。这是防回环的第一道防线。"""
    d = decide(_view(sender_id="10000"))
    assert d.verdict is Verdict.IGNORE
    assert d.reason == "self_message"


def test_cron_sender_dropped():
    """合成事件的 sender 默认是 "astrbot"（cron/events.py:23）→ 必须丢弃。

    ``ignore_bot_self_message`` 默认是 False（waking_check/stage.py:64-67），
    所以不能指望配置帮我们挡住，必须在 Gate 里显式处理。
    """
    conf = GateConfig(bot_ids=frozenset({"astrbot"}))
    d = decide(_view(sender_id="astrbot"), conf)
    assert d.verdict is Verdict.IGNORE
    assert d.reason == "self_message"


def test_other_bot_id_configured():
    conf = GateConfig(bot_ids=frozenset({"99999", "bot-x"}))
    assert decide(_view(sender_id="bot-x"), conf).verdict is Verdict.IGNORE


def test_empty_sender_not_treated_as_self():
    """sender 为空（异常事件）不应被误判成自己。"""
    assert decide(_view(sender_id="")).verdict is Verdict.ROUTE


# ---------------------------------------------------------------------------
# 规则 2：指令放行
# ---------------------------------------------------------------------------


def test_command_passthrough():
    d = decide(_view(message_str="/help"))
    assert d.verdict is Verdict.PASS
    assert d.reason == "command"


def test_command_with_leading_space():
    assert decide(_view(message_str="   /sid")).verdict is Verdict.PASS


def test_command_by_name_without_prefix():
    conf = GateConfig(command_names=frozenset({"help", "sid"}))
    assert decide(_view(message_str="help me"), conf).verdict is Verdict.PASS
    assert decide(_view(message_str="hello"), conf).verdict is Verdict.ROUTE


def test_slash_inside_text_is_not_command():
    """前缀只认开头，不能因为句中含 / 就放行。"""
    assert decide(_view(message_str="路径是 a/b/c 这个")).verdict is Verdict.ROUTE


def test_empty_message_not_command():
    assert decide(_view(message_str="")).verdict is Verdict.ROUTE


# ---------------------------------------------------------------------------
# 规则 3：来源白名单
# ---------------------------------------------------------------------------


def test_source_whitelist_blocks():
    conf = GateConfig(allowed_sources=frozenset({"aiocqhttp:999"}))
    d = decide(_view(), conf)
    assert d.verdict is Verdict.IGNORE
    assert d.reason == "source_not_allowed"


def test_source_whitelist_allows_member():
    conf = GateConfig(allowed_sources=frozenset({"aiocqhttp:123456"}))
    assert decide(_view(), conf).verdict is Verdict.ROUTE


def test_empty_whitelist_allows_everything():
    assert decide(_view(), GateConfig()).verdict is Verdict.ROUTE


def test_private_chat_chat_key():
    v = _view(group_id="", session_id="20001", umo="aiocqhttp:FriendMessage:20001")
    assert v.chat_key == "aiocqhttp:private"
    assert v.is_group is False


def test_group_chat_chat_key():
    assert _view().chat_key == "aiocqhttp:123456"
    assert _view().is_group is True


# ---------------------------------------------------------------------------
# 规则 4：节流
# ---------------------------------------------------------------------------


def test_rate_limit_drops_overflow():
    limiter = RateLimiter(limit=3, window_sec=60)
    conf = GateConfig(rate_limit=3, rate_window_sec=60)
    for i in range(3):
        assert decide(_view(), conf, limiter, now=float(i)).verdict is Verdict.ROUTE
    d = decide(_view(), conf, limiter, now=3.0)
    assert d.verdict is Verdict.IGNORE
    assert d.reason == "rate_limited"


def test_rate_limit_window_slides():
    limiter = RateLimiter(limit=3, window_sec=60)
    conf = GateConfig()
    for i in range(3):
        limiter.hit("aiocqhttp:123456", now=float(i))
    # 61 秒后窗口滑过，允许再次通过
    assert limiter.hit("aiocqhttp:123456", now=61.0) is True


def test_rate_limit_is_per_source():
    limiter = RateLimiter(limit=1, window_sec=60)
    conf = GateConfig()
    assert decide(_view(group_id="A"), conf, limiter, now=0.0).verdict is Verdict.ROUTE
    # 另一个群不受影响
    assert decide(_view(group_id="B"), conf, limiter, now=0.0).verdict is Verdict.ROUTE
    # 同一个群第二轮被限
    assert decide(_view(group_id="A"), conf, limiter, now=0.0).verdict is Verdict.IGNORE


def test_no_limiter_never_limits():
    conf = GateConfig(rate_limit=1)
    for _ in range(10):
        assert decide(_view(), conf, None).verdict is Verdict.ROUTE


def test_limiter_reset():
    limiter = RateLimiter(limit=1, window_sec=60)
    assert limiter.hit("k", now=0.0) is True
    assert limiter.hit("k", now=0.5) is False
    limiter.reset("k")
    assert limiter.hit("k", now=1.0) is True


# ---------------------------------------------------------------------------
# 优先级：自消息 > 指令
# ---------------------------------------------------------------------------


def test_self_message_beats_command():
    """自己发出去的 /stop 不能被当成用户指令放行。"""
    d = decide(_view(sender_id="10000", message_str="/stop"))
    assert d.verdict is Verdict.IGNORE
    assert d.reason == "self_message"


def test_command_beats_whitelist():
    """指令放行优先于白名单：否则在未配置的群里连 /help 都用不了。"""
    conf = GateConfig(allowed_sources=frozenset({"aiocqhttp:other"}))
    assert decide(_view(message_str="/help"), conf).verdict is Verdict.PASS


def test_decision_bool_matches_route():
    assert bool(decide(_view())) is True
    assert bool(decide(_view(sender_id="10000"))) is False
