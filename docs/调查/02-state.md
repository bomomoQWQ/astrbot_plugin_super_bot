# 02 · 插件可用的持久化原语

> 基线：AstrBot **v4.28.1**（`ab42c0d9b`，Python 3.13）。路径相对 `F:\理解痞老板\AstrBot`。
> 规则：每条结论附源码证据；无证据的推断显式标注「推断」。只读源码，未修改仓库。

---

## 0. 一页速查

| 原语 | 入口 | 底层 | 键空间隔离 | 事务/查询 | 卸载后 |
|---|---|---|---|---|---|
| 插件 KV | `self.put_kv_data/get_kv_data/delete_kv_data` (`astrbot/core/utils/plugin_kv_store.py:14-28`) | 主库 `preferences` 表，`scope="plugin"` | `scope_id = plugin_id = "author/name"` | ❌ 只能整键取/存 | 默认**保留**，`delete_data=True` 时清 |
| 主库会话 | `self.context.get_db()`（返回 `BaseDatabase`，`astrbot/core/star/context.py:777-783`） | SQLite `data/data_v4.db` | 全局共享（无隔离） | ✅ ORM/SQL | 保留 |
| 主库裸 session | `db.get_db()`（async contextmanager，`astrbot/core/db/__init__.py:62-69`） | 同一连接池 | 全局共享 | ✅ 任意 SQL | 保留（但**不进备份**） |
| 共享偏好 | `astrbot.core.sp` / `from astrbot.api import sp` | 同一个 `preferences` 表 | `global` / `umo` / `plugin` 三个 scope | ❌ | 保留 |
| 插件数据目录 | `StarTools.get_data_dir()` / `get_astrbot_plugin_data_path()` | 文件系统 | 每插件一目录 | 自建（SQLite/JSON） | 默认保留 |
| 插件配置 | `_conf_schema.json` → `AstrBotConfig` | `data/config/{dir}_config.json` | 每插件一文件 | — | 默认保留，`delete_config=True` 时删 |
| Cron 任务 | `context.cron_manager` (`astrbot/core/star/context.py:167`) | `cron_jobs` 表 | 全局共享 | ✅ | 保留 |

**结论先行**：AstrBot 给插件的"稳定持久层"只有两种形态 —— (a) 主库里的 KV 表，(b) 自己管一个 `data/plugin_data/<plugin>/` 目录。没有插件级 schema 版本机制、没有插件级数据库命名空间、没有公开的原子写工具。

---

## 1. 持久化原语逐个

### 1.1 `PluginKVStoreMixin`：有内建 KV，但只是一个三行包装

`Star` 继承了它（`astrbot/core/star/base.py:9,19`），实现在 `astrbot/core/utils/plugin_kv_store.py`，全文只有 28 行：

```python
async def put_kv_data(self, key, value):
    await sp.put_async("plugin", self.plugin_id, key, value)   # :20
async def get_kv_data(self, key, default):
    return await sp.get_async("plugin", self.plugin_id, key, default)  # :24
```

**键空间隔离**：`scope="plugin"` + `scope_id=self.plugin_id`，`key` 由插件自定。
- `plugin_id` 是**派生值**，不是目录名：`f"{author}/{name}"` 全小写、`/`→`_`（`astrbot/core/star/star.py:78-82`）。
- 加载时由 star_manager 注入到类与实例上（`astrbot/core/star/star_manager.py:1221` 和 `:1243`），所以**只有被 star_manager 正常加载的实例**才有这个属性；直接实例化（单元测试）会 `AttributeError`（`astrbot/core/utils/plugin_kv_store.py:12` 只有类型注解，无默认值）。
- 隔离的实际边界 = `(author, name)` 二元组。**改 `author` 或 `name` → 读到空 KV（旧数据成为孤儿，无人回收）**；两个 author+name 相同的插件会互相覆盖。若 `_conf_schema.json` 或 metadata 里没写 author，`author` 变成 `"unknown"`（`astrbot/core/star/star.py:81`）。

**TTL**：未找到。`preferences` 模型只有 `scope/scope_id/key/value` + `created_at/updated_at`（`astrbot/core/db/po.py:212-236`），没有过期时间字段。`SharedPreferences.temporary_cache` 有 24h 清空（`astrbot/core/utils/shared_preferences.py:38-39, 65-72`），但那是**纯内存 dict**，进程重启即失，不是持久层。

