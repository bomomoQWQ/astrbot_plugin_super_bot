# 30 · 决策记录（ADR）

> 每条：背景 → 决策 → 后果。标 `†` 的决策有用户在对话中的直接修正。
> 这些决策是整个调查的收敛点，改动任意一条都会连带影响其他条。

---

## ADR-01 · 做成 AstrBot 插件，不 fork 核心

**背景**：AstrBot 是 AGPL-3.0、45k★、1,126 open issue、当日仍在推送。上一代项目（《为人民服务》）是 OpenCode 插件，受制于平台能力。

**决策**：`astrbot_plugin_super_bot`，装进 `data/plugins/`。能用插件做的绝不改核心。

**后果**：
- ✅ 上游更新不腐烂；AGPL 不传染（我们是独立插件，不修改分发核心）
- ✅ 插件目录下 `skills/` 自动纳入 Skill Manager，20 个 SKILL.md 零成本迁移
- ❌ 进不了 pipeline 前 6 站（`STAGES_ORDER` 写死，不在表里的 Stage 会 `index()` 抛错）
- ❌ 私有 API（`_apply_sandbox_tools` 等）不可依赖

---

## ADR-02 † · 只有一个 Router 人格，它同时负责"听"和"说"

**背景**：早期草案把「对话通道」当成独立人格（Path A），与文章第 2 节冲突——原文："Router 层……负责承接所有入口的人类交互"、"由 Router 层统一向用户回复"。用户在对话中直接指出这一点。

**决策**：所有人类消息与环境事件都进同一个 Router；Worker 的产出也必须经 Router 出口。

**后果**：
- ✅ 单一序点、跨信道连续
- ❌ 主 agent 必须让位（`stop_event` + `yield` 或配置层关闭 `provider_settings.enable`）
- ❌ 你接过了对话体验的全部成本（流式、人设、装饰链）——但这块可以借，见 ADR-03
- ✅ 顺带消掉了"两套体系边界"这个难点：主 agent 不再是大脑，就不存在抢活

---

## ADR-03 · Router 借 pipeline 的嘴（`request_llm`），Worker 直调 `tool_loop_agent`

**背景**：两条跑道能力不同（见 `../调查/01-runtime.md` §1）。

**决策**：
- Router 需要人看回复 → `yield event.request_llm(...)` → 流式 + 落库 + 装饰链
- Worker 后台干活、人看不到过程 → `Context.tool_loop_agent(contexts=自己的)` → 精确控制上下文，不要流式

**后果**：
- ✅ 各取所长
- ❌ `request_llm` 的 `contexts` 与 `conversation` **互斥**（`astr_message_event.py:466-467`）→ 落库与自定义上下文不可兼得
- → 因此 Router 的**跨信道全局记忆要自己存**（摘要级 + ID 图），落库用当前 UMO 的 conversation

---

## ADR-04 · 状态存事实，状态算出来

**背景**：Shipyard Neo 的 Sandbox 状态是"根据当前 Session 实时计算"而不是存储字段；参考实现也拒绝让 Ticket 状态跟随 Executor。

**决策**：`Ticket.state_facts: list[Fact]`，`ticket_state(facts) -> str` 纯函数。

**后果**：
- ✅ 永不出现"状态说 running 但 worker 早死了"
- ✅ 重载后从事实重建，天然抗崩溃
- ❌ 读状态要过一次计算（可缓存，但不是权威）

---

## ADR-05 · 幂等键：内容哈希，父 id 进子 id

**背景**：参考实现 `key(*parts)=sha256(json)[:32]`，`ticket=key('ticket',eval_id)` 一行买到"一个 Eval 只一个 Ticket"。

**决策**：
```python
trigger_id = key("trigger", source, thread_key, platform_msg_id)
ticket_id  = key("ticket",  eval_id)
```

**后果**：
- ✅ 重复投递天然去重（落库 `INSERT OR IGNORE`）
- ✅ 不需要额外的去重表
- ❌ 内容里任何一个字段变化就是新 id → 字段必须**稳定**（不能用时间戳、不能用自增序号）

---

## ADR-06 · Eval 串行化用"单槽 + 只追加"，不用锁也不用定时器

**背景**：文章 2.1 的 batching 若用定时器实现，会引入节流参数（拍脑袋的数字）。

**决策**：`Thread.evaluating` 单槽 + `pending` 只追加；`start()` 时一次排空取批。

**后果**：
- ✅ 合并窗口自动等于"当前 Eval 的存活时长"，**零参数**
- ✅ 无锁，纯 asyncio 单线程语义
- ❌ 一个 Eval 卡住会阻塞该 Thread 的所有后续（必须配超时：连续 3 次超时记 `unavailable`）

---

## ADR-07 · 沙箱走官方 SDK，不用 `get_booter()`

**背景**：流 E 查证 `get_booter()` 的隔离粒度是 UMO、参数只读全局配置、无空闲回收、污染全局 `session_booter`。

**决策**：插件 `requirements.txt` 直接依赖 `shipyard-neo-sdk`，一个 Ticket 一个 sandbox。

