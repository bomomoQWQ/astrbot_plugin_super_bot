"""合成事件：给"没有真实消息"的任务造一个 ``AstrMessageEvent``。

这是 ``superbot/`` 包里**唯一依赖 astrbot** 的模块（其余模块保持纯逻辑、可脱框
架单测）。它照抄 ``astrbot/core/cron/events.py`` 的 ``CronMessageEvent``
（67 行，v4.28.1），只改了名字。

为什么需要它：``AstrMessageEvent`` 是本框架里 Agent 的**身份载体**——
``Context.tool_loop_agent(event=...)`` 要它、``ActiveEventRegistry`` 也拿它当
key。而定时器 / 队列 / Webhook 发起的任务没有真实消息，必须自己造一个。

相比直接 import，照抄的好处是：不依赖 ``astrbot.core.cron`` 这条私有路径，
上游改了也不会连带我们炸。代价是约 60 行重复代码，可接受。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

__all__ = ["SyntheticEvent", "make_synthetic_event"]


class SyntheticEvent(AstrMessageEvent):
    """模拟一条来自某个会话的消息事件（不经过平台适配器）。"""

    def __init__(
        self,
        *,
        context: Any,
        session: MessageSession,
        message: str,
        sender_id: str = "astrbot",
        sender_name: str = "SuperBot",
        extras: dict[str, Any] | None = None,
        message_type: MessageType = MessageType.FRIEND_MESSAGE,
    ) -> None:
        platform_meta = PlatformMetadata(
            name="superbot",
            description="Synthetic",
            id=session.platform_id,
        )

        msg_obj = AstrBotMessage()
        msg_obj.type = message_type
        msg_obj.self_id = sender_id
        msg_obj.session_id = session.session_id
        msg_obj.message_id = uuid.uuid4().hex
        msg_obj.sender = MessageMember(user_id=session.session_id, nickname=sender_name)
        msg_obj.message = [Plain(message)]
        msg_obj.message_str = message
        msg_obj.raw_message = message
        msg_obj.timestamp = int(time.time())

        super().__init__(message, msg_obj, platform_meta, session.session_id)

        # 回复地址用原始会话
        self.session = session
        self.context_obj = context
        self.is_at_or_wake_command = True
        self.is_wake = True

        if extras:
            self._extras.update(extras)

    async def send(self, message: MessageChain) -> None:
        if message is None:
            return
        await self.context_obj.send_message(self.session, message)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        async for chain in generator:
            await self.send(chain)


def make_synthetic_event(
    context: Any,
    umo: str,
    text: str,
    *,
    sender_id: str = "astrbot",
    extras: dict[str, Any] | None = None,
) -> SyntheticEvent:
    """按 ``umo`` 造一条合成事件。``umo`` 形如 ``平台:消息类型:会话``。"""
    session = MessageSession.from_str(umo)
    return SyntheticEvent(
        context=context,
        session=session,
        message=text,
        sender_id=sender_id,
        extras=extras,
        message_type=session.message_type,
    )