**类型限制**：`put_kv_data` 的注解允许 `bytes`（`astrbot/core/utils/plugin_kv_store.py:5`，`SUPPORTED_VALUE_TYPES = int | float | str | bytes | bool | dict | list | None`），但值最终写进 `value: dict = Field(sa_type=JSON)`（`astrbot/core/db/po.py:227`），写入前再包一层 `{"val": value}`（`astrbot/core/utils/shared_preferences.py:153-158`）。
- 可存：JSON 能表达的标量/嵌套 dict/list（`None` 靠 `{"val": null}` 表达）。
- **推断**：`bytes` / `datetime` / 自定义对象在 SQLAlchemy 的 JSON 序列化阶段会抛 `TypeError`；即注解骗了你，实际限制是"JSON 可序列化"。同理，`get_kv_data(key, default)` 若存入的是 `None`，`get` 侧有 `default if value is None else` 的兜底（`astrbot/core/utils/shared_preferences.py:452, 481`），**无法区分"存了 None"与"没存"**。

**性能**：读路径先查进程内 overlay，miss 则走一次点查询（`astrbot/core/utils/shared_preferences.py:278-285`）；表刻意**不在启动时全量预载**（同文件 `:44-52` 注释明确说明怕 OOM）。所以 KV 适合"小、少、按需读"的状态，不适合当表用。

### 1.2 `Context.get_db()` 与 `BaseDatabase`

```python
def get_db(self) -> BaseDatabase:   # astrbot/core/star/context.py:777-783
    return self._db
```

拿到的是 `BaseDatabase`（`astrbot/core/db/__init__.py:34`），一个 `@dataclass` + `abc.ABC`。它同时给你两样东西：

1. **面向上层业务的方法**（约 100 个抽象方法，`astrbot/core/db/__init__.py:91-975`）：会话、消息历史、人格、偏好、cron、attachment、api key、webchat thread……
2. **直接拿 AsyncSession 的口子**（注意：名字撞车但语义不同）：

```python
@asynccontextmanager
async def get_db(self) -> T.AsyncGenerator[AsyncSession, None]:   # :62-69
    if not self.inited:
        await self.initialize(); self.inited = True
    async with self.AsyncSessionLocal() as session:
        yield session
```

用法在内置插件里有现成先例 —— `astrbot/builtin_stars/builtin_commands/commands/conversation.py:185-206`：

```python
db = self.context.get_db()
async with db.get_db() as session:
    result = await session.execute(select(func.count(...)).where(...))
```

即 **插件可以在这个连接池上跑任意 SQLAlchemy 查询，甚至自己建表**。但这条路有三个明确缺口：
- **没有命名空间**：你和 AstrBot 共用 `data/data_v4.db`，表名冲突风险自负。
- **不进备份**：AstrBot 的备份导出只遍历 `MAIN_DB_MODELS` 白名单（`astrbot/core/backup/constants.py:43-57` + `astrbot/core/backup/exporter.py:206-213`），你自建的表**不在里面**。
- **建表时机**：`SQLModel.metadata.create_all` 在 `db.initialize()` 时执行一次（`astrbot/core/db/sqlite.py:85-86`），而 `db.initialize()` 在插件加载之前（`astrbot/core/core_lifecycle.py:173`）。**推断**：插件若用 `SQLModel, table=True` 定义自己的表，必须自己在 `initialize()` 里再调一次 `create_all` 或手写 `CREATE TABLE IF NOT EXISTS`，AstrBot 不会替你建。

### 1.3 `SharedPreferences`（`sp`）—— KV 的下半身，插件也可直接用

`astrbot/core/utils/shared_preferences.py`，全局单例在 `astrbot/core/__init__.py:41`，并被 `astrbot/api/__init__.py:4,82` 导出为 `from astrbot.api import sp`。

接口全是 async（`get_async/put_async/remove_async/clear_async/range_get_async/flush/initialize`，`:267-422`），另有三组语义糖：
- `session_get/session_put/...` → `scope="umo"`，`scope_id=UMO`（`:333-407`）
- `global_get/global_put/...` → `scope="global"`, `scope_id="global"`（`:353-364`）
- `range_get_async(scope, scope_id=None, key=None)` 返回 `list[Preference]`（`:287-299`）——这是**唯一能做"范围枚举"**的接口，插件可用它按 `scope="plugin"` 遍历自己全部 KV。

