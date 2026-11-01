"""全部阈值与常量集中一处。

依据：docs/调查/02-state.md、docs/调查/03-triggers.md、docs/调查/05-sandbox.md。
任何"魔法数字"都不应散落在业务代码里。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
"""状态库 schema 版本。改表必须 +1 并补一条迁移。"""

# ---------------------------------------------------------------------------
# Gate（准入层）
# ---------------------------------------------------------------------------

DEFAULT_WAKE_PREFIXES: tuple[str, ...] = ("/",)
"""唤醒前缀。以任一前缀开头的消息视为指令，直接放行给原生流程。"""

DEFAULT_RATE_LIMIT = 30
"""每来源的窗口内消息上限。

注意：AstrBot 自身 RateLimitStage 默认也是 30 次 / 60 秒
（config/default.py:69-73），常驻 Router 很容易撞上，必要时对 Router 的
UMO 单独放宽。
"""

DEFAULT_RATE_WINDOW_SEC = 60.0
"""限流窗口长度（秒）。"""

# ---------------------------------------------------------------------------
# Router / Worker
# ---------------------------------------------------------------------------

ROUTER_QUEUE_MAXSIZE = 512
"""Router 待处理 Trigger 队列上限。满了直接丢弃最旧的并记一条 Disposition。"""

ROUTER_BATCH_MAX = 32
"""单次 Router 推理最多合并多少条 Trigger（对应文章 2.1 的 batching）。"""

WORKER_MAX_CONCURRENCY = 4
"""同时运行的 Worker 上限。真实瓶颈是宿主资源与沙箱配额，不是这个数字。"""

WORKER_MAX_STEPS = 30
"""单次 Worker 工具循环的最大步数（与 tool_loop_agent 默认值保持一致）。"""

# ---------------------------------------------------------------------------
# Watchdog（任务级停滞检测）
# ---------------------------------------------------------------------------

WATCHDOG_TICK_SEC = 60.0
"""Watchdog 心跳间隔。对应《为人民服务》的 STALL_CHECK_INTERVAL_MS = 60_000。"""

STALL_THRESHOLD_SEC = 600.0
"""超过这么久没有进展即判停滞。对应 STALL_THRESHOLD_MS = 600_000。"""

MAX_RETRIES = 3
"""触发升级前的最大自动重试次数。"""

RETRY_BACKOFF_SEC: tuple[float, ...] = (30.0, 60.0, 120.0)
"""重试退避序列。对应 RETRY_BACKOFF = [30s, 60s, 120s]。"""

MAX_ESCALATION = 3
"""最大升级层数。超过即标记 failed。"""

# ---------------------------------------------------------------------------
# 落盘 / 发送
# ---------------------------------------------------------------------------

DB_FILENAME = "state.db"
"""状态库文件名。目录由 StarTools.get_data_dir() 给出（data/plugin_data/<plugin>）。"""

SQLITE_BUSY_TIMEOUT_MS = 30000
"""抄 AstrBot 自己的取值（db/__init__.py:40-52）。"""

SEND_MAX_ATTEMPTS = 3
"""单条外发消息的最大尝试次数。"""

SEND_BACKOFF_SEC: tuple[float, ...] = (5.0, 30.0, 300.0)
"""发送重试退避。参考实现 Outbox 的 5s → 300s。"""

TRIGGER_RETENTION_DAYS = 14
"""trigger 表的保留天数，防止无限增长。"""

DISPOSITION_RETENTION_DAYS = 30
"""disposition（去向审计）的保留天数。"""

HOUSEKEEPING_INTERVAL_SEC = 6 * 3600.0
"""定期清理（prune）的间隔，默认 6 小时。disposition 每条消息写一行，
不清理的话跑一年就是个没人管的垃圾堆。"""

TICKET_RETENTION_DAYS = 90
"""工单（含它的事实）的保留天数。这是审计数据，留久一点但也不能永远留。"""