**后果**：
- ✅ 粒度正确（Ticket 级）
- ✅ 参数可传（endpoint/token/profile/ttl）
- ❌ **必须自己保活**（AstrBot 从不调 `keepalive`/`extend_ttl`，TTL 3600s 到期 Cargo 级联删除）
- ❌ 多一个外部服务要部署（Windows 上建议 Bay 也跑在 Docker Desktop 的 Linux 容器里）

---

## ADR-08 · 经验沉淀落在 `Policy.rules`，不是记忆库

**背景**：流 D 查明参考实现**根本没有经验沉淀**。而我们的上一代项目有（红头 → skill），是它比参考实现多走的一步。

**决策**：把经验做成 `{id, name, enabled, instruction}` 的规则表，**参与 `policy_version` 哈希**。

**后果**：
- ✅ Gate 可确定性校验
- ✅ 规则变更能追踪"影响了哪些 Ticket"（参考实现的 `policyEffects` 思路）
- ✅ 不是向量库，不需要检索质量
- ❌ 只适合"可写成规则"的经验；语义类经验（如"这个方案不好"）仍需人或模型评判

> **映射**：红头文件 = 一次 `policy_version` 升级；skill = 其中的一条 rule；`source:` 字段 = 规则变更的来源凭据。

---

## ADR-09 · P0 只做纯函数，不碰 SDK

**决策**：P0 = `gate.py` + `thread_key.py` + `store.py`，全部只依赖标准库，可单测（见 `20-p0-spec.md`）。

**后果**：
- ✅ 10 条单测能覆盖最容易静默出错的部分
- ✅ 不依赖沙箱、不依赖 Router、不依赖 Docker
- ✅ 对应参考实现 `admission.ts` 1KB vs `assessment.ts` 20KB 的分野

---

## ADR-10 · 先不要做双层架构

**背景**：上一代项目"为了架构而架构"，在还不知道要它干嘛的时候先付了复杂度。

**决策**：P0→P1→P2（骨架 + skills + watchdog）后先跑一周真实任务，再决定 Router/Worker 的形态。

**后果**：
- ✅ 首个可用价值点提前
- ✅ Thread 指纹可以等有真实流量再定（这正是唯一需要经验的设计）
- ❌ 需要克制"先搭好架构"的冲动

---

## ADR-11 · 自己的状态库放 `data/plugin_data/<plugin>/state.db`，用 SQLite

**背景**（流 B 报告）：插件更新会 `remove_dir(target_plugin_path)` **整目录重放**（`star_manager.py:2115-2119`）—— 放在插件目录里的状态必失。而 `plugin_data/` **在备份白名单里**，主库里自建的表**不在**（`backup/constants.py:43-57,75-83`）。

**决策**：状态库 = `data/plugin_data/<plugin_name>/state.db`（`StarTools.get_data_dir()`），SQLite + WAL + `busy_timeout`。`PluginKVStore` 只放单条配置/指针。

**后果**：
- ✅ 插件更新/卸载/备份都不丢
- ✅ `aiosqlite` 是硬依赖，不用额外装
- ✅ 追加型数据（trigger / disposition）天然合适
- ❌ 必须自带 `schema_version` 表（框架没有插件级迁移机制）
- ❌ 并发责任在插件（框架的 `add_message_pair` 是裸 RMW、KV 无 CAS）
- ⚠ **改插件的 author/name 会让 KV 变孤儿**（键空间是 `author/name` 派生）

---

## ADR-12 · `terminate()` 里先 `sp.flush()` 再关库

**背景**：插件更新前只给一次 `terminate()` 回调（`star_manager.py:2107`），而且只有**直接定义在插件类里**的 `terminate` 才会被调用（`:1962-1963` 检查 `__dict__`）。

**决策**：`terminate()` 顺序固定为 —— 取消循环 task → `await 收尾` → `await sp.flush()` → 关 SQLite。

**后果**：
- ✅ 最后一批写入不丢
- ❌ 继承来的 `terminate` 不生效，容易踩

---

## Rejected Concepts

| 拒绝 | 理由 |
|---|---|
| 跨平台身份映射（同一个人在不同 IM 是不同 id） | 身份只是 Trigger 的元数据；映射是提示词文本 |
| 在插件里建用户/权限系统 | 用平台 sender id + AstrBot 的 `admins_id` / `permission` filter |
| 让 Router 能直接执行任务 | 一旦能执行，它就会执行；吞吐与上下文连续性同时垮 |
| 用自由文本推断"任务在等人" | 要结构化字段（参考实现叫 `Attention`） |
| 把任务状态绑在 asyncio task 上 | 重载即失忆 |
| 新建 `ExecutionHistory` 实体 | 参考实现证明它就是 `Ticket.assignments[]` + Decision 历史 |
| 把 `Subscription` 做成独立实体 | 参考实现拆成 `Thread.subscriptions` / `Thread.context` / `Ticket.follows` 三处冗余字段 |
| 用 `get_booter()` 做 per-Ticket 隔离 | 粒度是 UMO，参数不可传 |
| 用定时器实现 batching | 单槽 + 只追加就够了，且没有拍脑袋的参数 |
| 先搭双层架构再找活干 | P0→P1→P2 之后先跑一周真实任务 |