同步 API（`get/put/range_get/...`）全部被 `@deprecated(version="4.0.0")` 标记，且**同步读在部分后端直接不可用**：拿不到 `get_preference_sync` 就只返回 default 并打 warning（`:457-466`）。新代码只用 async。

⚠️ 一个硬约束：`sp` 的生命周期绑定到某个 event loop（`initialize()` 里 `self._loop`，`:191-233`），`flush()` 在**非拥有 loop** 上调用会 `RuntimeError`（`:247-253`）。所以别在子线程/子 loop 里写 `sp`。

### 1.4 「数据放 `data/` 目录」到底指哪个常量

不存在 `astrbot/core/utils/paths` —— **实际文件是 `astrbot/core/utils/astrbot_path.py`**（检索范围：`astrbot/core/utils/` 全目录 + 全仓 grep `get_astrbot_data_path`）。

| 常量 | 值 | 证据 |
|---|---|---|
| `get_astrbot_root()` | `$ASTRBOT_ROOT` 或 `cwd`；桌面打包版固定 `~/.astrbot` | `astrbot/core/utils/astrbot_path.py:29-35` |
| `get_astrbot_data_path()` | `<root>/data` | `:38-40` |
| `get_astrbot_plugin_data_path()` | `<root>/data/plugin_data`（**所有插件数据的父目录**） | `:60-62` |
| `get_astrbot_plugin_path()` | `<root>/data/plugins`（插件**代码**目录） | `:48-50` |
| `get_astrbot_config_path()` | `<root>/data/config` | `:43-45` |
| `get_astrbot_temp_path()` | `<root>/data/temp`（会被 `TempDirCleaner` 按配额清理，`astrbot/core/utils/temp_dir_cleaner.py:34-45`） | `:75-77` |
| `get_astrbot_backups_path()` | `<root>/data/backups` | `:105-107` |

单插件级 API（官方推荐）：

```python
StarTools.get_data_dir()   # astrbot/core/star/star_tools.py:243-298
# → Path(os.path.join(get_astrbot_data_path(), "plugin_data", plugin_name))  :285-287
```

自动从调用栈反查插件名（`:263-283`），并 `ensure_dir`。文档口径一致：`docs/zh/dev/star/guides/storage.md:23-33`（大文件放 `data/plugin_data/{plugin_name}/`）。

⚠️ **路径基准不一致的坑**：`get_data_dir` 用 **`metadata.name`**（插件名，`:280`），而卸载清理用 **`root_dir_name`**（插件目录名，`astrbot/core/star/star_manager.py:1558-1564`）。两者在常规安装下相同，但若目录名与插件名不同，`delete_data=True` **删不到**你自己建的目录 —— 对我们是好事（数据更安全），但别指望它自动清干净。

### 1.5 插件配置（`_conf_schema.json`）也是一种持久化

有 `_conf_schema.json` 时，star_manager 会实例化 `AstrBotConfig`，落到 `data/config/{root_dir_name}_config.json`（`astrbot/core/star/star_manager.py:1164-1170`），加载时自动做完整性补齐 + 配置迁移（`astrbot/core/config/astrbot_config.py:89-94`）。

边界很清楚：**它是"用户可编辑的配置"，不是"运行时状态"**。适合放开关、阈值、默认模型；不适合放 Thread/Ticket 这种高频变更数据（每次 `save_config` 都整文件写，见 §5）。

### 1.6 顺带一个：CronJob 表也向插件开放

`context.cron_manager`（`astrbot/core/star/context.py:167`）背后是持久化的 `cron_jobs` 表，带 `job_type`（`basic | active_agent`）、`payload`(JSON)、`cron_expression`、`timezone`、`persistent`、`run_once`、`status`、`last_error` 等字段（`astrbot/core/db/po.py:181-209`）。对我们的"常驻 Router + 定时唤醒 Worker"是有直接价值的现成持久化调度表，不必自造。

---

## 2. 会话历史：存在哪、怎么读写、能不能乱碰

### 2.1 存储结构

现代存储是 `conversations` 表（`ConversationV2`），**不是** `Conversation` 那个 dataclass：

```python
class ConversationV2(TimestampMixin, SQLModel, table=True):
    __tablename__ = "conversations"
    conversation_id: str   # uuid, unique          # po.py:75-80
    platform_id: str                               # :81
    user_id: str                                   # :82  ← 这里装的是 UMO
    content: list | None = Field(default=None, sa_type=JSON)   # :83
    title / persona_id / token_usage               # :85-91
```

