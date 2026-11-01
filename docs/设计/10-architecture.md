# 10 · 综合架构设计（草案 v0）

> 依据：《Super-agent 设计探索》+ `dsh-superagent-prototype` 领域模型 + AstrBot v4.28.1 实读（见 `../调查/`）。
> 状态：**草案**。标 `◆` 的条目依赖未完成的调查流（流 B/C/D/E），可能调整。

---

## 1. 目标与非目标

**目标**：在一个 AstrBot 实例上，做出「单一对话人格 + 多任务并行编队 + 经验沉淀」的形态。

| 要 | 不要 |
|---|---|
| 一个 Router 人格，跨信道连续 | 多个 agent 各自回复用户（交互混乱） |
| Worker 隔离执行，互不干扰 | 让 Router 直接干活 |
| 环境事件驱动（可持续观察） | 只响应 @ 消息 |
| 经验可沉淀、可复用 | 每次从零开始 |
| 重启后任务不丢 | 状态只活在内存里 |

**非目标**：不用它并行改代码（无 per-task 目录隔离，除非接沙箱）；不做多租户。

---

## 2. 总体结构：一个 Router、两条入口、一个出口

> 修正记录：早期草案把「对话通道」当成独立人格（Path A），与文章第 2 节冲突——**Router 同时负责承接人类交互与统一回复**。以下为修正后版本。

```
           入口 ①（人类消息，经 pipeline 9 站）
用户 ──────────────────────────────────┐
                                       │
           入口 ②（环境事件，直接入队或合成事件）├──→  Router（唯一人格，常驻循环）
Source/定时器/Webhook ──────────────────┘         │
                                                  │ 派活 / 收报告
                                       ┌──────────┴──────────┐
                                       ▼                     ▼
                                   Worker #1 … Worker #N（隔离上下文）
                                                  │
                                        汇报（不直接对用户说话）
                                                  ▼
                            出口：Router 统一回复 → send_message(umo) / 借 pipeline 的嘴
```

**入口 ③（可选）：外部可编程触发。** 三类 Webhook 的取舍（见 `../调查/03-triggers.md`）：

| 路径 | 鉴权 | 是否走 pipeline | 用途 |
|---|---|---|---|
| `POST /api/v1/chat` | API Key | ✅ 全流程（webchat 队列） | **唯一"可编程且过完整 pipeline"的外部触发** |
| `/api/platform/webhook/{uuid}` | **无** | ✅ | 只能伪造平台消息，风险高 |
| `/api/plug/...` | Dashboard JWT | 自定义 | 插件自有 Web API |

三条硬约束（来自 AstrBot 现实）：

1. **Router 必须是插件自持的循环**，因为主 agent 的会话按 UMO 分片（`conversation_mgr`），跨不了信道。
2. **`event` 是 Agent 的身份载体**：`tool_loop_agent`、`ActiveEventRegistry` 都要它。所以每个 Worker 都要绑定一个"出生事件"。
3. **pipeline 只负责入口与出口**，不承载 Router 的逻辑。

---

## 3. 领域模型（Python 草案）

只保留**必要**概念，砍掉当前规模用不上的（比如 `Correlation`）。

```python
# ---------- 入口侧 ----------
@dataclass
class Trigger:
    id: str                      # 幂等键：sha1(source|thread_key|event_id)
    source: str                  # "im:aiocqhttp" / "cron" / "webhook"
    umo: str                     # 统一消息来源；回复地址
    thread_key: str             # 同一件事的归属（见 §7）
    actor_id: str               # 发送者（平台身份，不做跨平台映射）
    text: str
    ts: float
    raw_ref: str | None = None  # 原始消息在平台侧的 id，便于追溯

@dataclass
class Thread:
    key: str
    triggers: list[str]         # trigger id，按时间序
    ticket_ids: list[str]       # 该 Thread 上的工单
    running_eval: str | None    # 同时最多一个 eval
    decisions: list[str]

# ---------- 授权与工单 ----------
@dataclass
class Mandate:
    policy_version: str
    scope: str                  # 授权范围（一句话）
    strength: Literal["explicit", "inferred"]
    granted_by: str             # 人的平台身份
    granted_at: float
    # 不可变：范围要扩大 → 新开 Ticket

@dataclass
class Ticket:
    id: str
    thread_key: str
    mandate: Mandate
    state_facts: list[Fact]     # ★ 只存事实，状态算出来（见 §5）
    worker_id: str | None
    created_at: float

@dataclass
class Assignment:
    id: str
    ticket_id: str
    executor: str               # worker 标识
    desired: Literal["pending", "running", "ended"]
    observed: Literal["pending", "starting", "running", "degraded", "stopping", "stopped", "failed"]
    started_at: float | None
    ended_at: float | None
    abort_reason: str | None

# ---------- 订阅 ----------
@dataclass
class Subscription:
    ticket_id: str
    thread_key: str
    # 多对多；fan-out 时不检查是谁发的
```

