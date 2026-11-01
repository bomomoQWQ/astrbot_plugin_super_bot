"""确定性准入层（Gate）—— 四条规则，纯逻辑，不调模型。

设计约束（docs/设计/20-p0-spec.md §1）：
- **不 import astrbot**：入参是 :class:`EventView`，由 ``main.py`` 的适配层从
  ``AstrMessageEvent`` 抽取。这样才能在没有 AstrBot 的机器上单测。
- 不产生任何副作用（限流器除外，它只记时间戳）。

四条规则按优先级：

1. 自消息直接丢弃（最高优先，防回环）；
2. 指令放行（``/help`` 之类由核心与内置插件处理）；
3. 来源白名单（程序层，不调模型）；
4. 节流。

参考实现的分野：Gate 是纯函数（``admission.ts`` 约 1KB），Assessment 才是唯一
调模型的地方（约 20KB）。能写成代码的判断，绝不交给模型。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

__all__ = [
    "EventView",
    "GateConfig",
    "GateDecision",
    "RateLimiter",
    "Verdict",
    "decide",
]


class Verdict(str, Enum):
    """一条消息该往哪走。"""

    IGNORE = "ignore"
    """直接丢弃，不调模型，不回复。"""

    PASS = "pass"
    """放行给原生流程（指令、插件自有命令）。"""

    ROUTE = "route"
    """交给 Router（本插件的主动路径）。"""


@dataclass(frozen=True)
class EventView:
    """从 ``AstrMessageEvent`` 抽出的最小只读视图。

    字段与 AstrBot v4.28.1 的取值方法一一对应（括号内为源码位置）：

    - ``platform``      ← ``event.get_platform_name()``   (astr_message_event.py:126)
    - ``self_id``       ← ``event.get_self_id()``
    - ``sender_id``     ← ``event.get_sender_id()``
    - ``group_id``      ← ``event.get_group_id()``（私聊为空串）
    - ``session_id``    ← ``event.get_session_id()``      (astr_message_event.py:191)
    - ``message_str``   ← ``event.message_str``
    - ``umo``           ← ``event.unified_msg_origin``    (astr_message_event.py:106)
    - ``session_isolated`` ← ``event.get_extra("_session_isolated", False)``

    ⚠ ``umo`` **不能**作为 Thread 的基座：开启 ``unique_session`` 后
    ``session_id`` 会被改写成 ``{sender}_{group}``（waking_check/stage.py:88-92），
    指纹会按人分裂。一律用 ``group_id``。
    """

    platform: str
    self_id: str
    sender_id: str
    group_id: str
    session_id: str
    message_str: str
    umo: str
    session_isolated: bool = False
    woken: bool = False
    """是否被显式呼叫（对应 ``event.is_at_or_wake_command``）。

    与 ``is_wake`` 不同：``waking_check/stage.py`` 只在「@ 机器人 / 唤醒前缀 /
    私聊」时置 ``is_at_or_wake_command``（:128/:149/:158），而**任何**插件
    filter 通过都会置 ``is_wake``（:232）。前者才决定主 agent 跑不跑。
    """

    @property
    def chat_key(self) -> str:
        """会话标识：平台 + 群号（私聊为 ``private``）。"""
        return f"{self.platform}:{self.group_id or 'private'}"

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


@dataclass
class GateConfig:
    """Gate 的配置。全部可选，默认值即"不过滤"。"""

    bot_ids: frozenset[str] = frozenset()
    """额外要当作"自己"的身份（除 ``self_id`` 外）。合成事件的 sender 是 ``astrbot``。"""

    wake_prefixes: tuple[str, ...] = ("/",)
    """唤醒前缀。以任一前缀开头即视为指令，直接 ``PASS``。"""

    command_names: frozenset[str] = frozenset()
    """额外视为指令的首词集合（无前缀命令用）。"""

    allowed_sources: frozenset[str] = frozenset()
    """允许的来源 ``chat_key`` 集合。空集 = 全放行。"""

    rate_limit: int = 30
    rate_window_sec: float = 60.0


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: str

    def __bool__(self) -> bool:
        return self.verdict is Verdict.ROUTE


class RateLimiter:
    """每 ``chat_key`` 的滑动窗口计数（进程内存，重启即清零）。

    刻意不做持久化：限流是"当下保护"，历史没有意义，而且重启后放行一批
    消息比把旧计数带上来更安全。
    """

    def __init__(self, limit: int, window_sec: float) -> None:
        self._limit = max(1, int(limit))
        self._window = float(window_sec)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, now: float | None = None) -> bool:
        """记录一次访问。返回 ``True`` 表示允许，``False`` 表示应丢弃。"""
        ts = time.monotonic() if now is None else float(now)
        dq = self._hits[key]
        cutoff = ts - self._window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self._limit:
            return False
        dq.append(ts)
        return True

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


def _looks_like_command(message_str: str, conf: GateConfig) -> bool:
    """是否是应当放行的指令。

    AstrBot 的 ``CommandFilter`` 硬性要求 ``is_at_or_wake_command``
    （star/filter/command.py:191-193）；这里用前缀/首词做等价判断，避免依赖
    ``AstrMessageEvent`` 的内部状态。
    """
    text = message_str.lstrip()
    if not text:
        return False
    for prefix in conf.wake_prefixes:
        if prefix and text.startswith(prefix):
            return True
    if conf.command_names:
        head = text.split(maxsplit=1)[0]
        if head in conf.command_names:
            return True
    return False


def decide(
    view: EventView,
    conf: GateConfig | None = None,
    limiter: RateLimiter | None = None,
    now: float | None = None,
) -> GateDecision:
    """按四条规则给出判定。纯函数（除限流器内部计数）。"""
    conf = conf or GateConfig()

    # 规则 1：自消息 —— 最高优先，唯一的防回环防线。
    # 依据：ignore_bot_self_message 默认 False（waking_check/stage.py:64-67），
    # 而合成事件 sender_id 默认就是 "astrbot"（cron/events.py:23）。
    own_ids = set(conf.bot_ids)
    if view.self_id:
        own_ids.add(view.self_id)
    if view.sender_id and view.sender_id in own_ids:
        return GateDecision(Verdict.IGNORE, "self_message")

    # 规则 2：指令放行。
    if _looks_like_command(view.message_str, conf):
        return GateDecision(Verdict.PASS, "command")

    # 规则 2.5：显式呼叫先交给原生流程（**临时策略**）。
    #
    # 按最终设计（ADR-02），Router 应当拥有全部人类交互。但 P3 阶段 Router 只说
    # 确定性桩语，让它接管 @ 消息体验会很差。所以先放行给 AstrBot 自带的主 agent；
    # 等评估器接上模型（P5）后再改回 ROUTE。
    #
    # 注意这里的取舍：放行且不 yield，主 agent 正常回答——正是要的；反之若判成
    # ROUTE 却又不 yield，就会"Router 和主 agent 各回一次"。
    if view.woken:
        return GateDecision(Verdict.PASS, "explicit_call")

    # 规则 3：来源白名单。程序层，绝不调模型。
    if conf.allowed_sources and view.chat_key not in conf.allowed_sources:
        return GateDecision(Verdict.IGNORE, "source_not_allowed")

    # 规则 4：节流。
    if limiter is not None and not limiter.hit(view.chat_key, now):
        return GateDecision(Verdict.IGNORE, "rate_limited")

    return GateDecision(Verdict.ROUTE, "ok")