- `content` 是 **OpenAI 格式的 `list[dict]`**（`astrbot/core/db/po.py:88-89` 注释明说），每个元素形如 `{"role": "user"|"assistant", "content": ..., "tool_calls": ...}`。
- `user_id` 字段装的是 **UMO**（`platform:message_type:session_id`）：`create_conversation(user_id=unified_msg_origin, ...)`（`astrbot/core/conversation_mgr.py:115-121`）。
- `astrbot/core/db/po.py:557-578` 的 `Conversation` 是**遗留只读视图**：`history` 是 JSON **字符串**，由 `_convert_conv_from_v2_to_v1` 现做：`history=json.dumps(conv_v2.content or [])`（`astrbot/core/conversation_mgr.py:80-90`）。别写它。
- 索引：`created_at DESC, inner_conversation_id DESC` 和 `(platform_id, created_at DESC, inner_id DESC)`（`astrbot/core/db/po.py:93-109`）。**`user_id` 上没有索引** —— 推断：按 UMO 全表扫，会话量大时 `get_conversations(umo)` 会慢。

### 2.2 读写 API（两套，都能用）

| 动作 | ConversationManager | BaseDatabase（更底层） |
|---|---|---|
| 按 UMO 列全部会话 | `get_conversations(umo, platform_id=None)` (`conversation_mgr.py:216-238`) | `get_conversations(user_id, platform_id)` (`db/__init__.py:128-137`) |
| 按 cid 取 | `get_conversation(umo, cid, create_if_not_exists)` (`:190-214`) | `get_conversation_by_id(cid)` (`:140-142`) |
| 新建 | `new_conversation(umo, ...)` (`:92-124`) | `create_conversation(...)` (`:185-197`) |
| 改历史/标题/persona/token | `update_conversation(umo, cid, history=..., ...)` (`:279-307`) | `update_conversation(cid, ...)` (`:200-211`) |
| 追加一问一答 | `add_message_pair(cid, user_msg, assistant_msg)` (`:357-390`) | — |
| 删 | `delete_conversation(umo, cid)` (`:139-158`) | `delete_conversation(cid)` (`:212-215`) |
| 分页/检索 | `get_filtered_conversations(...)` (`:240-277`) | `get_filtered_conversations(...)` (`:154-173`) |

`ConversationManager` 是**公开属性**挂在 Context 上：`self.conversation_manager = conversation_manager`（`astrbot/core/star/context.py:157`），内置插件正是这么用的（`astrbot/builtin_stars/astrbot/main.py:79-93`、`builtin_commands/commands/conversation.py:97-158`）。

### 2.3 插件能不能读写"任意会话"的历史？

**能，完全没有权限检查，也没有 owner 校验。**

- 读：`db.get_conversation_by_id(cid)` 只按 cid 查（`astrbot/core/db/__init__.py:140-142`）；`get_conversations(user_id=None)` 返回**所有用户**的会话（`:128-137`，`user_id` 为可选过滤）。`get_all_conversations(page, page_size)` 直接全量分页（`:145-151`）。
- 写：`ConversationManager.update_conversation` 拿到 `cid` 就直接调 `db.update_conversation(cid=conversation_id, ...)`（`conversation_mgr.py:300-307`），**`unified_msg_origin` 参数只用于"没给 cid 时取当前"**，不参与鉴权。
- 结论：Router 会话想读 Worker 会话的上下文、或者插件想给某个会话"塞"一条 assistant 消息，源码层面没有任何阻碍。**"隔离"只能靠我们自己的 key 约定（例如 worker 会话用固定 UMO 前缀 + 前缀校验）来保证，框架不给兜底。**

⚠️ 一个语义提醒：`content` 是**整条历史的全量覆盖写**。`add_message_pair` 是 read-modify-write（`conversation_mgr.py:373-390`），无行锁、无乐观锁 → 两个 asyncio 任务同时 append 会丢更新（详见 §5）。

---

## 3. 自造结构化状态（Thread / Ticket / Mandate）该落哪

### 3.1 三个候选，按"稳"排序

