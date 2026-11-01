"""Super Bot 插件入口。

分层（见 docs/设计/20-p0-spec.md）：

- ``superbot/`` 只放纯逻辑（gate / thread_key / store / outbox / watchdog /
  router / limits），**不 import astrbot**，因此能在没有 AstrBot 的机器上单测；
- 本文件是唯一适配层：把 ``AstrMessageEvent`` 抽成 ``EventView``、决定
  ``stop_event`` / ``yield`` 的取舍、把判定写进 ``disposition`` 表、把 Router
  的回复交给 Outbox 发出去。

当前阶段的行为（P3 骨架）：

- ``IGNORE``  → 裸 ``return``，事件自然结束
- ``PASS``    → 裸 ``return``，交给原生流程（指令、显式 @）
- ``ROUTE``   → 入 Router 队列；**不 yield**（非 @ 消息本来就不会惊动主 agent）

评估器目前是确定性桩（``SUPERBOT_EVAL_MODE`` 控制），P5 换成调模型的真评估器。

加载约定（踩过的坑，别改）：

1. 插件目录名必须是合法模块名 —— 本地用 junction 叫
   ``astrbot_plugin_super_bot``，仓库目录名带大写与连字符是不行的。
2. ``initialize`` / ``terminate`` 必须**直接定义在插件类里**：卸载逻辑检查
   ``"terminate" in star_cls_type.__dict__``（star_manager.py:1962-1963）。
3. 包名用 ``superbot`` 而不是 ``core``，避免与宿主环境里其它 ``core`` 撞名。
"""

from __future__ import annotations

import asyncio
import os
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
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # noqa: E402
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
from superbot.outbox import Outbox  # noqa: E402
from superbot.router import Reply, Router, Trigger  # noqa: E402
from superbot.store import DispositionRow, Store  # noqa: E402
from superbot.thread_key import thread_key, ticket_id_from_eval, trigger_id  # noqa: E402
from superbot.worker import Assignment, Ticket, WorkerPool, WorkerResult  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_super_bot"
PLUGIN_VERSION = "0.1.0"
LOG_TAG = "[super-bot]"

EVAL_MODE_ENV = "SUPERBOT_EVAL_MODE"
"""``ignore``（默认，永不回复）或 ``echo``（对以 ``sb:`` 开头的批次回一句）。"""

EVAL_DELAY_ENV = "SUPERBOT_EVAL_DELAY"
"""桩评估器的假延迟（秒）。真实模型本来就有 1~3 秒延迟，批处理窗口就是它。"""

STUB_NEEDLE = "sb:"
"""桩评估器只对这些消息回复，其余一律忽略——避免"每条群消息都插一句"。"""

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
        woken=bool(getattr(event, "is_at_or_wake_command", False)),
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


