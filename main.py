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
from superbot.sandbox import BaySandbox, SandboxPort, keepalive_loop  # noqa: E402
from superbot.store import DispositionRow, Store  # noqa: E402
from superbot.synthetic import make_synthetic_event  # noqa: E402
from superbot.thread_key import thread_key, ticket_id_from_eval, trigger_id  # noqa: E402
from superbot.worker import (  # noqa: E402
    Assignment,
    Outcome,
    Ticket,
    WorkerPool,
    WorkerResult,
)

# 让框架自带的 /stop 能停到我们：把每个会话的活跃任务登记进 ActiveEventRegistry。
# 这个模块是 AstrBot 内置命令用的同一个（builtin_stars/builtin_commands/commands/
# conversation.py:117-122），所以 `/stop` 会顺着它找到我们，不需要我们自己拦截命令。
try:
    from astrbot.core.utils.active_event_registry import active_event_registry
except Exception:  # noqa: BLE001
    active_event_registry = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_super_bot"
PLUGIN_VERSION = "0.1.0"
LOG_TAG = "[super-bot]"

EVAL_MODE_ENV = "SUPERBOT_EVAL_MODE"
"""``ignore`` / ``echo`` / ``live``。

- ``ignore``：永不回复（只验证批处理）
- ``echo``：对以 ``sb:`` 开头的批次回一句确定性文本
- ``live``：用**真模型**做评估（走 ``Context.tool_loop_agent``）
"""

SANDBOX_ENV = "SUPERBOT_SANDBOX"
"""设为 ``1`` 时 Worker 在真沙箱里执行（Shipyard Neo / Bay）。"""

BAY_ENDPOINT_ENV = "BAY_ENDPOINT"
BAY_KEY_ENV = "BAY_API_KEY"

KEEPALIVE_ENV = "SUPERBOT_KEEPALIVE_SEC"
"""沙箱保活间隔（秒），默认 1200。

为什么必须自己续：AstrBot 全仓库**从不**调用 keepalive / extend_ttl，
而 Bay 的默认 TTL 是 3600s，到期后 managed Cargo 会被级联删除——工作区直接没了。
"""

EVAL_DELAY_ENV = "SUPERBOT_EVAL_DELAY"
"""桩评估器的假延迟（秒）。真实模型本来就有 1~3 秒延迟，批处理窗口就是它。"""

STUB_NEEDLE = "sb:"
"""桩评估器只对这些消息回复，其余一律忽略——避免"每条群消息都插一句"。"""

