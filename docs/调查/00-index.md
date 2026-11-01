# 架构大调查 · 索引与结论速查

> 调查对象：AstrBot **v4.28.1**（`ab42c0d9b`，2026-09-14，`<AstrBot 仓库>`）
> 参考实现：`deepseek-ai/dsh-superagent-prototype`（MIT）
> 沙箱候选：`AstrBotDevs/shipyard-neo`（AGPL-3.0）
> 方法：源码实读 + 官方文档 + 参考实现文档。**每条结论带 `file:line` 证据；推断显式标注。**

---

## 文档索引

| 文档 | 内容 | 状态 |
|---|---|---|
| [01-runtime.md](01-runtime.md) | Agent 运行时的三条跑道、取消机制、hooks、工具体系、合成事件 | ✅ 完成 |
| [02-state.md](02-state.md) | 持久化原语：KV store / conversation / 路径 / schema 迁移 / 并发 | ✅ 完成 |
| [03-triggers.md](03-triggers.md) | 触发面全景、cron、pipeline 各 stage 的中断条件 | ⏳ 收尾中 |
| [04-reference-model.md](04-reference-model.md) | 参考实现的可移植机制（数据模型 / 状态机 / 算法 / 阈值） | ✅ 完成 |
| [05-sandbox.md](05-sandbox.md) | Shipyard Neo 接入可行性、Bay API、Windows 障碍、保活陷阱 | ✅ 完成 |
| [../设计/10-architecture.md](../设计/10-architecture.md) | 综合架构设计草案 | ✅ 草案 v0 |
| [../设计/20-p0-spec.md](../设计/20-p0-spec.md) | P0 骨架规格 + 12 条单测 | ✅ 可直接开工 |
| [../设计/30-decisions.md](../设计/30-decisions.md) | 12 条 ADR + Rejected Concepts | ✅ |

---

## 十条已确认事实（速查）