**A. 自建 SQLite 文件，放 `data/plugin_data/<plugin>/state.db`（推荐）**
- 路径用 `StarTools.get_data_dir()`（`astrbot/core/star/star_tools.py:285-287`）+ 一个 `state.db`。
- 驱动现成：`aiosqlite>=0.21.0` 是 AstrBot 硬依赖（`requirements.txt:4`），不用装。
- 好处：表结构完全自主；能建联合索引/唯一约束做幂等；能用 `BEGIN IMMEDIATE` 做写串行；**卸载默认保留**（`star_manager.py:1557-1576` 只有 `delete_data=True` 才删）；**插件更新不影响它**（见 §3.2）；**进 AstrBot 备份**（`astrbot/core/backup/constants.py:75-83` 的 `plugin_data` 项）。
- 代价：多一个连接池要自己管 PRAGMA（`journal_mode=WAL`、`busy_timeout`）；与主库不共享事务（跨库一致性要自己做）。

**B. 主库 `data/data_v4.db` 里加自己的表**（`context.get_db().get_db()`）
- 好处：与 AstrBot 共享 WAL + 30s busy_timeout（`astrbot/core/db/sqlite.py:87-89`），能在同一事务里读 AstrBot 的表。
- 坏处（都是硬的）：① **不在备份白名单里**（`astrbot/core/backup/constants.py:43-57` 只列了 13 张表），用户备份/迁移会静默丢你的表；② `create_all` 不会替你建表（§1.2）；③ 表名污染主库命名空间。

**C. KV（`put_kv_data`）存 JSON blob**
- 只适合"少量单条状态"，例如 `router_state`、`worker_registry`。
- 致命局限：**没有范围查询、没有条件更新、没有事务**。要列出"所有 open 的 Ticket"，只能自己维护一个 index key，然后 `range_get_async("plugin", plugin_id)` 全量拉回内存（`astrbot/core/utils/shared_preferences.py:287-299`）再过滤。
- 每次 `put` 都是"整值覆盖"（`insert_preference_or_update` 是先 select 再 update，`astrbot/core/db/sqlite.py:1501-1523`）→ **两个任务同时改同一个 key 必然丢更新**。

**明确不推荐**：把状态写进插件自己的代码目录 `data/plugins/<dir>/`。更新插件时目录被整棵删掉重放（`astrbot/core/star/star_manager.py:2115-2119`：`copytree` 备份 → `remove_dir(target_plugin_path)` → `move`），任何放里面的数据都会丢。

### 3.2 生命周期矩阵：什么数据在什么操作后还在

| 我们的数据位置 | 卸载（默认） | 卸载 `delete_data=True` | 卸载 `delete_config=True` | 插件更新 | AstrBot 升级 |
|---|---|---|---|---|---|
| `data/plugin_data/<name>/state.db` | ✅ 保留 | ❌ 删（按 `root_dir_name` 匹配，名字一致时） | ✅ | ✅ 保留 | ✅ 保留 |
| 插件 KV（`preferences`, scope=`plugin`, scope_id=`plugin_id`） | ✅ 保留 | ❌ `sp.clear_async("plugin", plugin_id)` | ✅ | ✅ 保留 | ✅ 保留 |
| `data/config/{dir}_config.json`（插件配置） | ✅ 保留 | ✅ 保留 | ❌ 删 | ✅ 保留 | ✅ 保留 |
| `data/plugins/<dir>/` 内任何文件 | ❌ 删（`star_manager.py:1719`） | ❌ 删 | ❌ 删 | ❌ **删**（`:2118`） | ❌ 删/覆盖 |
| 主库自建表 | ✅ 保留 | ✅ 保留 | ✅ 保留 | ✅ 保留 | ✅ 保留（但不在备份里） |
| `conversations` / `preferences(global,umo)` | ✅ | ✅ | ✅ | ✅ | ✅ |