WORKER_DELAY_ENV = "SUPERBOT_WORKER_DELAY"
"""桩 Worker 的假耗时（秒）。默认取 ``min(0.3, eval_delay)``。

测试 ``/stop`` 时要把它调大（比如 30），否则任务结束得太快、来不及取消。
"""

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
        self._sandbox: SandboxPort | None = None
        self._sandbox_ids: set[str] = set()
        # 停止钩子：umo -> 合成事件；umo -> 该会话在跑的 ticket；ticket -> assignment
        self._stop_events: dict[str, Any] = {}
        self._umo_tickets: dict[str, set[str]] = {}
        self._assignment_ids: dict[str, str] = {}
        self._keepalive_task: asyncio.Task | None = None
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

        # 沙箱（可选）：一个 Ticket 一个 sandbox，自己直连 Bay
        if _sandbox_enabled():
            endpoint = os.environ.get(BAY_ENDPOINT_ENV) or "http://localhost:8114"
            key = _bay_key()
            if key:
                self._sandbox = BaySandbox(endpoint=endpoint, api_key=key, ttl=3600)
                logger.info("%s sandbox enabled: %s", LOG_TAG, endpoint)
            else:
                logger.warning(
                    "%s SUPERBOT_SANDBOX=1 但找不到 BAY_API_KEY，Worker 退回桩执行",
                    LOG_TAG,
                )
        # 批处理调度：单槽 + 只追加，不用定时器
        self._router = Router(evaluate=self._evaluate)
        self._stop = asyncio.Event()
        self._router_task = asyncio.create_task(
            self._router.run_forever(self._stop),
            name="superbot-router",
        )
        # 沙箱保活：AstrBot 自己从不续 TTL，到期工作区会被级联删除
        self._start_keepalive()

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
        if self._stop is not None:            self._stop.set()
        if self._router_task is not None:
            self._router_task.cancel()
            try:
                await self._router_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._router_task = None

        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._keepalive_task = None

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

        # 把登记进 registry 的合成事件都摘掉，别让 /stop 指向已经没了的东西
        if active_event_registry is not None:
            for event in list(self._stop_events.values()):
                try:
                    active_event_registry.unregister(event)
                except Exception:  # noqa: BLE001
                    pass
        self._stop_events.clear()
        self._umo_tickets.clear()
        self._assignment_ids.clear()
        self._ticket_ctx.clear()

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

    # -- 沙箱保活 -----------------------------------------------------------

    def _start_keepalive(self) -> None:
        """启动保活循环（没有沙箱时什么都不做）。

        AstrBot 全仓库**从不**调用 ``keepalive`` / ``extend_ttl``，而 Bay 的默认
        TTL 是 3600s；到期后 managed Cargo 会被**级联删除**——工作区直接没了。
        所以这条循环不是优化，是正确性的一部分。
        """
        if self._sandbox is None or self._stop is None:
            return
        if self._keepalive_task is not None:
            return
        interval = _keepalive_interval()
        self._keepalive_task = asyncio.create_task(
            keepalive_loop(
                self._sandbox,
                lambda: list(self._sandbox_ids),
                interval=interval,
                stop=self._stop,
                on_result=lambda ok, fail: logger.info(
                    "%s keepalive ok=%d fail=%d", LOG_TAG, ok, fail
                ),
            ),
            name="superbot-keepalive",
        )
        logger.info("%s keepalive started: every %.0fs", LOG_TAG, interval)

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

        if _eval_mode() == "live":
            return await self._evaluate_live(batch)

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
            assignment_id = await self._workers.submit(
                Ticket(
                    id=ticket_id,
                    thread_key=thread,
                    mandate=f"stub batch={len(batch)}",
                    created_at=time.time(),
                )
            )
            self._register_stop_hook(batch[-1].umo, ticket_id, assignment_id)
            logger.info(
                "%s ticket %s dispatched (batch=%d)", LOG_TAG, ticket_id[:8], len(batch)
            )
        return []

    async def _evaluate_live(self, batch: list[Trigger]) -> list[Reply]:
        """真模型评估器：用 ``Context.tool_loop_agent`` 判断该怎么回。

        刻意**不给工具**（``max_steps=1``）：Router 只判断与分派，不自己执行
        （ADR-02）。真正的执行交给 Worker。

        没有真实消息时用合成事件当身份载体（``superbot/synthetic.py``）——
        它自带 UMO、``is_wake=True``，也能被 ``ActiveEventRegistry`` 登记。
        """
        if not batch:
            return []
        umo = batch[-1].umo
        thread = batch[-1].thread_key
        digest = "\n".join(f"- {t.actor_id}: {t.text}" for t in batch[:10])

        try:
            event = make_synthetic_event(self.context, umo, digest)
            provider_id = await self.context.get_current_chat_provider_id(umo)
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=(
                    "下面是一个群/会话里最近的几条消息：\n"
                    f"{digest}\n\n"
                    "请用一句简短的中文回应其中真正需要回应的内容；"
                    "如果都不需要回应，只输出 SKIP。"
                ),
                system_prompt="你是简洁的群助手，只输出一句话，不要客套，不要解释。",
                max_steps=1,
                tool_call_timeout=60,
            )
            plan = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s live eval failed: %s", LOG_TAG, exc)
            return []

        logger.info("%s live eval -> %r", LOG_TAG, plan[:80])
        if not plan or plan.upper().startswith("SKIP"):
            return []

        ticket_id = ticket_id_from_eval(f"{thread}:{batch[0].id}")
        self._ticket_ctx[ticket_id] = {
            "umo": umo,
            "thread_key": thread,
            "batch": len(batch),
            "plan": plan,
        }
        if self._workers is not None:
            assignment_id = await self._workers.submit(
                Ticket(
                    id=ticket_id,
                    thread_key=thread,
                    mandate=f"live batch={len(batch)}",
                    created_at=time.time(),
                )
            )
            self._register_stop_hook(umo, ticket_id, assignment_id)
            logger.info(
                "%s ticket %s dispatched (live, batch=%d)",
                LOG_TAG,
                ticket_id[:8],
                len(batch),
            )
        return []

    async def _run_worker(self, ticket: Ticket) -> WorkerResult:
        """Worker：有沙箱就在沙箱里执行，否则走桩。

        沙箱路径刻意演示了必须自己做的两件事：**创建**与**销毁**
        （``finally`` 里 destroy —— 取消时也一样，否则容器继续占配额）。
        """
        ctx = self._ticket_ctx.get(ticket.id) or {}

        if self._sandbox is None:
            await asyncio.sleep(_worker_delay())
            if ctx.get("plan"):
                return WorkerResult(text=ctx["plan"], ok=True)
            return WorkerResult(text=f"ECHO-BATCH:{ctx.get('batch', 0)}", ok=True)

        sandbox_id: str | None = None
        try:
            for attempt in (1, 2):
                sandbox_id = await self._sandbox.create(ticket_id=ticket.id)
                self._sandbox_ids.add(sandbox_id)
                logger.info(
                    "%s sandbox %s created for ticket %s (attempt %d)",
                    LOG_TAG,
                    sandbox_id,
                    ticket.id[:8],
                    attempt,
                )

                res = await self._sandbox.exec_python(sandbox_id, "print(6*7)")
                if res.ok:
                    break

                # 「沙箱在创建与执行之间就没了」是真实存在的情况（预热池轮转、
                # TTL 到期、被 GC）。这种情况下重试同一个 id 没有意义，要重建。
                gone = "not found" in (res.error or "").lower()
                logger.warning(
                    "%s sandbox exec failed (attempt %d, gone=%s): %s",
                    LOG_TAG,
                    attempt,
                    gone,
                    res.error,
                )
                if attempt == 2:
                    logger.error("%s sandbox exec gave up: %s", LOG_TAG, res.error)
                    return WorkerResult(text=f"沙箱执行失败: {res.error}", ok=False)

                # 先收拾掉这个不健康的沙箱，再重建
                self._sandbox_ids.discard(sandbox_id)
                try:
                    await self._sandbox.destroy(sandbox_id)
                except Exception:  # noqa: BLE001
                    pass
                sandbox_id = None
                if not gone:
                    await asyncio.sleep(1.0)

            out = (res.output or "").strip()
            await self._sandbox.write_file(sandbox_id, "ticket.txt", ticket.id)
            plan = ctx.get("plan") or ""
            text = f"{plan}（沙箱 6×7={out}）" if plan else f"SANDBOX-OK:{out}"
            return WorkerResult(text=text, ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s sandbox worker failed: %s", LOG_TAG, exc)
            return WorkerResult(text=f"沙箱失败: {exc}", ok=False)
        finally:
            if sandbox_id is not None:
                self._sandbox_ids.discard(sandbox_id)
                try:
                    await self._sandbox.destroy(sandbox_id)
                    logger.info("%s sandbox %s destroyed", LOG_TAG, sandbox_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s sandbox destroy failed: %s", LOG_TAG, exc)

    async def _on_worker_done(self, assignment: Assignment) -> None:
        """Worker 结束 → 结果交给 Outbox（不直接对用户信道说话）。"""
        ctx = self._ticket_ctx.get(assignment.ticket.id) or {}
        umo = ctx.get("umo")
        logger.info(
            "%s worker done ticket=%s outcome=%s text=%r",
            LOG_TAG,
            assignment.ticket.id[:8],
            assignment.outcome.value if assignment.outcome else "?",
            assignment.text[:200],
        )
        self._ticket_ctx.pop(assignment.ticket.id, None)
        self._unregister_stop_hook(assignment.ticket.id)
        if not umo:
            return

        # 失败要说话：Router 已经决定要回了，结果 Worker 挂了，静默会让用户
        # 以为"它不想理我"。取消（/stop）则不该补话。
        if assignment.outcome is Outcome.FAILED:
            logger.error(
                "%s ticket %s failed: %s",
                LOG_TAG,
                assignment.ticket.id[:8],
                assignment.error or assignment.text,
            )
            await self._emit(
                [
                    Reply(
                        umo=umo,
                        text="（这次没执行成功，稍后再试）",
                        thread_key=assignment.ticket.thread_key,
                    )
                ]
            )
            return
        if assignment.outcome is Outcome.CANCELLED or not assignment.text:
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

    # -- 停止钩子：让框架的 /stop 能停到我们 -------------------------------

    def _register_stop_hook(self, umo: str, ticket_id: str, assignment_id: str) -> None:
        """把一个会话的活跃任务登记进 ``ActiveEventRegistry``。

        为什么这样做而不自己拦截 ``/stop``：内置命令走的是
        ``active_event_registry.request_agent_stop_all(umo)``
        （builtin_stars/builtin_commands/commands/conversation.py:119），
        它遍历该 UMO 下的活跃事件，调 ``register_agent_stop_callback`` 注册的回调。
        登记进去，``/stop`` 就**天然**能找到我们，而且回执里的任务计数也是对的
        （不会出现"插件停了但提示说 0 个任务"）。

        每个会话只登记**一个**合成事件（不是每个工单一个）：语义本来就是
        "停掉这个会话里正在跑的所有事"。
        """
        if active_event_registry is None:
            return
        self._umo_tickets.setdefault(umo, set()).add(ticket_id)
        self._assignment_ids[ticket_id] = assignment_id

        if umo in self._stop_events:
            return
        try:
            event = make_synthetic_event(self.context, umo, "[super-bot task]")
            # 回调是同步的（registry 直接调用），只能 schedule
            active_event_registry.register(event)
            active_event_registry.register_agent_stop_callback(
                event, lambda: asyncio.create_task(self._cancel_umo(umo))
            )
            self._stop_events[umo] = event
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s register stop hook failed: %s", LOG_TAG, exc)

    def _unregister_stop_hook(self, ticket_id: str) -> None:
        self._assignment_ids.pop(ticket_id, None)
        for umo, tickets in list(self._umo_tickets.items()):
            tickets.discard(ticket_id)
            if tickets:
                continue
            self._umo_tickets.pop(umo, None)
            event = self._stop_events.pop(umo, None)
            if event is not None and active_event_registry is not None:
                try:
                    active_event_registry.unregister(event)
                except Exception:  # noqa: BLE001
                    pass

    async def _cancel_umo(self, umo: str) -> None:
        """``/stop`` 的实际动作：取消在跑的工单 + 清空还在排队的 Trigger。

        两步都要做：只取消在跑的，排队的下一批会紧接着又跑起来，
        用户会觉得"根本没停下"。
        """
        tickets = list(self._umo_tickets.get(umo, ()))

        # 1) 清掉还在排队的 Trigger（按这些工单所属的 Thread 清）
        threads = {
            ctx["thread_key"]
            for tid in tickets
            if (ctx := self._ticket_ctx.get(tid)) and ctx.get("thread_key")
        }
        dropped = 0
        if self._router is not None:
            if threads:
                for thread in threads:
                    dropped += self._router.clear(thread)
            elif not tickets:
                # 没有登记工单（可能是纯排队阶段）→ 整体清一次
                dropped = self._router.clear()

        # 2) 取消正在跑的工单
        cancelled = 0
        if self._workers is not None:
            for ticket_id in tickets:
                aid = self._assignment_ids.get(ticket_id)
                if aid and await self._workers.cancel(aid):
                    cancelled += 1

        logger.info(
            "%s stop umo=%s cancelled=%d dropped_pending=%d",
            LOG_TAG,
            umo,
            cancelled,
            dropped,
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


def _worker_delay() -> float:
    """桩 Worker 的假耗时。"""
    raw = os.environ.get(WORKER_DELAY_ENV)
    if raw is None:
        return min(0.3, _eval_delay())
    try:
        return max(0.0, float(raw))
    except ValueError:
        return min(0.3, _eval_delay())


def _keepalive_interval() -> float:
    """保活间隔（秒）。默认 1200（20 分钟），比 1 小时的 TTL 安全。

    下限取 0.05 而不是 1：既挡住 0/负数（会变成忙等），又让测试能用小间隔
    真的观察到循环在跑。生产上没人会配这个值。
    """
    raw = os.environ.get(KEEPALIVE_ENV)
    if raw is None:
        return 1200.0
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 1200.0


def _sandbox_enabled() -> bool:
    return (os.environ.get(SANDBOX_ENV) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _bay_key() -> str | None:
    """Bay 的 API key：环境变量优先，其次 ``.runtime/bay_key.txt``。

    ⚠ 读文件时用 ``utf-8-sig`` —— PowerShell 5.1 的 ``Set-Content -Encoding utf8``
    会写 BOM，带着 BOM 的 key 会被 Bay 判为 401（我们踩过）。
    """
    if k := os.environ.get(BAY_KEY_ENV):
        return k.strip()
    p = _HERE / ".runtime" / "bay_key.txt"
    if p.exists():
        return p.read_text(encoding="utf-8-sig").strip()
    return None