| # | 事实 | 证据 |
|---|---|---|
| 1 | **插件有两种驱动 agent 的方式**：`Context.tool_loop_agent()`（直调，可控上下文但**无流式**）与 `event.request_llm()`（yield 出去，走 pipeline，**有流式+落库**） | `star/context.py:215`；`platform/astr_message_event.py:425`；`star/context.py:322-323` |
| 2 | **`FunctionTool` / `ToolSet` 是公开 API**，从 `astrbot.api` 导入即可 | `api/__init__.py:5,73-83` |
| 3 | **`is_background_task=True` 就是文章 4.1 的异步工具**：立即返回 task id，完成后自动唤醒主 agent | `agent/tool.py:61-65`；`astr_agent_tool_exec.py:478-515` |
| 4 | **软停 / 硬停两套语义**，按 UMO 分桶统一入口；软停不中断传播、能收尾落库 | `utils/active_event_registry.py:52-91`；`astr_agent_run_util.py:26-27` |
| 5 | **hooks 只有 4 个点**（begin / tool_start / tool_end / done），拿不到原始 messages | `agent/hooks.py`（全文 30 行） |
| 6 | **上下文落库是显式调用** `conv_manager.update_conversation(umo, cid, history=[...])`，插件可复用 | `agent_sub_stages/internal.py:644-649`；`db/__init__.py:200` |
| 7 | **用量统计已在库里**：`ProviderStat` 表 + `agent_type` 字段可扩展 | `db/po.py:43`；`db/__init__.py:113` |
| 8 | **`unique_session` 会改写 UMO**，直接决定 Thread 指纹的粒度 | `platform/astr_message_event.py:106-108`；`pipeline/waking_check/stage.py:88-92` |
| 9 | **插件 `initialize()` 是实例方法**，加载时确实被 await；`terminate()` 必须**直接定义在插件类**里才会被调用 | `star/star.py:37,42`；`star_manager.py:1227,1421,1962-1963` |
| 10 | **插件目录下 `skills/` 自动纳入 Skill Manager** | `core/skills/skill_manager.py:335-346` |
| 11 | **合成事件已有现成实现**：`CronMessageEvent`（67 行）可充当任意无事件触发器的载体，自带回复地址与唤醒状态 | `core/cron/events.py:14-67` |
| 12 | **`initialize()` 在实例上被 await；`terminate()` 只有直接定义在插件类里才会被调用**（检查 `star_cls_type.__dict__`） | `star_manager.py:1221,1421,1962-1963` |
| 13 | **沙箱走官方 SDK 而非裸 REST**：AstrBot 自己就是 `from shipyard_neo import BayClient` | `requirements.txt:55`；`booters/shipyard_neo.py:20-26` |
| 14 | **`get_booter()` 不能做 per-Ticket 隔离**（粒度是 UMO、参数只读全局配置、无空闲回收、污染全局 `session_booter`） | `astr_main_agent.py:1631-1632`；`computer_client.py:22,609-626,665-667` |
| 15 | **AstrBot 从不调用 `keepalive`/`extend_ttl`**（全仓库零命中）→ TTL 3600s 到期，managed Cargo 级联删除 | 流 E 报告 §C；`default.py:165` |
| 16 | **Neo 没有并发上限配置**；`MAX_SHIP_NUM` 属旧版 shipyard（`shipyard_max_sessions`） | `compose-with-shipyard.yml:23,31`；`computer_client.py:601` |
| 17 | **Bay 的 `Idempotency-Key` 只对控制面有效，`exec` 不幂等** | 流 E 报告 §B.1 |
| 18 | **参考实现的抗注入支点**：Gate（纯函数）与 Assessment（唯一调模型处）之间用不可变 `Mandate` 隔开，模型只能提供 scope，不能提供授权 | `04-reference-model.md` §1；`src/policy.ts` |
| 19 | **插件自身的状态必须放 `data/plugin_data/<plugin>/`**：插件更新会整目录重放删文件；`plugin_data` 在备份白名单而主库自建表不在 | `star_manager.py:2115-2119,1557-1587`；`backup/constants.py:43-57,75-83` |
| 20 | **AstrBot 没有插件级 schema 迁移机制**，也没有 alembic／schema_version 表 → 插件必须自带 | `db/sqlite.py:86,127-145`；`db/migration/helper.py:27-34` |
| 21 | **会话历史 `conversations.content` 是 OpenAI 格式 `list[dict]`，`user_id` 装的正是 UMO**；插件对任意 cid 读写**零鉴权** | `db/po.py:67-91`；`conversation_mgr.py:115-121`；`db/__init__.py:128-137,200` |
| 22 | **`PluginKVStoreMixin` 的限制**：键空间 `scope_id="author/name"`（改名即孤儿）、无 TTL、无范围查询、无法区分"存了 None" | `utils/plugin_kv_store.py:14-28`；`star/star.py:78-82` |
| 23 | **`yield` 的三种语义**：`MessageEventResult` → 下游发送（吞掉事件）；`ProviderRequest` → 主 agent 用你的请求；裸 `return` → 放行 | `pipeline/context_utils.py:47-75` |
| 24 | **`_has_send_oper` 是在洋葱中间（下游发送时）被置真的**，不是 `set_result` 时 | `process_stage/stage.py:56-66`；`astr_message_event.py:297,500` |
| 25 | **`Context.cron_manager` 是公开的**（APScheduler 驱动，`add_basic_job` / `add_active_job`，任务落 DB 重启恢复）→ 插件可注册自己的定时任务 | `star/context.py:51,144,167`；`cron/manager.py:100,148,175` |
| 26 | **`RespondStage` 发送无锁无队列**，直接 `await event.send()` → 两个出口会交错 | `pipeline/respond/stage.py:230,281,310,320` |
| 27 | **事件可以注入**：`Context.get_event_queue()` 暴露原始队列，插件可 `put_nowait` 自己造的事件走完整 pipeline（早前"无注入入口"的判断有误） | `star/context.py:733-735`；`event_bus.py:39-54` |
| 28 | **`/api/v1/chat` 是唯一"可编程且过完整 pipeline"的外部触发**（API Key 鉴权，走 webchat 队列）；平台 webhook 无鉴权只能伪造平台消息；插件 Web API 需 Dashboard JWT | `open_api.py:173-188`；`platform.py:90-119`；`context.py:705-727` |
| 29 | **cron 的 `active_agent` 是唯一绕过整条 pipeline 的旁路**（直接 `build_main_agent` + `step_until_done`），但 Agent 生命周期钩子仍可见 | `cron/manager.py:398-541` |
| 30 | **会话语义**：`SessionPluginManager` 的会话级禁用发生在 `is_wake` 之后 → handler 不执行但**消息仍流向默认 LLM** | `waking_check/stage.py:247-254`；`session_plugin_manager.py:48-102` |
| 31 | **生成器 stage 不 yield 就 return，后续 stage 一个都不跑**；而每次 yield 都会完整跑一遍下游（= 一次装饰+发送） | `pipeline/scheduler.py:44-70` |
| 32 | **RateLimit 默认 30 次 / 60 秒**，会卡住常驻 Router | `config/default.py:69-73`；`rate_limit_check/stage.py:71-82` |

---

## 三个意外收获

**① 全双工异步工具不用自己写。**
给 `FunctionTool` 打上 `is_background_task=True`，就自动获得"立即返回 task id + 后台执行 + 完成唤醒主 agent"。当年在 OpenCode 上要 `promptAsync` + `sleep 2s` + `pending` 队列硬凑的东西，这里是声明式的。

**② Spill 已经实现。**
`tool_loop_agent_runner.py:369-455`：工具结果超阈值 → 写临时目录 → 上下文只留预览 + 路径 → 模型用 `astrbot_file_read_tool` 读回。而 `context.py:302-308` 会自动检测 ToolSet 里有没有这个读取工具，有就自动配好。文章 3.3 的 Spill 侧不用写。

**③ 上下文管理四层能力可注入。**
`truncate_turns` / `llm_compress_*` / `custom_token_counter` / `custom_compressor`，全部通过 `tool_loop_agent(**kwargs)` 透传给 `runner.reset()`。

---

## 三个仍待验证

| # | 问题 | 影响 |
|---|---|---|
| 1 | ~~无 `event` 的 Trigger 如何跑 agent、如何登记 registry~~ **已解决**：照抄 `CronMessageEvent`（67 行）造合成事件 | 不再阻塞设计 |
| 2 | Windows + Docker Desktop 能否跑通 Shipyard Neo 的 `container_network` 模式 | 决定 per-task 隔离的路线是否成立 |
| 3 | Thread 指纹在真实 IM 流量下的准确率 | 决定直通机制是省 token 还是制造混乱 |