证据：
- 卸载主流程：`_terminate_plugin` → `_unbind_plugin` → `remove_dir(plugin_path)` → `_cleanup_plugin_optional_artifacts(delete_config=..., delete_data=...)`（`astrbot/core/star/star_manager.py:1702-1733`）。
- `delete_data` 分支：删 `plugin_data/<root_dir_name>` 与 `plugins_data/<root_dir_name>`，再清 KV（`:1557-1587`）。两个 `data_dir_name` 都试（`"plugin_data", "plugins_data"`，`:1559`），说明框架兼容过旧目录名。
- 两个开关默认都是 `False`（`uninstall_plugin(..., delete_config: bool = False, delete_data: bool = False)`，`:1675-1680`）→ 默认"卸载不清数据"。
- 更新插件：`_cleanup_plugin_state(dir_name)` 只清 `sys.modules` / `star_map` / handlers / tools / adapters，**不碰磁盘数据**（`:773-816`）；随后才 `remove_dir(target_plugin_path)`（`:2118`）。
- AstrBot 升级：只把 release zip 解压到源码根 `self._main_path`（`astrbot/core/updater.py:313, 482-496`；`astrbot/core/zip_updater.py:313-321` 仅 `extractall`），**没有任何删除 `data/` 的动作** → **推断**：AstrBot 升级不影响 `data/` 下任何内容（唯一例外见 `astrbot/core/updater.py:340-353`，那里只动 `data/dist` 这个前端产物目录）。
- `StorageCleaner` 只清 `data/logs`、`data/temp`、`data/plugins.json`、`data/plugins_custom_*.json`、`data/sandbox_skills_cache.json`（`astrbot/core/utils/storage_cleaner.py:154-171`）→ **不碰 `plugin_data`**。

---

## 4. Schema 版本 / 数据迁移：框架有，但只覆盖它自己

### 4.1 数据库：没有迁移框架，只有 `create_all` + 手写 `ALTER TABLE`

```python
async def initialize(self) -> None:                      # astrbot/core/db/sqlite.py:83
    async with self.engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)  # :86  建缺失的表
        await conn.execute(text("PRAGMA journal_mode=WAL")) # :87
        ...
        await self._ensure_persona_folder_columns(conn)     # :95
        await self._ensure_persona_skills_column(conn)      # :96
        await self._ensure_persona_custom_error_message_column(conn)  # :97
        await self._ensure_platform_message_history_checkpoint_column(conn)  # :98
        await self._ensure_chatui_project_workspace_columns(conn)            # :99
        await self._ensure_conversation_indexes(conn)       # :100
```

- **加表**靠 `SQLModel.metadata.create_all`（幂等）。
- **加列**靠每列一个 `_ensure_*_column`：`PRAGMA table_info(x)` 取现有列 → 缺了就 `ALTER TABLE ... ADD COLUMN`（`astrbot/core/db/sqlite.py:127-145` 是 `folder_id` / `sort_order` 的范例，注释明说"为了支持旧版数据库的平滑升级"）。
- **加索引**靠 `CREATE INDEX IF NOT EXISTS`（`:106-125`）。
- **没有** `alembic`、**没有** `schema_version` 表、**没有**版本号 → 全局检索 `astrbot/core/db/` 全目录 + grep `alembic|schema_version|create_all`，未找到通用迁移器。**加列是"代码里逐个补"，加表是"idempotent create"，仅此而已。**

### 4.2 数据迁移：用"preference 打标记"的幂等开关

一次性迁移的模式非常统一 —— **拿一个 preference key 当 migration flag**：

| 迁移 | flag key | 证据 |
|---|---|---|
| v3→v4 全库 | `migration_done_v4`（`global/global`） | `astrbot/core/db/migration/helper.py:27-34`（查）、`:67`（写） |
| token_usage 列回填 | `migration_done_token_usage_1` | `astrbot/core/db/migration/migra_token_usage.py:21-24, 41, 56` |
| 插件工具失活状态 | `inactivated_llm_tools_plugin_state_migrated_v1` | `astrbot/core/star/star_manager.py:102, 961-962` |

前置判断也常看"旧文件是否存在"：`check_migration_needed_v4` 先查 `data/data_v3.db` 在不在（`astrbot/core/db/migration/helper.py:22-26`）。

### 4.3 配置：有显式版本号

- 默认配置里带 `"config_version": 3`（`astrbot/core/config/default.py:66`）。
- 迁移函数读版本决定迁移分支（`astrbot/core/utils/migra_helper.py:166-167`，`legacy_version = ... or config_version < 3`），末尾**无条件把版本刷到 3**（`:286-288`）。
- 触发点：`AstrBotConfig.__init__` 里 `migrate_config_on_load(conf, Path(config_path))`（`astrbot/core/config/astrbot_config.py:89-94`），迁移改了东西就 `save_config()`（`:108-109`）。
- 注意 `check_config_integrity` 负责"补上新增的默认键"（`:93`）—— 这是插件配置文件加字段无需迁移的根因。

### 4.4 启动编排