def _message_id(event: AstrMessageEvent) -> str:
    """取平台侧的消息 id，用作 Trigger 幂等键的一部分。"""
    msg = getattr(event, "message_obj", None)
    mid = getattr(msg, "message_id", None) if msg is not None else None
    return str(mid) if mid else ""


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
    """确定性准入 + Router 批处理 + 可靠投递。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._store: Store | None = None
        self._limiter: RateLimiter | None = None
        self._gate_conf: GateConfig | None = None
        self._router: Router | None = None
        self._outbox: Outbox | None = None
        self._workers: WorkerPool | None = None
        self._stop: asyncio.Event | None = None
        self._router_task: asyncio.Task | None = None
        self._ticket_ctx: dict[str, dict[str, Any]] = {}
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

        # 投递队列：同一会话串行、失败重试、幂等入队
        self._outbox = Outbox(send=self._send_text)

        # Worker 池：隔离执行、并发上限、可取消
        self._workers = WorkerPool(run=self._run_worker, on_done=self._on_worker_done)

        # 批处理调度：单槽 + 只追加，不用定时器
        self._router = Router(evaluate=self._evaluate)
        self._stop = asyncio.Event()
        self._router_task = asyncio.create_task(
            self._router.run_forever(self._stop),
            name="superbot-router",
        )

        self._started_at = time.time()
        logger.info(
            "%s initialized: db=%s schema=v%s self_id=%s eval_mode=%s",
            LOG_TAG,
            data_dir / limits.DB_FILENAME,
            await self._store.schema_version(),
            self._gate_conf.bot_ids,
            _eval_mode(),
        )
        logger.info(
            "%s startup took %.0f ms", LOG_TAG, (time.perf_counter() - started) * 1000
        )

    async def terminate(self) -> None:
        # 顺序很重要：先停循环，再排空队列，最后 flush + 关库。
        if self._stop is not None:
            self._stop.set()
        if self._router_task is not None:
            self._router_task.cancel()
            try:
                await self._router_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._router_task = None

        if self._outbox is not None:
            try:
                await self._outbox.drain()
                await self._outbox.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s outbox flush failed: %s", LOG_TAG, exc)
            self._outbox = None

        if self._workers is not None:
            try:
                await self._workers.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s worker shutdown failed: %s", LOG_TAG, exc)
            self._workers = None

        if self._store is not None:
            try:
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
            bot_ids=frozenset(str(x) for x in (cfg.get("bot_ids") or ["astrbot"])),
            wake_prefixes=tuple(
                cfg.get("wake_prefixes") or limits.DEFAULT_WAKE_PREFIXES
            ),
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
        """拦截所有消息，做确定性准入；需要接管时投进 Router 队列。

        三种结局：

        - ``IGNORE`` / ``PASS`` → 裸 ``return``（事件自然结束 / 交给原生流程）
        - ``ROUTE`` → 入 Router 队列，同样裸 ``return``

        为什么 ``ROUTE`` 不 yield：非 @ 的群消息 ``is_at_or_wake_command`` 为假，
        主 agent 本来就不会跑（process_stage/stage.py:56-66），所以不 yield 也不会
        出现双回。反过来，将来若要接管 @ 消息，**必须** yield 一句确认，否则
        ``_has_send_oper`` 仍为假，主 agent 会再回一遍。
        """
        view = to_view(event)
        conf = self._gate_conf or GateConfig()
        decision = decide(view, conf, self._limiter)

        thread = ""
        if decision.verdict is Verdict.ROUTE:
            thread = thread_key(view, reply_ref=_reply_ref(event))
            self._enqueue(view, thread, event)

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

    # -- Router 输入 -------------------------------------------------------

    def _enqueue(self, view: EventView, thread: str, event: AstrMessageEvent) -> None:
        if self._router is None:
            return
        tid = trigger_id("aiocqhttp", thread, _message_id(event) or f"{view.sender_id}:{time.time()}")
        added = self._router.enqueue(
            Trigger(
                id=tid,
                thread_key=thread,
                umo=view.umo,
                actor_id=view.sender_id,
                text=view.message_str,
                ts=time.time(),
            )
        )
        if not added:
            logger.debug("%s duplicate trigger ignored: %s", LOG_TAG, tid)

    # -- Router 评估器（当前为确定性桩） -----------------------------------

    async def _evaluate(self, batch: list[Trigger]) -> list[Reply]:
        """P3：确定性桩。P5 换成调 ``tool_loop_agent`` 的真评估器。

        桩的两个作用：
        1. 用假延迟模拟模型耗时 —— 批处理的合并窗口就是它，没有延迟就没有合并；
        2. 只对带 ``sb:`` 标记的消息回复，避免"每条群消息都插一句"。

        判定"要干活"之后不直接回复，而是**开一张工单交给 Worker**，
        这样跑通的是完整链路：Gate → Router → Worker → Outbox → send_message。
        """
        delay = _eval_delay()
        if delay > 0:
            await asyncio.sleep(delay)

        logger.info(
            "%s router eval thread=%s batch=%d texts=%s",
            LOG_TAG,
            batch[0].thread_key if batch else "-",
            len(batch),
            [t.text[:16] for t in batch[:4]],
        )

        if _eval_mode() != "echo":
            return []
        if not any(t.text.lstrip().startswith(STUB_NEEDLE) for t in batch):
            return []

        thread = batch[-1].thread_key
        ticket_id = ticket_id_from_eval(f"{thread}:{batch[0].id}")
        self._ticket_ctx[ticket_id] = {
            "umo": batch[-1].umo,
            "thread_key": thread,
            "batch": len(batch),
        }

        if self._workers is not None:
            await self._workers.submit(
                Ticket(
                    id=ticket_id,
                    thread_key=thread,
                    mandate=f"stub batch={len(batch)}",
                    created_at=time.time(),
                )
            )
            logger.info(
                "%s ticket %s dispatched (batch=%d)", LOG_TAG, ticket_id[:8], len(batch)
            )
        return []

    async def _run_worker(self, ticket: Ticket) -> WorkerResult:
        """P3 桩 Worker：模拟一次耗时执行。P5 换成真工具循环。"""
        ctx = self._ticket_ctx.get(ticket.id) or {}
        await asyncio.sleep(min(0.3, _eval_delay()))
        return WorkerResult(text=f"ECHO-BATCH:{ctx.get('batch', 0)}", ok=True)

    async def _on_worker_done(self, assignment: Assignment) -> None:
        """Worker 结束 → 结果交给 Outbox（不直接对用户信道说话）。"""
        ctx = self._ticket_ctx.get(assignment.ticket.id) or {}
        umo = ctx.get("umo")
        logger.info(
            "%s worker done ticket=%s outcome=%s text=%r",
            LOG_TAG,
            assignment.ticket.id[:8],
            assignment.outcome.value if assignment.outcome else "?",
            assignment.text[:40],
        )
        self._ticket_ctx.pop(assignment.ticket.id, None)
        if not umo or not assignment.text:
            return
        if assignment.outcome and assignment.outcome.value != "ok":
            return
        await self._emit(
            [
                Reply(
                    umo=umo,
                    text=assignment.text,
                    thread_key=assignment.ticket.thread_key,
                )
            ]
        )

    async def _emit(self, replies: list[Reply]) -> None:
        """把回复交给 Outbox（同会话串行、失败重试）。"""
        if self._outbox is None or not replies:
            return
        for reply in replies:
            await self._outbox.enqueue(reply.umo, reply.text)
        # 不 await drain：让 Router 立刻回去取下一批，投递在后台进行
        asyncio.create_task(self._outbox.drain(), name="superbot-outbox-drain")

    # -- 发送（Outbox 的 adapter） -----------------------------------------

    async def _send_text(self, umo: str, text: str) -> bool:
        """真正把一句话发到某个会话。

        用 ``context.send_message`` 而不是 ``event.send``：后者绑定在具体事件上，
        而 Router 的回复可能发生在事件早已结束之后（也可能发到别的会话）。
        """
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s send to %s failed: %s", LOG_TAG, umo, exc)
            return False
        logger.info("%s sent to %s: %r (ok=%s)", LOG_TAG, umo, text[:40], ok)
        return bool(ok)

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
# 桩评估器的环境配置
# ---------------------------------------------------------------------------


def _eval_mode() -> str:
    return (os.environ.get(EVAL_MODE_ENV) or "ignore").strip().lower()


def _eval_delay() -> float:
    raw = os.environ.get(EVAL_DELAY_ENV)
    if raw is None:
        return 0.6
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.6