**刻意不建模的**（对齐参考实现的 `Rejected concepts`）：

| 不做 | 理由 |
|---|---|
| `Principal` / 跨平台身份映射 | 身份只是 Trigger 的元数据；映射是提示词文本 |
| `Correlation` 做路由 | 它只加上下文，**只有 Subscription 路由** |
| 一个 Thread 只允许一个活 Ticket | 一个 Thread 可以带多件独立的事 |
| Ticket 状态跟随 Worker 生命周期 | Worker 会换手、会重载、会崩 |

---

## 4. 模块边界（文件结构）

```
astrbot_plugin_super_bot/
  metadata.yaml
  _conf_schema.json          # WebUI 表单：来源白名单、模型、预算、thread 规则
  requirements.txt
  main.py                    # Star 子类：initialize() 起两个循环 + terminate() 收尾
  core/
    limits.py                # 所有阈值集中一处（含预算、超时、退避）
    store.py                 # 落盘：原子写 + schema 版本 + 迁移入口
    gate.py                  # 确定性准入（纯函数，可单测）
    thread_key.py            # ★ Thread 指纹（纯函数，可单测）
    router.py                # Router 常驻循环（跑道 C 借嘴 / 队列 / 批处理）
    worker.py                # Worker 执行（跑道 B）
    watchdog.py              # 60s tick + 升级表
    outbox.py                # 可靠投递（重试、去重、顺序）
    sandbox.py               # SandboxPort 抽象 + LocalSandboxPort 实现
  skills/                    # 20 个 SKILL.md，AstrBot 自动纳入 Skill Manager
  pages/                     # 可选：插件面板（只读投影）
```

**边界规则**：
- `gate.py` 与 `thread_key.py` **只依赖标准库**，不 import astrbot —— 这样能被单测覆盖（这是参考实现 `admission.ts` 1KB vs `assessment.ts` 20KB 的启示）。
- `router.py` / `worker.py` 是唯一碰 SDK 的地方。
- `store.py` 是唯一写盘的地方。

---

## 5. 状态机：存事实，算状态

参考实现与 Shipyard Neo 都用同一招——**状态不是存储字段，而是从事实实时算出来的**。

```python
def ticket_state(facts: list[Fact]) -> str:
    if any(f.kind == "human_stopped" for f in facts):   return "stopped"
    if any(f.kind == "delivered" for f in facts):       return "done"
    if any(f.kind == "blocked" for f in facts):         return "blocked"
    if any(f.kind == "worker_failed" for f in facts):   return "retrying"
    if any(f.kind == "accepted" for f in facts):        return "running"
    return "open"
```

好处：永不出现"状态说 RUNNING 但 worker 早死了"。

**`Assignment` 用 Desired vs Observed 双状态**，并且必须区分 `degraded`（没死但不健康）——这正是当年监委要抓的"看起来在跑其实没进展"。

---

## 6. 三个关键时序

### 6.1 人类消息 → Router 决定"直接回答"

```
platform → pipeline 9站 → Gate(放行指令/丢弃自消息) → 判定 thread_key
  → Router 一次轻量判断（含全局摘要上下文）
      → 决定「直接回」
          → yield event.request_llm(prompt=..., system_prompt=Router人格,
                                    conversation=当前umo的conv)   # ★ 流式 + 落库
          → ResultDecorate → Respond
```

**注意**：`request_llm` 的 `contexts` 与 `conversation` **互斥**（`astr_message_event.py:466-467`）。
所以取"落库 + 流式"时，全局记忆只能靠 `system_prompt` 注入摘要。

### 6.2 人类消息 → Router 决定"开任务"

```
Gate → Router 判断 → 建 Ticket(带 Mandate) → 入队
  → yield "收到，我处理一下"        # ★ 必须 yield，否则主 agent 还会跑
  → 事件到此结束（handler 返回）

（后台）
Worker 取 Ticket → tool_loop_agent(contexts=自己的) → 完成
  → 写 result → Outbox → Router 决定怎么回 → send_message(umo)
```

### 6.3 环境事件（定时/Webhook）→ 主动行动

```
cron/webhook → 构造 Trigger（无 event 载体！见 §8 风险）
  → 直接入 Router 队列（不经 pipeline）
  → Router 判断 → 派 Worker 或 自己回
```

---

## 7. `thread_key`：唯一需要经验的设计

分两种模式（取决于 `unique_session` 配置，见 `../调查/03-triggers.md`）：

| 模式 | 判定 | thread_key |
|---|---|---|
| `unique_session = false` | UMO 的 session_id 是群号 | `f"{platform}:{group_id}:{topic_hint}"` |
| `unique_session = true` | UMO 的 session_id 是 `sender_group` | 必须显式取 `get_group_id()`，别用 UMO |