```python
await self.db.initialize()      # astrbot/core/core_lifecycle.py:173
...
await migra(self.db, self.astrbot_config_mgr, self.umop_config_router, self.astrbot_config_mgr)  # :197-204
```

`migra()` 内部逐个迁移独立 `try/except`，**任一迁移失败只记 error 不阻断启动**（`astrbot/core/utils/migra_helper.py:444-475`）。

### 4.5 插件数据呢？

**未找到**任何"插件数据 schema 版本"机制。检索范围：`astrbot/core/star/`（star_manager 全文）、`astrbot/core/db/`、`docs/zh/dev/star/` 全目录。框架从不读写 `data/plugin_data/*` 的内部结构，插件自己负责版本化。**推断**：官方立场是"插件数据目录是插件私有领域，框架不介入"，所以版本自证必须我们自己做（见 §6 建议）。

---

## 5. 并发安全：框架怎么防，能复用什么

### 5.1 SQLite 层：WAL + 30s busy_timeout（现成的，白拿）

```python
connect_args = {"timeout": 30} if is_sqlite else {}      # astrbot/core/db/__init__.py:45-52
self.engine = create_async_engine(self.DATABASE_URL, ..., connect_args=connect_args)
```
```python
await conn.execute(text("PRAGMA journal_mode=WAL"))       # astrbot/core/db/sqlite.py:87
await conn.execute(text("PRAGMA busy_timeout=30000"))     # :88
await conn.execute(text("PRAGMA synchronous=NORMAL"))     # :89
```
注释直接点名场景（`astrbot/core/db/__init__.py:40-44`）："SQLite only supports a single writer at a time... enough to ride out brief write bursts from concurrent agent/metrics/session operations"。
→ **对我们自建的 `state.db`，抄这三个 PRAGMA 即可获得同等行为**。

### 5.2 应用层：单写者 FIFO 队列（`SharedPreferences` 的答案）

`SharedPreferences` 的并发模型值得直接抄，它解决的就是"多个 asyncio 任务同时写"：

1. **进程内 overlay + 锁**：`self._cache` + `threading.RLock()`（`astrbot/core/utils/shared_preferences.py:53-54`），读命中直接返回 deepcopy（`:278-281`）。
2. **写全部排队**：`asyncio.Queue` + 单一 `_writer_task`，FIFO 顺序落库（`:139-180`）。注释明写 "persist queued preference mutations in FIFO order"（`:140`）。
3. **跨线程安全投递**：当前 loop 直接 `enqueue()`，否则 `loop.call_soon_threadsafe`（`:116-127`）。
4. **写完成语义**：`put_async` 建 `asyncio.Future` 塞进队列项，`await completion`（`:366-379`）→ **返回即已落库**（不是 fire-and-forget）。
5. **批量屏障**：`flush()` = `await queue.join()` + `await writer_task`（`:235-259`），插件 `terminate()` 里可用来确保收尾。

### 5.3 会话级互斥：`SessionLockManager`

`astrbot/core/utils/session_lock.py`，为"同一个会话不许并发处理"提供锁：

```python
class _PerLoopSessionLockManager:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # :12
    @asynccontextmanager
    async def acquire_lock(self, session_id: str):                        # :16-30
```

外层 `SessionLockManager` 按 event loop `WeakKeyDictionary` 隔离（`:33-52`），全局单例 `session_lock_manager`（`:55`）。
⚠️ 它是**进程内、按 session_id 字符串**的锁，不跨进程、不跨 loop。**推断**：对我们的价值是"同一个 Worker 会话不被两条消息同时驱动"，可以直接 `async with session_lock_manager.acquire_lock(worker_umo): ...`。

### 5.4 文件层：有原子写，但**没有公开工具函数**

全仓 grep `os.replace|os.rename|fsync|filelock|atomic`，命中只有三处真实实现，且**都是私有函数**：

| 场景 | 手法 | 证据 |
|---|---|---|
| 配置文件保存 | `tempfile.mkstemp` → `json.dump` → `flush` → `os.fsync` → `os.replace` | `astrbot/core/config/astrbot_config.py:331-344` |
| 图片缓存发布 | `mkstemp` → 写完 → `os.replace`，注释"Concurrent readers never observe a partially written cache file" | `astrbot/core/utils/media_utils.py:1186-1219` |
| CLI 单实例保护 | `filelock.FileLock(lock_file, timeout=5)` | `astrbot/cli/commands/cmd_run.py:8, 64`；`astrbot/cli/commands/cmd_init.py:6, 67` |

