# 20 · P0 骨架规格（可先写、不依赖沙箱与 Router）

> 目标：把**最容易静默出错**的部分先用纯函数/纯数据做对，并让它可单测。
> 依据流 B 报告（`../调查/02-state.md`）修订了持久化方案。
> 本文件不依赖流 C/D/E 的结论，可直接开工。

---

## 0. 为什么 P0 只有这三件事

参考实现的启示：`admission.ts` **1KB**（确定性）vs `assessment.ts` **20KB**（模型判断）。能写成代码的判断，绝不该交给模型。而这三块恰好全是纯逻辑，且**不写就静默出错**。

---

## 1. `gate.py` —— 确定性准入（四条规则）

```python
from dataclasses import dataclass
from enum import Enum

class Verdict(Enum):
    IGNORE = "ignore"     # 直接丢弃
    PASS   = "pass"       # 放行给原生流程（指令等）
    ROUTE  = "route"      # 交给 Router

@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: str            # 供日志与 /sb why 查询
```

### 规则 1：丢弃机器人自己的消息（**最高优先**）

```python
# 依据：ignore_bot_self_message 默认 False（waking_check/stage.py:64-67）
# 而合成事件的 sender_id 默认就是 "astrbot"（cron/events.py:23）
if event.get_sender_id() in conf.self_ids:      # 含 event.get_self_id() 与配置的机器人 id
    return GateDecision(Verdict.IGNORE, "self_message")
```

> **不能依赖配置默认值**。Worker 用 `send_message` 发出的消息若被平台回显，这一条是唯一的防回环防线。

### 规则 2：放行指令与插件自有命令

```python
if event.message_str.startswith(tuple(conf.wake_prefixes)) or event.is_command():
    return GateDecision(Verdict.PASS, "command")
```

理由：`/help` `/sid` `/provider` 由 `builtin_commands` 与 core 处理；全量接管会把它们一起打断。

### 规则 3：来源白名单（程序层，不调模型）

```python
key = (event.get_platform_name(), event.get_group_id() or "private")
if conf.sources and key not in conf.sources:
    return GateDecision(Verdict.IGNORE, "source_not_allowed")
```

### 规则 4：节流（每来源每分钟 N 条，超了丢弃并记一条 `Disposition`）

```python
if rate_limiter.exceeded(key):
    return GateDecision(Verdict.IGNORE, "rate_limited")
```

### 输出

| 判定 | handler 动作 | 主 agent 会不会跑 |
|---|---|---|
| `IGNORE` | 裸 `return` | **不会**（`is_at_or_wake_command` 为假） |
| `PASS` | 裸 `return` | 会（指令本来就要它跑） |
| `ROUTE` 同步路径 | `yield event.request_llm(...)` | 会，且用你的 ProviderRequest |
| `ROUTE` 异步路径 | 入队 + `yield` 一句"收到" | **不会**（洋葱中间已发送 → `_has_send_oper=True`） |

> ⚠ **唯一的死坑**：异步路径里 `stop_event()` 之后**什么都不 yield**。此时 `get_result()` 为 None → `process_stage/stage.py:63` 的 `or not event.get_result()` 为真 → **主 agent 照样跑**。机制见 `../调查/01-runtime.md` §2.2。

---

## 2. `thread_key.py` —— Thread 指纹（纯函数）

```python
def thread_key(event, conf) -> str:
    """同一件事的归属。只读 event 的取值方法，不碰网络与存储。"""
```

### 两种模式（取决于 `unique_session`）

| 模式 | 判定 | 基座 |
|---|---|---|
| `unique_session = False` | UMO 的 `session_id` 就是群号 | 可直接用 `event.get_group_id()` |
| `unique_session = True` | UMO 被改写为 `{sender_id}_{group_id}` | **必须显式 `event.get_group_id()`，不能用 UMO** |

> 依据：`unified_msg_origin` 就是 `str(self.session)`（`astr_message_event.py:106-108`），而 `WakingCheckStage` 会改 `session_id`（`waking_check/stage.py:88-92`）。
> **检测方式**：`event.get_extra("_session_isolated")` 为真即说明改写过。