`topic_hint` 的来源按平台定，优先级：
1. 回复链（`Reply` 消息段的 `sender_id` / `message_id`）→ 同一话题
2. 显式标签（如消息里带 issue/PR 编号）
3. 兜底：`f"{group_id}:{天粒度}"`

**先只支持一种平台 + 一种指纹，跑稳再扩。** 指纹错了会让任务串味，比不做直通更糟。

---

## 8. 已知风险与开放问题

| # | 风险 | 应对 |
|---|---|---|
| 1 | ~~无 event 的 Trigger 无法跑 agent / 无法登记 registry~~ **已解决** | 照抄 `core/cron/events.py` 的 `CronMessageEvent`（67 行）为每个逻辑任务造合成事件：自带 UMO、`is_wake=True`、可 `send()` 到任意会话、可被 `ActiveEventRegistry` 登记 |
| 2 | Thread 指纹算错导致任务串味 | 只支持一种平台先跑；提供 `/sb thread list` 人工纠正 |
| 3 | 预算失控 | 复用 `ProviderStat` + `agent_type` 分开统计，超预算熔断 |
| 4 | **两个出口发送顺序** | `RespondStage` 直接 `await event.send()`，**无锁、无队列、无串行化**（`pipeline/respond/stage.py:230,281,310,320`）。→ Outbox 必须**按 UMO 串行**（每 UMO 一把 `asyncio.Lock` + 单消费者），否则 Router 的主动投递会和别人的 RespondStage 交错 |
| 5 | 热重载丢状态 | 所有状态落盘；`terminate()` 里 await 收尾；schema 版本号 |
| 6 | 沙箱未部署 | `SandboxPort` 先本地实现，Docker 验证通过再切 ◆ |
| 7 | **提示词注入** | Gate 是确定性防线，拦不住语义攻击；高危工具（shell/写盘）加二次确认 |
| 8 | **`RateLimit` 默认 30 次 / 60 秒**，常驻 Router 的密集回复会被自家限流卡住 | `config/default.py:69-73`；`rate_limit_check/stage.py:71-82` → 上调或对 Router 的 UMO 加白名单 |
| 9 | **会话级禁用插件后，消息仍会流向默认 LLM** | `waking_check/stage.py:247-254` → 这就是 Router **必须自己 `stop_event`** 的原因，不能指望"插件没跑就等于没跑" |

---

## 12. 调查完成度

| 流 | 文档 | 状态 |
|---|---|---|
| A 运行时 | [01-runtime.md](../调查/01-runtime.md) | ✅ |
| B 持久化 | [02-state.md](../调查/02-state.md) | ✅ |
| C 触发面 | [03-triggers.md](../调查/03-triggers.md) | ✅ |
| D 参考实现 | [04-reference-model.md](../调查/04-reference-model.md) | ✅ |
| E 沙箱 | [05-sandbox.md](../调查/05-sandbox.md) | ✅ |

流 C 报告标注的「未查清」项：`follow_up` 生命周期、`subagent_orchestrator` 是否自建事件、`live_chat`/`chat_projects` 是否完全复用队列、`was_aborted` 回调注册点、多配置档竞态。

---

## 9. 分期（与实现无关，仅标依赖顺序）

| 期 | 内容 | 依赖 |
|---|---|---|
| P0 | 骨架 + Gate 三条硬规则 + `thread_key` 纯函数 + 单测 | 无 |
| P1 | `skills/` 移植 20 个 SKILL.md | 无 |
| P2 | watchdog（60s + 升级表）+ Outbox | P0 |
| P3 | Router 循环（直接回答路径）+ 队列 | P0 |
| P4 | Thread 订阅直通 | P0 P3 |
| P5 | Worker（`tool_loop_agent` + `SandboxPort`） | P3 |

---

## 10. 沙箱接入策略（据流 E 报告修正）

**修正结论：不要走 `get_booter()` 做 per-Ticket 隔离。** 理由（`../调查/05-sandbox.md`）：

| 问题 | 证据 |
|---|---|
| 隔离粒度是 **UMO**，不是 Ticket | `astr_main_agent.py:1631-1632` |
| endpoint / token / ttl / profile **只能读全局配置，不能传参** | `computer_client.py:609-626` |
| 无空闲回收（`idle` 回收只对 CUA 生效） | `computer_client.py:666-667` |
| 会污染共享的全局 `session_booter` | `computer_client.py:22,665` |
| 现成沙箱工具把 session **硬绑** `event.unified_msg_origin` | `python.py:97-100` |

**正解**：插件**直接依赖官方 SDK** —— AstrBot 自己就是这么做的：

```
requirements.txt:55   shipyard-neo-sdk>=0.2.0
booters/shipyard_neo.py:20-26   from shipyard_neo import BayClient
```