- **不存在的**：`astrbot/core/utils/` 里没有任何 `atomic_write_json` / `file_lock` 公共 helper。检索范围：`astrbot/core/utils/` 全目录 46 个文件 + 全仓 grep。→ 要复用只能**照抄模式**（约 10 行），或自己写一个。
- **`filelock` 可以直接用**：它是 AstrBot 硬依赖（`requirements.txt:17`，`pyproject.toml:28`），插件运行时一定能 import，无需声明。

### 5.5 框架自己**没有**做的（我们得自己防）

- **`conversation_mgr.add_message_pair` 是裸 read-modify-write**：`get_conversation_by_id` → `history.append(...)` → `update_conversation(content=history)`（`astrbot/core/conversation_mgr.py:373-390`），全程无锁、无版本号 → 并发 append 丢更新。**推断**：这是 AstrBot 已知的并发弱点，插件不能假设它安全。
- **插件 KV 的 key 级无锁**：并发写同一 key 靠 `insert_preference_or_update` 的 select-then-update（`astrbot/core/db/sqlite.py:1501-1523`），语义是"最后一个写赢"，没有 CAS。
- **`_pm_lock` 只保护插件管理操作**（`astrbot/core/star/star_manager.py:215-216`），不保护数据。

---

## 6. 未找到项与检索范围

| 问题 | 结论 | 检索范围 |
|---|---|---|
| 插件 KV 的 TTL / 过期 | **未找到**。`preferences` 无过期字段（`db/po.py:212-236`）；24h 清理只作用于内存 `temporary_cache`（`shared_preferences.py:65-72`） | `astrbot/core/db/`、`astrbot/core/utils/shared_preferences.py`、`astrbot/core/utils/plugin_kv_store.py` |
| 插件级 schema 版本 / 迁移机制 | **未找到** | `astrbot/core/star/`（含 star_manager 2234 行全文）、`astrbot/core/db/migration/`、`docs/zh/dev/star/` |
| 通用 DB 迁移框架（alembic 类） | **未找到**。只有 `create_all` + 手写 `ALTER TABLE` + 索引 IF NOT EXISTS | `astrbot/core/db/sqlite.py`（2613 行）、全仓 grep `alembic|schema_version` |
| 公开的原子写 / 文件锁工具 | **未找到**（只有私有实现 + `filelock` 三方依赖） | `astrbot/core/utils/` 全目录、全仓 grep `os.replace|fsync|atomic|filelock` |
| `astrbot/core/utils/paths` 模块 | **不存在**；正确文件是 `astrbot/core/utils/astrbot_path.py` | `astrbot/core/utils/` 目录列举 |
| 插件自定义表的官方推荐方式 | **未找到**（文档只推荐 KV + `plugin_data` 目录） | `docs/zh/dev/star/guides/storage.md`、`docs/zh/dev/star/plugin.md` |

---

## 对插件开发的直接含义

1. **主状态库用 `data/plugin_data/<plugin_name>/state.db`（自建 SQLite，`aiosqlite`）**，Thread/Ticket/Mandate 三张表 + 一张 `schema_version` 表自带版本号，别塞进插件目录、也别塞进主库。
2. **`get_kv_data/put_kv_data` 只存"单条小配置"（当前 Router 会话指针、开关、worker 注册表单键），凡是要按条件查询或列表展示的状态一律进 SQLite** —— KV 无范围查询、无事务、无 TTL。
3. **会话历史的读写必须走"我们自己的 UMO 约定 + 前缀校验"**，因为 `context.conversation_manager` / `db.update_conversation(cid, ...)` 对任意 cid 完全不鉴权（`conversation_mgr.py:300-307`），框架不替你隔离 Router 与 Worker。
4. **并发写一律"单写者"**：给 `state.db` 设 `journal_mode=WAL` + `busy_timeout=30000`，进程内再用一个 `asyncio.Queue` + 单 writer task（照抄 `shared_preferences.py:139-180` 的形状）；需要跨进程保护时用现成的 `filelock`。
5. **`terminate()` 里先 `await sp.flush()` 再关自己的 SQLite 连接**，因为插件更新会先调 `terminate`（`star_manager.py:2107`）再整目录重放，留给你收尾的窗口只有那一次回调。