### 优先级链

```
1. 显式标签：正则命中 issue #123 / PR #456 / [T-xxxx] → f"{base}|tag:{命中值}"
2. 回复链  ：消息里有 Reply 段 → f"{base}|reply:{被回复消息的 sender_id}:{被回复消息 id}"
3. 话题    ：平台若有话题 id → f"{base}|topic:{id}"
4. 兜底    ：f"{base}|day:{本地日期}"
```

```python
def thread_key(event, conf) -> str:
    base = f"{event.get_platform_name()}:{event.get_group_id() or 'private'}"
    if (tag := _explicit_tag(event.message_str)):
        return f"{base}|tag:{tag}"
    if (rid := _reply_target(event)):
        return f"{base}|reply:{rid}"
    if (topic := _platform_topic(event)):
        return f"{base}|topic:{topic}"
    return f"{base}|day:{_local_date()}"
```

### 幂等键（照参考实现：内容哈希，父 id 进子 id）

```python
def key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]

trigger_id = key("trigger", source, thread_key, platform_msg_id)   # 入库去重
ticket_id  = key("ticket",  eval_id)                                # 一个评估只一个工单
```

> 参考实现用同一手法一行买到「一个 Eval 只产生一个 Ticket」（`../调查/04-reference-model.md` §3）。
> **约束**：参与计算的字段必须稳定——不能用时间戳、不能用自增序号。

---

## 3. `store.py` —— 落盘契约（SQLite，不是 JSON）

### 3.1 放哪儿：`data/plugin_data/<plugin_name>/state.db`

**绝对不能放插件目录里。** 依据（流 B 报告）：

| 事实 | 证据 |
|---|---|
| 插件更新会 **整目录重放**：`remove_dir(target_plugin_path)` | `star_manager.py:2115-2119` |
| 卸载默认 `delete_config=False` / `delete_data=False`，`plugin_data/` 只在 `delete_data=True` 时才删 | `star_manager.py:1557-1587,1675-1680` |
| AstrBot 升级只解压源码，不动 `data/` | `updater.py:313,482-496`（推断） |
| **`plugin_data` 在备份白名单里**；而你自建在主库的表**不在**（`MAIN_DB_MODELS` 仅 13 张表） | `backup/constants.py:43-57,75-83`；`exporter.py:206-213` |
| StorageCleaner 只清 logs / temp / plugins.json，不碰 `plugin_data` | `storage_cleaner.py:154-171` |

路径拿法：

```python
from astrbot.core.star.star_tools import StarTools
data_dir = StarTools.get_data_dir()      # → data/plugin_data/{plugin_name}（star_tools.py:285-287）
```

也可以用常量 `get_astrbot_plugin_data_path()`（`utils/astrbot_path.py:60-62`）—— 注意**没有** `utils/paths` 这个模块。

### 3.2 为什么用 SQLite 而不是 JSON

- `aiosqlite` 是 AstrBot 硬依赖（`requirements.txt:4`），不用额外装
- 抄它的连接参数，**WAL + busy_timeout 白拿**：`db/__init__.py:40-52`、`db/sqlite.py:87-89`
- `triggers` / `dispositions` 是**追加型**数据，SQLite 天然合适
- JSON 方案要么每写一次全量重写，要么自己实现文件锁；而**框架的原子写实现全是私有函数，没有公开 helper**（`config/astrbot_config.py:331-344`、`utils/media_utils.py:1186-1219`）

### 3.3 表设计（v1）

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS trigger (
  id          TEXT PRIMARY KEY,        -- key("trigger", ...)，天然幂等
  source      TEXT NOT NULL,
  thread_key  TEXT NOT NULL,
  umo         TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  text        TEXT NOT NULL,
  ts          REAL NOT NULL,
  raw_ref     TEXT
);

