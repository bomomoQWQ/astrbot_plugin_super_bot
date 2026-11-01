"""Super Bot 插件入口。

分层（见 docs/设计/20-p0-spec.md）：

- ``superbot/`` 只放纯逻辑（gate / thread_key / store / limits），**不 import
  astrbot**，因此能在没有 AstrBot 的机器上单测；
- 本文件是唯一适配层：把 ``AstrMessageEvent`` 抽成 ``EventView``、决定
  ``stop_event`` / ``yield`` 的取舍、把判定写进 ``disposition`` 表。

P0 阶段的行为：只做**判定与记录**，不接管消息。也就是说
``ROUTE`` 之后仍然交给原生流程（非 @ 的群消息本来就不会触发主 agent）。
接管（路由到 Router / 直通 Worker）留到 P3/P4。

加载约定（踩过的坑，别改）：

1. 插件目录名必须是合法模块名 —— 本地用 junction 叫
   ``astrbot_plugin_super_bot``，仓库目录名带大写与连字符是不行的。
2. ``initialize`` / ``terminate`` 必须**直接定义在插件类里**：卸载逻辑检查
   ``"terminate" in star_cls_type.__dict__``（star_manager.py:1962-1963），
   继承来的不会被调用。
3. 包名用 ``superbot`` 而不是 ``core``，避免与宿主环境里其它 ``core`` 撞名。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

# 让 ``from superbot import ...`` 在本文件被以任意方式加载时都能工作
# （插件模块名是 data.plugins.<name>.main，相对导入在某些加载路径下不可靠）。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from astrbot.api import logger, star  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.event.filter import EventMessageType  # noqa: E402

from superbot import limits  # noqa: E402
from superbot.gate import (  # noqa: E402
    EventView,
    GateConfig,
    GateDecision,
    RateLimiter,
    Verdict,
    decide,
)
from superbot.store import DispositionRow, Store  # noqa: E402
from superbot.thread_key import thread_key  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_super_bot"
PLUGIN_VERSION = "0.1.0"
LOG_TAG = "[super-bot]"

__all__ = ["SuperBotPlugin"]


# ---------------------------------------------------------------------------
# 数据目录
# ---------------------------------------------------------------------------


def _resolve_data_dir() -> Path:
    """``data/plugin_data/<plugin>``。

    优先用官方的 ``StarTools.get_data_dir``；它依赖 ``star_map`` 与调用栈，
    在极端情况下可能失败，于是退回按路径常量手拼。
    """
    try:
        from astrbot.core.star.star_tools import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


# ---------------------------------------------------------------------------
# 适配层：AstrMessageEvent -> EventView
# ---------------------------------------------------------------------------


def to_view(event: AstrMessageEvent) -> EventView:
    """抽取只读视图。字段来源见 ``superbot/gate.py::EventView`` 的注释。"""
    return EventView(
        platform=_call(event, "get_platform_name", default=""),
        self_id=str(_call(event, "get_self_id", default="") or ""),
        sender_id=str(_call(event, "get_sender_id", default="") or ""),
        group_id=str(_call(event, "get_group_id", default="") or ""),
        session_id=str(_call(event, "get_session_id", default="") or ""),
        message_str=getattr(event, "message_str", "") or "",
        umo=str(getattr(event, "unified_msg_origin", "") or ""),
        session_isolated=bool(_extra(event, "_session_isolated", False)),
    )


def _call(obj: Any, name: str, *, default: Any = None) -> Any:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return default
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _extra(event: AstrMessageEvent, key: str, default: Any = None) -> Any:
    fn = getattr(event, "get_extra", None)
    if not callable(fn):
        return default
    try:
        return fn(key, default)
    except TypeError:
        try:
            return fn(key)
        except Exception:  # noqa: BLE001
            return default
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------


@star.register(
    PLUGIN_NAME,
    "bomomoQWQ",
    "Super-agent 插件：确定性准入（Gate）、Thread 订阅、Router/Worker 编队",
    PLUGIN_VERSION,
)
class SuperBotPlugin(star.Star):
    """P0：只做确定性准入与去向记录。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._store: Store | None = None
        self._limiter: RateLimiter | None = None
        self._gate_conf: GateConfig | None = None
        self._started_at = 0.0

    # -- 生命周期（必须直接定义在本类里） ----------------------------------

    async def initialize(self) -> None:
        started = time.perf_counter()
        data_dir = _resolve_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        self._store = Store(data_dir / limits.DB_FILENAME)
        await self._store.open()

        self._gate_conf = self._build_gate_conf()
        self._limiter = RateLimiter(
            self._gate_conf.rate_limit,
            self._gate_conf.rate_window_sec,
        )
        self._started_at = time.time()

        logger.info(
            "%s initialized: db=%s schema=v%s self_id=%s",
            LOG_TAG,
            data_dir / limits.DB_FILENAME,
            await self._store.schema_version(),
            self._gate_conf.bot_ids,
        )
        logger.info("%s startup took %.0f ms", LOG_TAG, (time.perf_counter() - started) * 1000)

    async def terminate(self) -> None:
        if self._store is not None:
            try:
                # 先 flush 偏好存储，再关库（插件更新前只有这一次回调机会）
                sp = getattr(self.context, "shared_preferences", None)
                if sp is not None and hasattr(sp, "flush"):
                    await sp.flush()
            except Exception:  # noqa: BLE001
                pass
            await self._store.close()
            self._store = None
        logger.info("%s terminated", LOG_TAG)

    # -- 配置 ---------------------------------------------------------------

    def _build_gate_conf(self) -> GateConfig:
        """从插件配置读；缺省即"不过滤"，便于冷启动先跑通。"""
        cfg: dict[str, Any] = {}
        raw = getattr(self, "config", None)
        if isinstance(raw, dict):
            cfg = raw.get("gate", {}) if isinstance(raw.get("gate"), dict) else raw

        return GateConfig(
            bot_ids=frozenset(
                str(x) for x in (cfg.get("bot_ids") or ["astrbot"])
            ),
            wake_prefixes=tuple(cfg.get("wake_prefixes") or limits.DEFAULT_WAKE_PREFIXES),
            command_names=frozenset(str(x) for x in (cfg.get("command_names") or [])),
            allowed_sources=frozenset(
                str(x) for x in (cfg.get("allowed_sources") or [])
            ),
            rate_limit=int(cfg.get("rate_limit") or limits.DEFAULT_RATE_LIMIT),
            rate_window_sec=float(
                cfg.get("rate_window_sec") or limits.DEFAULT_RATE_WINDOW_SEC
            ),
        )

    # -- 主处理器 ----------------------------------------------------------

    @filter.event_message_type(EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """拦截所有消息，做确定性准入。

        ⚠ 这里**只判定、不接管**。三种结局：

        - ``IGNORE``：裸 ``return``，事件自然结束（非 @ 群消息不会惊动主 agent）
        - ``PASS``：裸 ``return``，交给原生流程（指令）
        - ``ROUTE``：P0 阶段同样裸 ``return``，让原生流程处理；
          P3 起改为"投队列 + yield 一句确认"，届时**必须 yield**，
          否则 `_has_send_oper` 仍为假、主 agent 还会再回一遍
          （process_stage/stage.py:56-66）。
        """
        view = to_view(event)
        conf = self._gate_conf or GateConfig()
        decision = decide(view, conf, self._limiter)

        thread = ""
        if decision.verdict is Verdict.ROUTE:
            thread = thread_key(view, reply_ref=_reply_ref(event))

        await self._record(view, decision, thread)
        logger.info(
            "%s gate=%s reason=%s chat=%s sender=%s thread=%s text=%r",
            LOG_TAG,
            decision.verdict.value,
            decision.reason,
            view.chat_key,
            view.sender_id,
            thread or "-",
            view.message_str[:40],
        )
        return

    # -- 记录 ---------------------------------------------------------------

    async def _record(
        self, view: EventView, decision: GateDecision, thread: str
    ) -> None:
        if self._store is None:
            return
        try:
            await self._store.record_disposition(
                DispositionRow(
                    trigger_id=None,
                    verdict=decision.verdict.value,
                    reason=f"{decision.reason}|{view.chat_key}|{thread}",
                    ts=time.time(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s record disposition failed: %s", LOG_TAG, exc)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _reply_ref(event: AstrMessageEvent) -> str | None:
    """从消息链里取被回复消息的引用（OneBot 的 ``reply`` 段）。"""
    msg = getattr(event, "message_obj", None)
    chain = getattr(msg, "message", None) if msg is not None else None
    if not chain:
        return None
    for seg in chain:
        if getattr(seg, "type", None) == "reply":
            rid = getattr(seg, "id", None) or getattr(seg, "message_id", None)
            if rid:
                return str(rid)
    return None