于是 `SandboxPort` 的实现直接是 `BayClient`，一个 Ticket 一个 sandbox，自己管生命周期。

### 10.1 必须自己做的两件事（否则一定出事）

**① 保活。** 全仓库 grep：**AstrBot 从不调用 `keepalive` / `extend_ttl`**（零命中）。
沙箱 TTL 默认 **3600s**，到期后 managed Cargo **级联删除** → 工作区没了。
→ 你的 `SandboxPort` 必须有后台保活任务（例如每 20 分钟 `extend_ttl`），且**在 Ticket 结束前不允许过期**。

**② 冷启动不是 bug。** Profile `idle_timeout` 默认 **1800s**，容器会被回收；下次调用表现为"莫名卡 10~60 秒"。→ 这是 Session 透明的代价，日志里要标出来，否则会被当成卡死。

### 10.2 配额的真实来源（更正 §13.7 的说法）

`MAX_SHIP_NUM` 属**旧版** shipyard（`compose-with-shipyard.yml:23,31` → 代码 `shipyard_max_sessions`，`computer_client.py:601`，仅 `booter=="shipyard"` 分支）。

**Neo 的 `Settings` 里没有任何并发上限字段。** 真实限额只来自：

- `Profile.resources`（cpus / memory）
- `Profile.idle_timeout`
- `cargo.default_size_limit_mb`
- `gc`
- `warm_pool_size`

429 `quota_exceeded` 有错误码但无对应配置 → 推断默认不限数量，实际瓶颈是宿主机资源。

### 10.3 幂等边界（重要）

Bay 的 `Idempotency-Key` **只对「创建沙箱 / 创建 Cargo / 延 TTL」有效，`exec` 不幂等**。

→ 所以"执行一个 Python 片段"这个动作，重试会**真的执行两次**。你的 Outbox 与重试逻辑必须知道这个边界：**可重试的只有控制面操作，执行面要靠自己的幂等键。**

### 10.4 Windows 落地建议

- AstrBot 的 `driver.type` 只有 `docker | k8s`，没有第三种。
- `__auto__` 自动拉起在 Windows 上坏掉（`bay_manager.py:106-109` 硬编码 `/var/run/docker.sock`）。
- **最省心：把 Bay 也放进 Docker Desktop 的 Linux 容器**，用官方 compose 原样跑；插件通过暴露的端口用 `host_port` 模式访问。

---

## 11. 从参考实现借来的两个关键机制

### 11.1 幂等键：父 id 进子 id

参考实现：`key(*parts) = sha256(json(parts))[:32]`，且父 id 参与子 id 计算：

```
ticket_id   = key("ticket",   eval_id)
decision_id = key("decision", eval_id)
```

一行代码买到「**一个 Eval 只产生一个 Ticket**」的强保证。→ 我们的对应：

```python
trigger_id = key("trigger", source, thread_key, platform_msg_id)   # 入库去重
ticket_id  = key("ticket",  eval_id)                                # 一个评估只一个工单
```

### 11.2 Eval 串行化：单槽 + 只追加，零定时器

参考实现不用锁：

- `Thread.evaluating`（单槽，当前正在跑的 eval）
- `Thread.pending`（只追加的队列）
- `start()` 时**一次排空**取批

于是"合并窗口"自动等于"当前 Eval 的存活时长"，**不需要任何定时器或节流参数**。这正好对应文章 2.1 的 batching，但实现比定时器简单得多。

阈值：连续 3 次超时（`REQUEUES=2`）才记 `unavailable`；未送达主会话的 Eval 永不超时；暂停时 deadline 顺延。

### 11.3 参考实现的另一个启示：经验沉淀它没有

流 D 明确指出：**参考实现里经验沉淀完全不存在**（`src/core/memory.ts` 只是测试用内存仓储）。它的哲学文档「Few concepts」明确拒绝为业务需求新增概念。

建议（流 D 标注为推断）：把经验沉淀落成 `Policy.rules` —— `{id, name, enabled, instruction}`，**参与 `policy_version` 哈希**，于是：

- Gate 可校验（确定性）
- 规则变更能追踪"影响了哪些 Ticket"
- 不是自由文本记忆库，也不是向量库

这正好对上你当年的「红头 → skill」：**红头 = 一次 Policy 版本升级，skill = 其中的一条 rule**。

---

## 12. 待合并

- ◆ 流 B（持久化）：`PluginKVStore` / `sp` / `conv_manager` 的确切 API 与迁移机制
- ◆ 流 C（触发面）：cron 能否注册插件任务、每个 stage 的精确中断条件

已完成：[01-runtime.md](../调查/01-runtime.md)、[04-reference-model.md](../调查/04-reference-model.md)、[05-sandbox.md](../调查/05-sandbox.md)