CREATE TABLE IF NOT EXISTS thread (
  key         TEXT PRIMARY KEY,
  evaluating  TEXT,                    -- 正在跑的 eval id（单槽）
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_pending (   -- 只追加
  thread_key  TEXT NOT NULL,
  trigger_id  TEXT NOT NULL,
  PRIMARY KEY (thread_key, trigger_id)
);

CREATE TABLE IF NOT EXISTS ticket (
  id          TEXT PRIMARY KEY,        -- key("ticket", eval_id)
  thread_key  TEXT NOT NULL,
  mandate_json TEXT NOT NULL,          -- 不可变
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_fact (      -- 只追加：状态由事实算出
  ticket_id   TEXT NOT NULL,
  kind        TEXT NOT NULL,           -- accepted/worker_failed/blocked/delivered/human_stopped
  payload     TEXT,
  ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS disposition (      -- 审计：谁被丢弃/放行/路由，为什么
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id  TEXT,
  verdict     TEXT NOT NULL,
  reason      TEXT NOT NULL,
  ts          REAL NOT NULL
);
```

### 3.4 并发

- **进程内**：单写者队列（抄 `SharedPreferences` 的 FIFO + RLock 思路，`shared_preferences.py:53-60,95-180`）
- **跨进程**：`filelock` 是硬依赖（`requirements.txt:17`），直接包一层
- **会话级互斥**：`utils/session_lock.py:33-55` 有现成的 `session_lock_manager`
- ⚠ 框架自身有弱点：`add_message_pair` 是裸 read-modify-write（`conversation_mgr.py:373-390`），KV 是 select-then-update 无 CAS（`db/sqlite.py:1501-1523`）→ **不丢更新的责任在插件**

### 3.5 迁移：必须自带 `schema_version` 表

**AstrBot 没有 alembic、没有 schema_version 表，也没有插件级迁移机制。** 它自己靠 `SQLModel.metadata.create_all` + `PRAGMA table_info` → `ALTER TABLE` + 一次性 preference flag（`db/sqlite.py:86,127-145`；`db/migration/helper.py:27-34`）。

```python
async def migrate(conn):
    v = await _read_version(conn)          # 缺表/缺行 → 0
    for step in MIGRATIONS:                # MIGRATIONS[v] -> SQL/函数
        if v < step.to_version:
            await step.apply(conn)
            v = step.to_version
            await _write_version(conn, v)
```

**第一版就要有这张表**，哪怕 `MIGRATIONS` 为空。插件会被 WebUI 一键更新，旧库一定存在过。

### 3.6 KV 只放小东西

`PluginKVStoreMixin`（`utils/plugin_kv_store.py:14-28`）的限制要记住：

- 键空间 `scope="plugin"`、`scope_id = plugin_id = "author/name"` 小写派生（`star/star.py:78-82`）
- **改插件 author/name 会让 KV 变孤儿**
- 无 TTL、只存 JSON、无范围查询
- **无法区分"存了 None"与"未存"**（`shared_preferences.py:452,481`）

→ 只放单条配置/指针；列表与条件查询一律进 SQLite。

---

## 4. 单测清单（P0 的全部验收）

| 测试 | 断言 |
|---|---|
| `test_self_message_dropped` | `sender_id == self_id` → `IGNORE` |
| `test_cron_sender_dropped` | `sender_id == "astrbot"` → `IGNORE`（合成事件防回环） |
| `test_command_passthrough` | `/help` → `PASS` |
| `test_source_whitelist` | 白名单外 → `IGNORE`，且**没有调用任何 LLM**（mock 断言） |
| `test_rate_limit` | 第 N+1 条 → `IGNORE`，`disposition` 多一行 |
| `test_thread_key_reply` | 回复同一条消息两次 → **同一个 key** |
| `test_thread_key_isolated` | `unique_session=True` 时同群两人 → **同一个 key**（不能按人分裂） |
| `test_trigger_id_idempotent` | 同 `(source, thread_key, msg_id)` → 同一个 id；重复入库只留一行 |
| `test_ticket_id_from_eval` | 同一 eval 重跑多次 → 同一个 ticket_id |
| `test_state_from_facts` | 加一条 `human_stopped` 事实后，`ticket_state` 立刻变 `stopped` |
| `test_migrate_noop` | 空库 → 自动建到当前版本 |
| `test_migrate_from_zero` | 无 `schema_version` 表的旧库 → 补齐到当前版本 |

**P0 完成的定义**：以上全绿；插件装进 `data/plugins/` 能加载；日志里能看到每条消息的 `GateDecision.reason`；**主 agent 不被无意触发**（普通闲聊不进模型）；`/stop` 能停。

---

## 5. 明确不在 P0 范围

Router 循环（P3）、Thread 订阅直通（P4）、Worker（P5）、沙箱（P5 之后）、任何提示词。

---

## 6. 三个实现细节

1. **插件内用相对导入**（`from .core.gate import ...`）。插件模块名是 `data.plugins.<name>.main`（`api/__init__.py:19` 的 `_PLUGIN_MODULE_ROOTS`），依赖安装目录名，不可硬编码。
2. **`terminate()` 必须直接定义在插件类里**。卸载逻辑检查 `"terminate" in star_cls_type.__dict__`（`star_manager.py:1962-1963`）——继承来的不会被调用，循环就停不下来。
3. **`terminate()` 里先 `await sp.flush()` 再关库**。插件更新前只给这一次回调（`star_manager.py:2107`），漏了就丢最后一批写入。

---

## 7. 实现状态（2026-11-01）

P0 三块已落地并通过单测（commit `7418ea8`）。

| 模块 | 文件 | 状态 | 单测 |
|---|---|---|---|
| Gate（四条规则） | `core/gate.py` | ✅ | 22 |
| Thread 指纹 / 幂等键 | `core/thread_key.py` | ✅ | 25 |
| 状态库（SQLite + 迁移链） | `core/store.py` | ✅ | 26 |
| 阈值集中 | `core/limits.py` | ✅ | — |
| **合计** | | | **73 passed / 0.98s** |

实现时相对本规格的**两处调整**（不改变语义，只让测试更容易）：

1. **`EventView` 代替 `AstrMessageEvent`**：`gate.decide()` 与 `thread_key.thread_key()` 吃的是一个 frozen dataclass（`core/gate.py::EventView`），由 `main.py` 的适配层从真实事件抽取。于是这三个模块**完全不 import astrbot**，在任何 Python 3.10+ 环境都能测。
2. **写操作串行化用 `asyncio.Lock` + 单连接**，而不是复刻 `SharedPreferences` 的 FIFO 队列——单实例部署下语义等价，代码少一半。跨进程仍可加 `filelock`。

尚未验证的验收项（需要 AstrBot 在场，即远程服务器）：

- ~~装进 `data/plugins/` 能加载~~ **已验**（本地实例加载成功）
- ~~日志里能看到每条消息的 `GateDecision.reason`~~ **已验**
- ~~普通闲聊不触发主 agent~~ **已验**

### 7.1 后续三块（已完成）

| 模块 | 文件 | 验证 |
|---|---|---|
| Outbox（可靠投递） | `superbot/outbox.py` | 13 单测 + e2e 实发 |
| Watchdog（停滞检测） | `superbot/watchdog.py` | 12 单测 |
| Router（单槽 + 只追加 + 批处理） | `superbot/router.py` | 14 单测 + e2e batch=3 |
| Worker（并发上限 + 可取消） | `superbot/worker.py` | 14 单测 + e2e worker done |

**e2e 全链路**：Gate → Router（批处理）→ Worker（执行）→ Outbox（投递）→ `send_message`
—— 12 项断言全绿，见 `docs/测试环境.md`。

### 7.2 仍未做

- `P5`：把桩评估器换成真模型（`Context.tool_loop_agent`）、Worker 接沙箱
- `P4`：Thread 订阅直通（事件绕过 Router 直达 Worker）
- 取消链路在真实例里的验证（单测已覆盖 `task.cancel` 语义）
