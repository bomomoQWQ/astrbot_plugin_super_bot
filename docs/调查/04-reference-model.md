# 04 · 参考实现机制抽取：dsh-superagent-prototype

> 调查对象：`deepseek-ai/dsh-superagent-prototype`（默认分支 `main`，MIT License，Copyright (c) 2026 DeepSeek Harness Team，见 `LICENSE`）。
> 目的：为 AstrBot「Super-agent」插件（常驻 Router 会话 + 隔离 Worker + Thread 订阅 + 任务编队 + 经验沉淀）抽取**可移植的机制**——数据结构、状态机、算法、阈值。**不抽取提示词正文**。
> 阅读方式：`docs/000~003` 是规范（英文，含术语中英对照表）；`src/core/*` 是纯领域层（无 I/O、无模型调用）；`src/app/*` 是宿主装配层；`src/sources/*` 是 Source 适配层。
>
> 本报告引用路径均为**仓库内相对路径**（可直接拼 `https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/` 复核）。调查时的本地镜像放在 `docs/调查/_ref/`，未通读的文件在 §9 列明。

---

## 0. 一句话总览

参考实现的核心不是「编排大模型」，而是**把「谁有权开工」做成确定性内核（Gate），把「要不要开工」做成可丢弃的模型调用（Assessment），中间用不可变授权凭证（Mandate）隔开**；所有跨进程/跨重启的动作都先落库再执行（Effect/Outbox），所有身份都用内容哈希派生（`key()`），因此整条链路可重放、可去重、可审计。

这个结构对 AstrBot 插件的直接价值：**Router 会话不持有任何权威**，它只产出一个 `Decision`；权威保存在 `Mandate` 里，Worker 只按 `Mandate.scope` 干活；即使 Router 会话被重启、被换模型、被注入垃圾，也不会凭空多出工作。

---

## 1. 分层与依赖方向

| 层 | 目录 | 是否允许 I/O | 是否调模型 | 职责 |
| --- | --- | --- | --- | --- |
| 领域内核 | `src/core/` | 否（同步、纯） | 否 | 不变量、状态机、幂等键 |
| 端口 | `src/core/ports.ts` | 否 | 否 | 定义 Policy / Assessor / Executor / Outbox / Repositories |
| 宿主装配 | `src/app/` | 是（SQLite、子进程） | 否 | Effect 队列、Outbox、Session 管理、健康面板 |
| 评估适配 | `src/app/assessment.ts` | 是 | **是**（主会话） | 唯一调模型的地方 |
| 来源适配 | `src/sources/` | 是 | 否 | 把 GitHub/飞书事件翻译成 Trigger |
| 执行适配 | `src/executor/` | 是 | 是（Worker 会话） | 跑 Assignment |

**关键约束**（`src/core/engine.ts` 头部注释）：`Engine` 是唯一的事务入口，所有 mutation 在 `repo.atomic()` 里同步完成，副作用（Effect）在事务内**只排队**、提交后才分发。`Engine` 向调用方返回的是 `Pick<Engine, ...>` 角色接口（`EngineForSources` / `EngineForMainSession` / `EngineForPeople` / `EngineForSettings` / `EngineForExecutor`，`src/core/engine.ts` 末尾），而不是完整 Engine——**Source 拿不到 `decide`，主会话拿不到 `stopTicket`**。这是最小权限原则在类型系统里的落地，非常值得照搬。

`src/core/part.ts` 的 `EnginePart` 基类给 Intake/Evals/Tickets/Assignments/Replies 五个部件共享 `repo / policy() / now() / emit()`，并提供 `queueForEval`、`handBack`、`takeUp`、`owners` 四个跨聚合原语。

---

## 2. 数据模型（字段级）

以下全部来自 `src/core/model.ts`（单文件 353 行，是整个系统的真相）。已按 Python 可直接翻译的形式重写。

### 2.1 值基元

```python
@dataclass(frozen=True)
class ActorRef:            # 发送者在「该 Source 自己的身份体系」里的 id
    source: str
    id: str

@dataclass(frozen=True)
class ThreadRef:
    source: str
    id: str

@dataclass(frozen=True)
class ChannelRef:          # 写侧地址（只在 Thread 上写，不读）
    source: str
    thread: str
    address: dict[str, str]  # 地址格式由 Source 定义

def thread_key(ref: ThreadRef) -> str:
    return f"{ref.source}:{ref.id}"
```

`isoTime(value)`（`src/core/model.ts` L17-20）：把所有能解析的时间统一成 `Date#toISOString` 形式，**目的是让时间戳可以按字典序比较**（内存与 SQL 里都用字符串比较，而不是解析成时间）。不解析失败的原样返回。**这条要抄**：AstrBot 里大量用字符串时间做比较排序，统一格式能省掉一整类 bug。

### 2.2 Trigger（不可变事件）

```python
@dataclass(frozen=True)
class Trigger:
    id: str                  # 由 Source 保证确定性（见 §5.3）
    source: str
    threads: list[ThreadRef] # threads[0] 是 primary thread，承载它的 Eval
    actor: ActorRef
    human: bool              # Source 声明：人写的 / bot·CI·本 agent 写的
    addressed: Literal['mention','direct'] | None  # Source 声明：是否指向 agent
    at: str
    text: str                # 不可信外部数据
    channel: ChannelRef | None
    url: str | None
    meta: dict               # Source 原生细节，core 永不解释
```

要点：`human` 与 `addressed` **由 Source 声明，不由文本推断**；`meta` 是 core 完全不解释的黑盒，只回传给同一个 Source（`src/core/model.ts` L22-39，与 `src/sources/kind.ts` 的 `SourceKind.scope(input: {meta})` 配对）。插件里对应「平台适配器负责判定 是否 @ 机器人 / 是否 私聊 / 是否 机器人消息」，不要让 Router 用正则猜。

### 2.3 Thread（聚合根：Eval 的串行化边界 + 订阅容器 + 决策历史）

```python
@dataclass
class Thread:
    key: str                          # "source:id"
    ref: ThreadRef
    created_at: str
    last_at: str | None               # 最新 trigger 的时间
    subscriptions: list[str]          # 订阅本 thread 的 ticket id
    context: list[str] | None         # 子集：只当只读上下文订阅的 ticket（不授予权威、不复活）
    correlated: list[str]             # 共享过 trigger 的其它 thread key（只加语境，永不路由）
    pending: list[str]                # 等下一批 Eval 的 trigger id（去抖缓冲）
    evaluating: Evaluation | None     # 同一时刻最多一个
    expired: int | None               # 连续超时重排队次数，Decision 落定即清零
    meta: dict
```

**注意「Subscription 不是独立实体」**：它被拆成三处冗余——`Thread.subscriptions`（正向索引，扇出时用）、`Thread.context`（例外标记）、`Ticket.follows[]`（该 Ticket 绑过哪些 thread，用于判断收尾）。这是一个有意的反规范化：扇出是热路径，必须 O(1) 查 ticket 集合；代价是删除订阅时要写两处（`src/core/tickets.ts` `dropSubscriptions` 同时清 `thread.subscriptions` 和 `ticket.follows`）。**插件照抄时建议保留这个反规范化**，但把 `dropSubscriptions` 做成唯一写入口。

`Thread.context` 是「只读上下文订阅」这个概念的载体：`docs/001-domain.md`（Subscription 行）明确「A Thread an Executor follows for context reaches its Ticket the same way, but lends it no authority and does not resume it」。

### 2.4 Evaluation（进行中的评估）

```python
@dataclass
class Evaluation:
    id: str
    thread_key: str
    trigger_ids: list[str]
    primary: str | None      # 给本批次强度的那个可准入 trigger，Ticket 拿它当 origin
    strength: Strength       # 'explicit' | 'inferred'
    actor: ActorRef          # 「最强可准入 trigger」的发送者，accept 时用它播种 mandate
    basis: MandateBasis      # 同上，Gate 给的准入依据
    policy_version: str
    started_at: str
    delivered: str | None    # 候选卡片送达主会话的时刻；deadline 从此刻算起
    deadline: str
    paused_at: str | None    # 处理暂停开始时刻；恢复时把 deadline 按暂停时长后移
```

`delivered` 的存在解决了一个真实工程问题：**主会话忙的时候不应该消耗评估预算**（`src/app/assessment.ts` L96-102 注释：deadline 在卡片被主会话收到之后才开始计）。`docs/002-authorization.md` §When Eval does not finish：「The time limit starts when the candidate reaches the main Session. Time spent waiting for delivery does not count, and an undelivered candidate never expires.」

### 2.5 Decision（评估结果）

```python
DecisionKind = Literal['ignore','accept','reopen','stop','unavailable']

@dataclass
class Decision:
    id: str
    eval_id: str
    thread_key: str
    trigger_ids: list[str]
    policy_version: str
    at: str
    kind: DecisionKind
    reason: list[Reason]           # 结构化码 + 模板值，可多条（每条可带 trigger_id）
    strength: Strength
    ticket_id: str | None
    mandate: Mandate | None
    audit: dict | None             # 规则评估、方向判定、证据
    dismissed: bool | None         # 人手工忽略了 unavailable 的告警
```

`Reason` 是 `{code, values?, triggerId?}`，`REASONS` 是一张 code→英文模板的表（`src/core/reasons.ts`）。**这是把「为什么」变成可统计数据的做法**：`stopNotExplicit`、`noRuleMatched`、`directionUnsettled`、`ignoredFromCard`、`observed`、`startedTicket`、`routed`、`policyChanged`、`timeout`、`noAdmissible` 这些码可以直接做面板聚合与回归测试断言。注意 `reasons.ts` 里是**模板字符串不是提示词**，可以照抄结构。

### 2.6 Mandate 与 MandateBasis（不可变授权凭证）

```python
Strength = Literal['explicit','inferred']
AdmissionCell = Literal['listedUnaddressed','unlistedAddressed','unlistedUnaddressed']
AcceptMode = Literal['off','observe','auto']

@dataclass(frozen=True)
class Received:                 # 收到位置的回执
    source: str | None
    scope: dict | None          # Source 记录的接收范围
    row: str | None             # admission 表里的行

# MandateBasis 是 Received 的「判别联合」，Python 用继承或 tagged union
@dataclass(frozen=True)
class BasisAddressed(Received):    via = 'addressed';  addressed: Literal['mention','direct']
@dataclass(frozen=True)
class BasisFollowup(Received):     via = 'followup';   authority: str | None
@dataclass(frozen=True)
class BasisAdmission(Received):    via = 'admission';  cell: AdmissionCell; addressed: ... ; observation: {mode} | None
@dataclass(frozen=True)
class BasisListed(Received):       via = 'listed';     addressed: ...; observation: {...}
@dataclass(frozen=True)
class BasisBot(Received):          via = 'bot';        addressed: ...; observation: {mode}   # 必有

@dataclass(frozen=True)
class Mandate:
    strength: Strength
    policy_version: str
    basis: MandateBasis     # 给 revalidate 与审计用
    actor: ActorRef
    scope: str              # 允许做的事
```

`basis` 上的 `sender`（受理时的名单名）与 `rules`（评估命中的规则 id 列表）是**复活校验的输入**，见 §5.5。`docs/002-authorization.md` §Resume and stop：「Only three operations change a Mandate: accept creates it, a person's stop withdraws it, and failed revalidation revokes it.」——**Mandate 没有「更新」这条路**，要改就新开 Ticket。

### 2.7 Ticket（授权工作单元）

```python
TicketEndBy = Literal['executor','human']
TicketOutcome = Literal['completed','merged','closed','dropped','stopped']
ReportState  = Literal['progress','waiting','ready','blocked','failed','dropped']

@dataclass
class Attention:                 # 等一个人回答的问题
    id: str
    question: str
    trigger_id: str | None       # 提出该问题的那条人类消息（决定回哪个会话）

@dataclass
class Follow:                    # Ticket 跟踪的 Thread（如它开的 PR）
    thread: str
    own: bool                    # 该 Ticket 自己开的
    closed: {at, outcome: Literal['merged','closed']} | None

@dataclass
class Execution:                 # Worker 自选
    name: str                    # 短名 / 目录名
    directory: str
    assignment: str              # 派工文本

@dataclass
class Ticket:
    id: str
    title: str
    decision_id: str
    mandate: Mandate
    channels: list[ChannelRef]   # 回复去哪
    ended: {at, by: TicketEndBy, outcome: TicketOutcome, reason} | None
    attention: Attention | None
    assignments: list[Assignment]        # 只追加
    inputs: list[TicketInput]            # 只追加（见 2.9）
    undelivered: list[str]               # 尚未交给运行中 Assignment 的 input id
    follows: list[Follow] | None
    answers: list[str] | None            # Worker 已作答的人类提问
    created_at: str
    updated_at: str
    origin: {trigger_id: str, trigger_ids: list[str]}
    execution: Execution | None
    delivery: {error: str, at: str} | None   # 最近一次投递失败，成功即清
```

派生的纯函数（`src/core/model.ts` L317-353），**都是可以直接照抄的判定式**：

```python
live_ticket(t)        = t.ended is None
ticket_outcome(t)     = t.ended.outcome if t.ended else None
running_assignment(t) = first(a for a in t.assignments if a.state=='running')
latest_assignment(t)  = t.assignments[-1]
latest_report(t)      = last report over assignments (findLast)
read_until(t, progress=True) = max( 最近一次报告.at（progress=False 时跳过 progress 报告）, 各 assignment.idle_at )
waits_for_person(t)   = latest_assignment(t).report.state in ('blocked','failed')
                        and 不存在 (input.kind=='trigger' and input.human and not input.context
                                   and input.at > report.at)
```

`waits_for_person` 就是「人工阻塞是否已解除」的唯一判据，`src/core/stage.ts` 的 `canRetry` 直接复用。

### 2.8 Assignment（派工绑定）

```python
AssignmentState = Literal['pending','running','ended']

@dataclass
class Assignment:
    id: str
    state: AssignmentState
    created_at: str
    started_at: str | None
    ended_at: str | None
    ended_reason: str | None
    report: {state: ReportState, summary: str, at: str} | None
    delivered_at: str | None     # Input 最后一次到达 worker 的时刻
    idle_at: str | None          # 该 worker 一次 turn 结束且无待办的时刻
```

判据（`src/core/model.ts` L226-232 注释 + `src/core/assignments.ts` `interrupted`）：
`delivered_at >= report.at` 意味着「worker 自上次报告后又收到了活」——重启恢复时用它区分「该续跑」还是「该补投递」。

### 2.9 TicketInput（可判别联合，六种）

```python
TicketInput = Union[
  {id, kind:'trigger', trigger_id, at, note?, human?, context?:True,
   read_at: str|None,          # None=还在会话里排队；未记录读取语义的旧数据算 delivered_at
   delivered_to?: str,         # 最后投给哪个 Assignment
   delivered_at?: str},
  {id, kind:'policy-changed', from, to, at, effect?: PolicyEffect},
  {id, kind:'reopen',         decision_id, at},
  {id, kind:'resumed',        trigger_id, at},   # 人类消息复活的标记，插在队首
  {id, kind:'text',           text, at},
  {id, kind:'note',           text, at},         # 主会话给的事实，只要语境不要权威
]
```

`read_at` 这一对字段（`read_at` / `delivered_at` / `delivered_to`）是整套「已读/未读」语义的骨架，直接决定 Ticket 结束时「哪些消息要退回给 Eval」——见 §5.6 的 `letGo`。**这是参考实现里最容易被忽略、但对「不丢消息」最关键的三元组。**

### 2.10 TriggerDisposition（去向记录）

```python
@dataclass
class TriggerDisposition:
    filtered: Reason | None   # 在进域前被 Source 范围过滤
    eval_id: str | None
    decision_id: str | None
    fanout: list[str]         # 扇出到的 ticket id（已结束/未复活的会从中剔除）
    context: list[str] | None # 其中只经 context thread 到达的
    requeued: True | None     # 人/主会话重新送去 Eval；handback 不置此位
    handbacks: list[{ticket_id, reason, at}]   # 各 Ticket 各交回一次，附原因
    acked: list[str]          # 承认或作答过它的 ticket id
    at: str
```

`TriggerDisposition` 是「一条消息的全生命周期账本」，`followupOwners()` 只读它就排除了大部分消息（`src/core/model.ts` L79 注释：「The disposition alone rules most messages out, so a Thread or a ticket is read only for one that may have an owner.」）——**先查账本再查实体，是扇出/归属判定的性能关键**。

### 2.11 PolicyEffect（策略变更对 Ticket 的影响）

```python
@dataclass
class PolicyEffect:
    rules: list[{id, name, change: Literal['changed','disabled'], instruction?}] | None
    proactive: False | None            # 推断型工作不再被接受（如从 auto 掉出去）
    requester_allowed: False | None
    requester: ActorRef | None
```

### 2.12 两处**命名不匹配**要显式纠正

| 任务书里的名字 | 参考实现里的真实对应物 | 说明 |
| --- | --- | --- |
| **Subscription** | 无独立实体。`Thread.subscriptions: string[]` + `Thread.context: string[]` + `Ticket.follows: Follow[]` | 见 §2.3。插件里做成 `Subscription` 表也可以，但必须保留 `context_only` 与 `own` 两个标记，否则「只读跟随」和「PR 收尾」两条语义会丢失。 |
| **ExecutionHistory** | 无独立实体。= `Ticket.assignments[]`（只追加，含 `report`/`started_at`/`ended_at`/`ended_reason`/`delivered_at`/`idle_at`）+ `Decision`（`decisions.by_thread(key)` 索引）+ `effect`/`receipt` 表（`src/app/store.ts` L72-83） | 参考实现认为「执行历史」是 Ticket 的一个属性而不是新概念，依据是 `docs/000-philosophy.md`「A business need becomes an attribute of one of them... The design does not add a concept for it.」插件若要独立 `ExecutionHistory` 表，属于**推断性扩展**，参考实现不支持也不反对。 |
| **Experience / 经验沉淀** | **不存在**。`src/core/memory.ts` 是「内存版仓储实现（测试用）」，不是「记忆/经验」。 | 这是本任务与参考实现最大的缺口，见 §8。 |

---

## 3. 状态机

### 3.1 Thread（三个正交子状态，无终止态）

Thread 永不「结束」，只有三种运行状态与一个计数器：

```
         receive(trigger)
                │
                ▼
   ┌──── pending=[t1,t2,...] ────┐   pending 非空 且 evaluating 为空
   │   (去抖缓冲, 只追加)          │──────────────┐
   └──────────────────────────────┘              ▼
                                     ┌── evaluating=E ──┐
                                     │ triggerIds 取走   │
                                     │ pending=[]        │
                                     └────────┬──────────┘
                                              │
              ┌───────────────────────────────┼────────────────────────┐
              │ finish(decision)              │ expire()                │ reassess()
              ▼                               ▼                         ▼
        evaluating=None                 expired<n ? 重排队 : finish   finish(unavailable)
        expired=None                     (unavailable)                 然后把 trigger 插回 pending 队首
              │                               │
              └───────────► pending 非空则下一批 start ◄────────────────┘
```

- **不变量**：`evaluating` 至多一个（`Thread.evaluating?: Evaluation`，单槽，非数组）。
- `pending` 只由 `queueForEval` 追加，且 `queueForEval` 幂等：若已在 `pending` 或已在该 thread 的 `evaluating.triggerIds` 里，直接返回 false（`src/core/part.ts` L57-63）。
- `expired` 计数器由 `finish/ignore` 清零，由 `expire()` 递增。

### 3.2 Evaluation（有终止态，但可重试进入下一轮）

```
start ──► started_at 设定, deadline = started + evalTimeoutMs
             │
             ├── assessor 卡片送达主会话 ──► delivered 设定, deadline = now + evalTimeoutMs
             │                                  │
             │                                  ├── decide() ──► finished
             │                                  └── deadline 到 ──► expire()
             └── 未送达 ──► 「永不超时」（due() 里 `if !evaluating.delivered` 直接 return）
```

`expire()`（`src/core/evals.ts` L60-69）：

```python
REQUEUES = 2   # 连续超时重排队的次数上限

def expire(thread):
    expired = (thread.expired or 0) + 1
    if expired > REQUEUES:                      # 第 3 次超时 → 落定 unavailable
        finish(thread, thread.evaluating, {kind:'unavailable', reason:[{'code':'timeout'}]})
        return
    trigger_ids = thread.evaluating.trigger_ids
    del thread.evaluating
    thread.expired = expired
    thread.pending = trigger_ids + [x for x in thread.pending if x not in trigger_ids]  # 插到队首
    save(thread)
    assessor.release(eval_id)
```

**阈值：连续 3 次超时（`REQUEUES=2` → `expired > 2`）才记为 `unavailable`**，前 2 次静默重排队。这是「繁忙不等于失败」的量化表达，`docs/002-authorization.md` §When Eval does not finish 与代码一致。

暂停处理：`due()` 在 `policy.paused` 时把 `paused_at` 写进 Evaluation，恢复时 `deadline += (now - paused_at)`（`src/core/evals.ts` L35-46）。**暂停期间 Eval 不超时，且已消耗的时间被补回**——这条对 AstrBot 的「维护窗口 / 限流窗口」很有用。

### 3.3 Decision（终止，但 `unavailable` 可被人为 `dismiss`）

```
kind ∈ {ignore, accept, reopen, stop, unavailable}
```
- `stop` 只可能由人类（`explicit` 强度）产生；非 explicit 的 stop 降级为 `ignore(reason=stopNotExplicit)`；目标不存在或已结束降级为 `ignore(stopUnknown)`（`src/core/evals.ts` L293-301）。
- `unavailable` 可被 `dismiss(decision_id)` 标记，从而不再上报（`src/core/evals.ts` L193-199）。
- **`unavailable` 不是终止态意义上的失败**：`docs/002-authorization.md` 明确「A later Trigger can start a new Eval」，即同一批 trigger 可以被后来的 trigger 重新带进来。

### 3.4 Ticket（唯一有「终止 + 可复活」的状态机）

```
                    create(accept)  [by='executor' 或 'human']
                          │
                          ▼
                    ┌──────────┐
                    │   open   │◄──────────────────────────┐
                    └────┬─────┘                           │
                         │ end(by, outcome)                │ wake()
                         ▼                                 │ 仅当：
                    ┌──────────┐                           │  · 有 undelivered input
                    │  ended   │                           │  · ended.by != 'human'
                    └────┬─────┘                           │  · 队首存在 human trigger input
                         │                                 │  · revalidate(mandate) 通过
                         ├── by='human'  → 终态，永久拒绝一切（Mandate 被撤回）
                         └── by='executor' → 可复活 ───────┘
```

- 结局五元：`completed`（ready 无 PR）/ `merged` / `closed`（跟的 PR 收尾）/ `dropped`（没什么可做）/ `merged|closed` 也可能；`stopped` 仅 `by='human'`。
- **复活（resume）没有新 Eval、没有新 Decision**：`wake()` 直接 `delete ticket.ended` 并新建 Assignment，同时把 `{kind:'resumed', trigger_id}` 插到 `undelivered` 队首（`src/core/tickets.ts` L252-270）。`docs/001-domain.md`（Ticket 行）：「goes open → ended, and back to open when a person's message on one of its Threads resumes it, unless a person stopped it」。
- `wake()` 幂等：开头 `if live or ended.by=='human' or not undelivered: return`，且每次都重新读 ticket。
- **被人类 stop 的 Ticket 是唯一真终态**：`intake` 里它根本不进 `reached`（`resumes = human and ended.by != 'human' and not followed`），`attachable()` 也抛 `ticketStopped`。

### 3.5 Assignment（三段线性，可多次并存于一个 Ticket 内）

```
pending ──started()──► running ──endAssignment()──► ended
   │                      │
   │                      └── interrupted()（宿主重启）
   │                            ├── 已 settled（waiting/ready/blocked/failed 且无新投递）→ 保留，只补投 pending input
   │                            └── 否则 → restart()：结束旧 Assignment，建新 Assignment，队首插 recovery note
   └── retry() 时从最新 Assignment 结束再建新（同 Session/目录/分支/PR）
```

`restart()` 的 recovery note 文本在 `src/core/assignments.ts` L21（**这是操作指令不是提示词模板，可自由改写**）；`docs/003-execution.md` §Restart 要求「oldest Ticket first」——`policyChanged` 里的排序注释也印证了「按 `createdAt` 升序遍历，使投递按受理顺序排队」（`src/core/tickets.ts` L202-203）。

### 3.6 Report（六态，直接驱动 Ticket 阶段）

`progress / waiting / ready / blocked / failed / dropped`（`src/core/model.ts` L216）。

`report()` 的收敛规则（`src/core/assignments.ts` L99-134）：
- `dropped` → `end(ticket, 'executor', followsDone?.outcome ?? 'dropped')`
- `ready` → 无 `follows` 时 `completed`；否则等 `followsDone` 成立才用其 outcome 结束
- `blocked`/`failed` → 清掉 `undelivered` 里的**自动** input（`automatic()` = `note` 或非 human 的 trigger），即「不再唤醒」
- `question` 与 `ready` / `answers` 互斥；`question=null` 清空 `attention`；被替换/清空的 `attention` 会把未发出的回复从 Outbox 撤回

### 3.7 TicketStage（**不是存储状态，是纯函数投影**）

`src/core/stage.ts` 整个文件只有 26 行，`ticketStage(ticket)` 按顺序匹配、**首行命中即返回**：

| 序 | 条件 | 投影 |
| --- | --- | --- |
| 1 | `ended` | `{stage:'ended'}` |
| 2 | `ticket.delivery` 存在 | `person / delivery`（Input 投递失败，2s 后重试，退避翻倍到 1 分钟） |
| 3 | `ticket.attention` | `person / answer` |
| 4 | 最新 report = `ready` | `person / review` |
| 5 | 最新 report ∈ {blocked, failed} 且 `waits_for_person` | `person / blocked|failed` |
| 6 | 无 assignment 或最新是 `pending` | `working / queued` |
| 7 | 最新 report = `waiting` 或最新 assignment `ended` | `idle` |
| 8 | 其它 | `working` |

**这是整份报告里最值得直接照抄的一段代码**：面板、健康、重试按钮、并发槽占用全部读这一个投影，不存冗余状态，因此不可能出现「状态不一致」。`docs/003-execution.md` §Ticket stages 与代码逐行对应。

### 3.8 Source / 轮询（有「冷却期」）

`src/sources/poll.ts`：
```python
POLL_HORIZON_MS = 14 * 24 * 3600_000          # 14 天

def polled(ctx, thread, now) -> bool:
    if any(live_ticket(ctx.tickets.get(i)) for i in thread.subscriptions): return True
    times = [thread.last_at or thread.created_at] + [t.ended.at for t in tickets if t]
    return now - max(times) < POLL_HORIZON_MS

def cursor_at(started: str) -> str:           # 起点回退 5 秒，防漏事件
    return started - 5000ms
```
**阈值：订阅的 Thread 在「无活 Ticket」后仍继续轮询 14 天**；轮询游标每次回退 5 秒，靠确定性 trigger id 去重重叠区。`docs/003-execution.md` §Follow-ups and context Threads 同句：「for two weeks after the later of the last Trigger and the subscribing Tickets' end」。

---

## 4. 幂等键体系（先讲这个，因为所有算法都依赖它）

`src/core/ids.ts` 全文只有 5 行：

```python
def key(*values: str) -> str:
    return sha256(json.dumps(values)).hexdigest()[:32]
```

**内容派生的 32 位十六进制**。所有实体 id 都是 `key(...)` 的结果：

| 实体 | 构造 | 文件 |
| --- | --- | --- |
| Trigger | **不由 core 生成**，由 Source 保证确定性 | `src/core/intake.ts` L23 |
| Evaluation | `key('eval', thread_key, policy_version, at, *pending)` | `src/core/evals.ts` L107 |
| Evaluation（直判） | `key('eval', thread_key, 'direct', trigger_id, kind, *[ticket_id], at)` | `src/core/evals.ts` L277 |
| Evaluation（忽略） | `key('eval', thread_key, 'ignored', policy_version, at, *ids)` | `src/core/evals.ts` L248 |
| Decision | `key('decision', evaluation_id)` | `src/core/evals.ts` L329 |
| Ticket | `key('ticket', evaluation_id)` | `src/core/evals.ts` L311 |
| Assignment | `key('assignment', ticket_id, str(len(ticket.assignments)))` | `src/core/tickets.ts` L382 |
| Input（扇出） | `key('input', ticket_id, 'trigger', trigger_id)` | `src/core/intake.ts` L50 |
| Input（重新附加） | `key('input', ticket_id, 'trigger', trigger_id, 'attached', at)` | `src/core/tickets.ts` L140 |
| Input（note） | `key('input', ticket_id, 'trigger', trigger_id, 'note', note)` | `src/core/tickets.ts` L143 |
| Input（策略变更） | `key('input', ticket_id, 'policy', from, to, at)` | `src/core/tickets.ts` L206 |
| Input（复活） | `key('input', ticket_id, 'resumed', trigger_id)` | `src/core/tickets.ts` L265 |
| Input（重启） | `key('input', ticket_id, 'restart', assignment_id)` | `src/core/tickets.ts` L323 |
| Attention | `key('attention', ticket_id, question, at)` | `src/core/assignments.ts` L115 |
| 回复（ack） | `key('ack', trigger_id)` | `src/core/replies.ts` L44 |
| 回复（attention） | `key('attention-reply', attention_id)` | `src/core/replies.ts` L47 |
| 回复（其它） | `key('reply', trigger_id ?? ticket_id ?? '', text)` | `src/core/replies.ts` L79 |

设计规律，三条：
1. **父 id 进哈希**：`ticket = key('ticket', eval_id)`、`decision = key('decision', eval_id)` → **一个 Eval 只能产出一个 Decision、一个 Ticket**，天然不可能重复建单。
2. **语义前缀进哈希**：`'attached'` / `'note'` / `'restart'` / `'resumed'` 制造「同父不同义」的合法新行；`'ignored'` / `'direct'` 让「忽略的 Decision」不会覆盖「已评估的 Decision」（`src/core/evals.ts` L245-246 注释明确说这一点）。
3. **`at` 只在需要区分「同一语义的多次发生」时才进哈希**（如 `policy 变更`、`restart`、`直判`），纯扇出的 Input 不带 `at`，因此**同一 trigger 扇到同一 ticket 永远只有一行**。

持久化层再兜一道（`src/app/store.ts` L72-83）：
```sql
trigger(id TEXT PRIMARY KEY), disposition(id PK), thread(key PK), ticket(id PK),
decision(id PK, thread TEXT), outbox(id PK),
trigger_thread(thread, id, PRIMARY KEY(thread,id)),
effect(kind, key, ..., PRIMARY KEY(kind,key))
```
`effect` 的 `PRIMARY KEY(kind,key)` 配合 `ON CONFLICT DO UPDATE SET generation = generation + 1`（`src/app/effects.ts` L62-63），实现「同键任务合并、但保留队列位置（`seq`）」。

---

## 5. 关键算法

### 5.1 Eval 的串行化与批处理

**三条机制叠加**，缺一不可。

**(a) Thread 上的单槽 + 排空式批量取走**——「同一 Thread 同时最多一个 Eval」的落实：

```python
# src/core/engine.ts  evaluateDue()
def evaluate_due(self):
    policy = self.policy()
    # 阶段 1：只处理「已有 evaluating」的 thread 的超时
    for thread in self.repo.threads.active():
        if thread.evaluating:
            self.repo.atomic(lambda: self.evals.due(thread, policy))
    if policy.paused: return
    # 阶段 2：已结束但有未投递 input 的 ticket 尝试唤醒（复活）
    for ticket in self.repo.tickets.active():
        if not live_ticket(ticket) and ticket.undelivered:
            self.repo.atomic(lambda: self.tickets.wake(ticket.id))
    # 阶段 3：有 pending 且**没有** evaluating 的 thread 才启动
    for thread in self.repo.threads.active():
        if thread.pending and not thread.evaluating:
            self.repo.atomic(lambda: self.evals.start(thread.key, policy))
```
`threads.active()` = `len(pending) > 0 or evaluating is not None`（`src/core/memory.ts` L35）。

**(b) `pending` 是只追加的缓冲，`start()` 一次性全部取走**——「期间到达的 Trigger 合并进下一批」的落实：

```python
# src/core/evals.ts  start()
def start(self, thread_key, policy):
    thread = self.repo.threads.get(thread_key)
    at = self.now()

    # 0) 先剔除「仍被某个 Ticket 的 worker 决定」的人类追问，它们不进 Eval
    followups = [i for i in thread.pending if self.owners(self.repo.triggers.get(i))]
    if followups:
        thread.pending = [i for i in thread.pending if i not in followups]
        save(thread)
        for i in followups: emit('trigger.followup', triggerId=i)
        if not thread.pending: return

    # 1) 已经产生过 Ticket 的 Trigger → 直接 ignore，绝不重复建单
    accepted = self.accepted(thread.key)                 # triggerId -> Decision(accept)
    started  = [i for i in thread.pending if i in accepted]
    if started: self.ignore(thread, started, [startedTicket(i) for i in started], policy, at)

    # 2) 逐条过 Gate，选「最强 / 同强度取最新」的一条作为 primary
    tickets = self.tickets_on_thread(thread)
    strongest, reasons = None, []
    for trg in triggers:
        g = policy.gate(trg, thread, tickets)            # 纯函数
        if not g.admissible:
            reasons.append({code: g.reason, triggerId: trg.id}); continue
        later  = g.strength == strongest.strength and trg.at >= strongest.trigger.at
        weaker = strongest is None or stronger(strongest.strength, g.strength) != strongest.strength
        if weaker or later: strongest = (trg, g.strength, g.basis)
    if strongest is None:
        self.ignore(thread, thread.pending, reasons or [{code:'noAdmissible'}], policy, at)
        return

    # 3) 建 Evaluation；**pending 一次性清空**，之后到达的自动落入下一批
    eval_id = key('eval', thread.key, policy.version, at, *thread.pending)
    thread.evaluating = Evaluation(
        id=eval_id, thread_key=thread.key, trigger_ids=thread.pending,
        primary=strongest.trigger.id, strength=strongest.strength,
        actor=strongest.trigger.actor, basis=strongest.basis,
        policy_version=policy.version, started_at=at,
        deadline=at + policy.eval_timeout_ms)
    thread.pending = []
    save(thread)
    for trg in triggers: save_disposition(trg.id, {..., evalId: eval_id})
    emit('eval.started', ...)
    self.options.assessor.request(self.candidate_of(thread))   # 只排队，不回等
```

**合并窗口 = 当前这一批 Eval 的存活时长。** 期间 `queueForEval` 判 `primary.evaluating.trigger_ids` 包含它吗？不包含（因为它不在这一批里），所以追加到 `pending`；`start` 只在 `evaluating` 为空时才被调用，所以它必然被下一批捞走。**没有定时器、没有去抖延时，纯靠状态机天然合并。**

**(c) 全局 pass 的单飞 + 合并**——避免多个 pass 并发跑同一个 thread：

```python
# src/app/passes.ts
async def pump(self):
    if not configured() or closing(): return
    if self.pumping is None:
        return self.pumping = self.pump_once().finally(clear)
    next_ = lambda: (self.pump_again := None) or self.pump()
    return self.pump_again or (self.pump_again := self.pumping.then(next_, next_))
```
语义：**并发调用者被合并成一次后续 pass**（不是排队 N 次）。定时器 2 秒一次（`src/app/passes.ts` L28），任何 `receive() === 'stored'` 立刻 `request()` 一次。

**AstrBot 移植要点**：`thread.evaluating` 必须是数据库里的一行/一个字段而非内存 flag，否则进程重启后「同一 Thread 一个 Eval」会破。参考实现用 `thread(key PRIMARY KEY, value)` 整行存 JSON，靠 `repo.atomic()` 串行化读改写。

### 5.2 Subscription fan-out 的匹配规则与例外

`src/core/intake.ts` `receive()` 是唯一入口（63 行，全文照录逻辑）：

```python
def receive(self, received: Trigger) -> Literal['stored','duplicate','filtered']:
    trigger = {**received, 'at': iso_time(received.at)}
    if not trigger.threads: raise Error('必须至少属于一个 thread')
    if self.repo.triggers.get(trigger.id): return 'duplicate'      # ← 幂等第一道闸

    at = self.now()
    f = self.options.scope.scope(trigger)                            # ← Source 范围过滤
    if not f.listened:
        save(trigger); save_disposition(trigger.id, {filtered:{code:f.reason}, fanout:[], at})
        return 'filtered'                                            # ← 不进域的记录仍留审计

    save(trigger)
    keys = [thread_key(r) for r in trigger.threads]
    reached = {}                                                     # ticketId -> (ticket, context)
    for ref in trigger.threads:
        thread = self.thread(ref)
        if thread.last_at is None or trigger.at > thread.last_at: thread.last_at = trigger.at
        thread.correlated = uniq(thread.correlated + [k for k in keys if k != thread.key])  # 只加语境
        for ticket_id in thread.subscriptions:
            ticket  = self.repo.tickets.get(ticket_id)
            followed = thread.context and ticket_id in thread.context
            resumes  = trigger.human and ticket and ticket.ended.by != 'human' and not followed
            if not ticket or (not live_ticket(ticket) and not resumes): continue   # ← 例外集合
            reached[ticket_id] = (ticket, followed and reached.get(ticket_id, (_,True))[1])
        save(thread)

    for ticket_id, (ticket, followed) in reached.items():
        add_input(ticket, {id: key('input', ticket_id, 'trigger', trigger.id), kind:'trigger',
                           trigger_id: trigger.id, at,
                           **({'human':True} if trigger.human else {}),
                           **({'context':True} if followed else {})})
    fanout  = list(reached)
    context = [i for i in fanout if reached[i][1]]
    save_disposition(trigger.id, {fanout, **({'context':context} if context else {}), at})

    if not self.owners(trigger):          # ← 例外：无人拥有 → 进 Eval
        self.queue_for_eval(trigger)
    for ticket_id in fanout: self.tickets.wake(ticket_id)
    emit('trigger.received', ...)
    return 'stored'
```

**扇出的匹配规则**（进 Ticket 的条件，全部满足才进）：

| # | 条件 | 依据 |
| --- | --- | --- |
| 1 | Source 范围 `listened` | `kind.ts` 的 `SourceKind.scope()`，可返回 `{listened, observed, row}` |
| 2 | Trigger 的某个 thread 的 `subscriptions` 含该 ticket | `Thread.subscriptions` |
| 3 | ticket 活着 **或** 满足「人类消息 + 非 human 停止 + 非 context-only」 | `resumes` |
| 4 | —— | 注意：**不检查发送者是谁**，`docs/001-domain.md`（Subscription 行）：「fans out to every live subscribed Ticket, with no check of who sent it」 |

**扇出的例外**（这些不进 Ticket，或进了也不算它的活）：

| 例外 | 处置 | 依据 |
| --- | --- | --- |
| 被 `SourceScope` 过滤 | 存 Trigger + `filtered` disposition，不扇出、不进 Eval | `intake.ts` L26-29 |
| 已结束且被人类 stop 的 Ticket | 不扇出（Mandate 已撤回），消息走 Eval 路径 | `intake.ts` L43；`docs/002` §Resume and stop |
| 已结束且非人类 stop，但 Trigger 是 bot/CI | 不扇出、不复活（Executor 复活后会自己重读 Thread） | `intake.ts` L43 注释 |
| 只经 `Thread.context`（只读跟随）到达 | **进** Ticket 但带 `context:true`：不加权威、不赋予「追问所有权」、不复活已结束 Ticket | `docs/002` §Context-only subscriptions |
| 人把某 Thread 的订阅全删掉 | 消息照旧进 Eval；该 Ticket 不再因该 Thread 收尾 | `tickets.ts` `dropSubscriptions` |

**哪些消息不经过 Eval**——只有一个判据：`followupOwners(trigger)` 非空。

```python
# src/core/model.ts L75-88
def followup_owners(trigger, records) -> list[str]:
    if not trigger.human: return []                                   # bot 消息永远进 Eval
    d = records.triggers.disposition(trigger.id)
    if d is None or d.requeued: return []                             # 被重新送去 Eval
    returned = {h.ticket_id for h in d.handbacks or []}
    reached  = [i for i in d.fanout if i not in returned and i not in (d.context or [])]
    if not reached: return []
    # Eval 持有它时（老版本可能排进了队列），无人拥有
    if trigger.id in (records.threads.get(thread_key(trigger.threads[0])).evaluating or {}).trigger_ids:
        return []
    return [i for i in reached
            if (t := records.tickets.get(i)) and (
                 live_ticket(t)
                 or (t.ended.by != 'human'
                     and any(inp.kind=='trigger' and inp.trigger_id==trigger.id
                             and inp.id in t.undelivered for inp in t.inputs)))]

# src/core/part.ts
def queue_for_eval(trigger) -> bool:
    primary = self.thread(trigger.threads[0])
    if trigger.id in primary.pending: return False
    if primary.evaluating and trigger.id in primary.evaluating.trigger_ids: return False
    primary.pending.append(trigger.id); save(primary); return True
```

结论表：

| 消息类型 | 进 Ticket？ | 进 Eval？ |
| --- | --- | --- |
| bot / CI（非名单 bot） | 是（若 thread 已订阅） | **是**（`human=false` → owners 恒空） |
| bot（在 Source 的 bot 名单上） | 是 | 是（但只能成 `inferred`，永不成 explicit、永不 stop） |
| 人类的 @/私聊，来自名单 | 是 | **否**（owner 存在），直接由 worker 决定 |
| 人类消息，到达活着的普通订阅 Ticket | 是 | **否** |
| 人类消息，只到达 context-only Ticket | 是（`context:true`） | **是**（被排除出 `reached`） |
| 人类消息，只到达被 stop 的 Ticket | 否 | **是** |
| 人类消息，无任何订阅 | 否 | **是** |
| 人类消息，worker 已 `handback` 且无其它 owner | 是（仍在） | **是**（handback 后重新排队） |
| 人从面板「送交 Eval」 | —— | 是（置 `requeued`，owners 归零） |

### 5.3 幂等的三道闸（同一个 Trigger 被接受两次为什么只产生一个 Ticket）

**闸 1：Trigger 入库幂等。** `if self.repo.triggers.get(trigger.id): return 'duplicate'`（`src/core/intake.ts` L23）。这要求 `trigger.id` 由 Source 保证确定性——GitHub 用 comment/review id，飞书用 `om_` 消息 id。轮询重叠区靠 `cursor_at()` 回退 5 秒 + 确定性 id 兜住（`src/sources/poll.ts` L15-16 注释）。

**闸 2：Evaluation/Decision/Ticket 的父子哈希。** `decision_id = key('decision', eval_id)`、`ticket_id = key('ticket', eval_id)`。同一 Evaluation 无论被 `decide()` 调用多少次，写进 `decisions`/`tickets` 的都是同一主键行——上层是 UPSERT，物理上无法产生第二行。

**闸 3：显式的「已受理」检查**（真正防「同一 Trigger 两次 accept 产生两个 Ticket」的那道）：

```python
# src/core/evals.ts  decide()
def decide(self, trigger_id, outcome) -> Decision:
    trigger = get(trigger_id)
    if outcome.evalId and held.evaluating.id != outcome.evalId:
        raise UserError('evalFinished')                       # 迟到的结果被拒
    if outcome.kind == 'accept':
        earlier = self.accepted(thread_key(trigger.threads[0])).get(trigger_id)
        if earlier:                                          # ← 该 Trigger 已有 accept Decision
            others = held.evaluating.trigger_ids - {trigger_id}
            if held and others: reassess(held, {code:'acceptedBefore'}, [trigger_id])
            elif held: finish(held, {kind:'ignore', reason:[startedTicket(trigger_id)]})
            return earlier                                   # 直接返回旧 Decision，不建单
    ...
```
`accepted(threadKey)` 遍历该 Thread 的**全部** Decision（含已结束 Ticket 的），建 `triggerId → Decision` 映射（`src/core/evals.ts` L254-265）。

`start()` 里再兜一次（见 §5.1 步骤 1）：**已经在 `pending` 里等着、但其实早已产生过 Ticket 的 Trigger，直接 `ignore(reason=startedTicket)`，连 Eval 都不开。**

三道闸的关系：闸 1 挡「同一条消息重复投递」，闸 2 挡「同一批 Eval 被重复判定」，闸 3 挡「同一 Trigger 在两次不同 Eval 里被重复 accept」。**闸 2 的 `ticket = key('ticket', eval_id)` 是最经济的一道**——只要 Eval 不重复创建，Ticket 就不可能重复。

### 5.4 Outbox：可靠投递（失败重试 / 顺序 / 去重）

底层是一张**落库的副作用队列**（`src/app/effects.ts`），Outbox 只是它的一个 kind（`src/app/outbox.ts`，52 行）。

```python
# 注册：kind='reply'，并发 1，退避 [5000ms, 300000ms]
effects.handle('reply', deliver, {limit: lambda: 0 if paused() else 1, retry: [5000, 300_000]})

def send(self, message: OutboundMessage):
    if message.kind == 'reply' and source == 'feishu': checked_im_text(message.text)
    store.atomic(lambda: effects.enqueue('reply', message.id, {}) if store.receipts.add(message) else None)

def deliver(self, message_id):
    receipt = store.receipts.get(message_id)
    if receipt.status != 'pending': return                          # ← 去重：已发出/已丢弃不再发
    if not sources.instance(receipt.channel.source).enabled:
        raise Later('Source is off')                                # ← 不退避计数，仅按 retry[0] 重排
    if store.receipts.behind(message_id, FAILURES_BEFORE_ERROR):     # ← 顺序闸
        raise Later('An earlier message of this ticket to this conversation waits')
    if not sources.channel_allowed(receipt.channel):
        store.receipts.settle(receipt, 'channelIgnored'); return     # ← 静默丢弃（永久不可达）
    result = await sources.send(receipt, commands)
    if result == 'waiting': raise Later('Source not installed')
    store.receipts.settle(receipt, 'channelGone' if result=='gone' else 'sent')
```

**去重**：`receipts.add()` 以 `message.id` 为主键（`outbox(id TEXT PRIMARY KEY)`），插入成功才入队；`deliver` 首行再查一次 `status`。`Outbox.send` 的 doc 注释写明「Queue a message idempotently; an id queued before keeps its first message」（`src/core/ports.ts` L76-83）。

**顺序保证**（三层）：
1. `effects` 表 `ORDER BY seq` 全局排队，`ordered` 类 kind 一次只跑一条，且**即使首行未到 due 也挡住后面的行**（`src/app/effects.ts` L109-113）。
2. `receipts.behind(id, 3)`：同一 **(ticket, conversation)** 组合里，只要有一条更早的未发出消息、且它失败次数 < 3，后来的就抛 `Later` 让路。**阈值 3 = `FAILURES_BEFORE_ERROR`**（`src/app/effects.ts` L7）。
3. **跨 Ticket 不排队**：`docs/003-execution.md` §Channel failure：「Messages of other Tickets, and messages of no Ticket, do not wait for it.」

**重试与退避**（`src/app/effects.ts` L130-146）：
```python
if failure is Later:                      # 环境性阻塞（暂停/槽满/Source 关）
    due = now + retry[0]                  # 不增加 attempts
else:
    delay = min(retry[1], retry[0] * 2 ** min(attempts, 16))
    attempts += 1; error = str(failure)[:1000]; due = now + delay
```
- Outbox：`retry = [5000, 300000]` → 5s, 10s, 20s, … 封顶 300s。
- 其它 kind 默认 `retry = [2000, 60000]`（relay / notice / launch / delivery / assessment）。
- `min(attempts, 16)` 是**指数爆炸的保险丝**（2^16 已远超 300s 上限，纯防御）。
- 关机时**不删运行中的行**（`else if self.stopped: return`），下次启动自动重试。

**丢弃**（终态，永不重排）：只在 `channelIgnored`（Source 不再监听该会话）或 `channelGone`（目标永久不可达：GitHub 已删 issue / 锁定的会话、飞书已撤回或找不到的消息）时 settle 为 dropped，并**保留记录与丢弃原因**（`docs/003-execution.md` §Channel failure：「A dropped message keeps its record, with when and why it was dropped and the Ticket it was for, so it is never queued again」）。

### 5.5 retry-health：两套完全不同的「健康判定」

⚠️ **任务书里 `retry-health.ts` 的定位需要纠正**：它不是通用的「重试次数→健康度」模块，而是一个**纯投影器**：从 Session 事件流里识别「模型预算被耗尽、正在退避重试」这一特定状态。真正的阈值型健康判定在 `effects.ts` + `infra/health.ts` + `sources/poll.ts`。两者要分开移植。

#### (a) 阈值型健康（`src/app/effects.ts` + `src/infra/health.ts`）

```python
FAILURES_BEFORE_ERROR = 3    # 任何 effect kind 连续失败 3 次才把健康条目置为 error

def report(self, kind):
    row = SELECT COUNT(*) waiting, MIN(since), MAX(attempts),
                 (SELECT error FROM effect WHERE kind=? AND attempts>0 ORDER BY seq DESC LIMIT 1)
          FROM effect WHERE kind=?
    failing = row.attempts >= FAILURES_BEFORE_ERROR and not self.handlers[kind].quiet
    health[kind].set({state: 'error' if failing else 'ready',
                      detail: f"{waiting} waiting since {since}" + (f"; last failure: {error}" if failing else "")})
```
**升级/降级条件**：
- 升级到 `error`：`MAX(attempts) >= 3`（且该 kind 非 `quiet`）。
- 降级到 `ready`：下一次 `dispatch` 成功时 `this.broken.delete(kind)` → `report(kind)` 重算（`src/app/effects.ts` L119）。
- `quiet: true` 的 kind 永不置 error（因为失败已经显示在 Ticket 上）——**这是「避免同一故障报两遍」的机制，很值得抄**。
- 阈值与 Source 轮询失败阈值**刻意共用同一个常量**（`src/app/effects.ts` L6 注释：「matching the Sources poll failure threshold」）。

`HealthBoard`（`src/infra/health.ts`，35 行）是**单写者所有权注册表**：`claim(name)` 若名字已被占则抛异常，`release()` 注销后后续 `set` 变 no-op。**这防止两个部件互相覆盖同一条健康状态。**

Source 侧（`docs/003-execution.md` §Source failure + `src/sources/poll.ts` L18-31）：每次轮询失败在 Thread 上记 `pollError`，成功即清；Source 连续 **3** 次轮询失败才上报 Source 级 error；**任何一次成功都重置计数**；404 也重试（因为「无权访问私有库」也返回 404）；持续读失败的 Issue/PR 在**首次失败后一天**被丢弃。

#### (b) 投影型预算重试（`src/app/retry-health.ts`）

输入是 Session 事件流（`{type, seq, time, data}`），输出是「当前正在等模型预算重试」的会话清单。**不修改重试策略、不读 assistant 消息正文**（文件头部注释明确）。

```python
# 单调水位 + turn/step 三维去乱序
def observe(self, session_id, event):
    if not ordinal(event.seq) or not finite(event.time): return
    if event.seq <= state.seq: return                    # 乱序/重放丢弃
    state.seq = event.seq

    if event.type in ('turn/start','turn/end'):
        if data.turn < state.turn: return
        if data.turn > state.turn: reset(turn=data.turn, step=-1, ended=False, settled=False, active=None)
        if event.type == 'turn/end': state.active = None; state.ended = True
        return

    if event.type == 'llm/retry-started': return          # ← 开始重试 ≠ 已恢复
    if event.type not in ('step/start','llm/retry','assistant/message'): return
    if data.turn < state.turn or (data.turn == state.turn and (state.ended or data.step < state.step)): return

    if event.type == 'assistant/message':
        src = data.message.source
        if (state.active and data.turn == state.turn and data.step == state.step
            and data.interrupted is not True and src.kind == 'model'
            and src.provider == state.active.provider):
            state.active = None; state.settled = True        # ← 恢复判据：同 provider 的模型消息
        return

    if data.turn > state.turn or data.step > state.step:
        reset(turn=data.turn, step=data.step, ended=False, settled=False, active=None)
    if event.type == 'step/start' or state.settled: return

    if not (nonempty str data.retryId and str data.provider
            and int data.retry >= 1): return
    if not budget_failure(data.failure): state.active = None; return   # 非预算类失败 → 清空
    attempts = min(data.retry, MAX_SAFE_INTEGER)
    prev = state.active.status if (state.active.id == data.retryId and state.active.provider == data.provider) else None
    if prev and attempts <= prev.attempts: return           # 同一重试链的重复/倒退
    state.active = Chain(id=data.retryId, provider=data.provider,
                         status={sessionId, provider: safe(data.provider), attempts,
                                 since: prev.since if prev else now, lastAt: now})
```

`budget_failure()` 的**判定优先级**（`src/app/retry-health.ts` L10-23）——这套结构化识别顺序很值得抄：
1. `status ∈ {401, 403, 429}` → **不是**预算（是鉴权/限流，另有处理）
2. `code` 匹配 `/AUTH|RATE_LIMIT|CONTEXT|TOKEN_LIMIT/i` → **不是**预算
3. `type == 'budget_exceeded'` 或 `code == 'budget_exceeded'` → **是**预算
4. 否则尝试把 `message` 前 8192 字符里第一个 `{` 之后当 JSON 解析，取 `body.error` 或 `body`，看 `type`/`code` 是否 `budget_exceeded` → **是**
5. 否则正则 `/\bBudget has been exceeded(?:!|[.:]|$)/i` → **是**
6. 其余一律 **否**

`provider` 落地前被清洗：`/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/` 不匹配则记为 `'unknown'`——**防止把任意字符串写进健康面板**。

恢复判据的双重性：`turn/end` 或 `agent/status == 'idle'` 都会 `clear()`（`src/app/sessions.ts` L133-135），且 `clear()` 只清 `active` 而**保留 seq 水位**（`src/app/retry-health.ts` L39-42），避免乱序事件把已清的状态又激活。

### 5.6 消息不丢：`letGo` / `refuse` / `requeueUntaken`

这是参考实现里最精细的一段，决定「Ticket 结束时没处理完的消息去哪」。**AstrBot 插件一定会遇到同一个问题**（Worker 被 kill / 超时 / 用户 stop）。

```python
# src/core/tickets.ts  end()
def end(self, ticket_id, by, outcome, at=now(), reason=None):
    ticket = require(ticket_id)
    if not live_ticket(ticket): return
    stopping = [a for a in ticket.assignments if a.state != 'ended']
    until = read_until(ticket)                    # 最近一次报告 或 最近一次 turn 结束
    waiting = self.triggers_of(ticket, ticket.undelivered)                # 从没送到 worker 的
    left    = self.untaken(ticket, [i.id for i in ticket.inputs
                                    if i.kind=='trigger' and (not read_before(i, until)
                                                              or i.id in ticket.undelivered)])
    ticket.ended = {at, by, outcome, reason}
    ticket.undelivered = []
    delete ticket.attention; delete ticket.delivery
    for a in stopping: a.state = 'ended'; a.ended_at = at
    save(ticket)
    self.let_go(ticket_id, waiting, left)
    emit('ticket.ended', ...)
    for a in stopping: executor.stop(ticket, a, reason or outcome)

def let_go(self, ticket_id, waiting, left):
    # 1) 把该 ticket 从这些 trigger 的 fanout 里剔除（它不再算「到达过」）
    for trg in [*waiting, *left]:
        d = disposition(trg.id)
        if ticket_id in d.fanout: save_disposition(trg.id, {**d, fanout: d.fanout - {ticket_id}})
    # 2) 「left」（它决定过但没读完的人类消息）重新进 Eval——除非已有别的 owner / 被 ack 过 / Decision 已落定
    for trg in left:
        d = disposition(trg.id)
        decision = decisions.get(d.decision_id) if d.decision_id else None
        stopped = decision and decision.kind=='stop' and decision.ticket_id==ticket_id and not d.handbacks
        decided = decision and (settled(decision) or stopped)
        if not owners(trg) and not d.acked and not decided:
            queue_for_eval(trg)

# settled(): stop / unavailable 都不算「已定案」；reason 含 stopNotExplicit / stopUnknown / routed 也不算
settled = lambda d: d.kind not in ('stop','unavailable') \
    and not any(r.code in ('stopNotExplicit','stopUnknown','routed') for r in d.reason)
```

**`readBefore()`** 是判「worker 到底看没看到」的核心（`src/core/tickets.ts` L19-20）：
```python
def read_before(input, until):
    if input.read_at is None:      # 未记录读取语义的旧数据
        return (input.delivered_at if input.delivered_at and input.delivered_at > input.at else input.at) <= until
    return bool(input.read_at) and input.read_at < until
```
配合 `untaken()`（`owners(trigger)` 含本 ticket 且 `takenUp()` 为假），就实现了 `docs/003-execution.md` §Follow-ups and context Threads 的规范：「An Input counts as read after the Session takes it into a step and subsequently reports or ends a turn... Read messages left without action, such as thanks, do not [return to Eval].」

`requeueUntaken()`（`src/core/tickets.ts` L333-337）补另一个洞：**新 Assignment 启动时，把「已投给被替换的 Assignment 但其 Session 没取走」的 input 插回队首**，且要求「新 delivery 是唯一副本」（`docs/003-execution.md` §Restart 末段）。

**移植建议**：这一段必须整套搬，不能只搬一半。核心是三个字段 `read_at / delivered_to / undelivered` 的语义与 `read_until` 的定义，缺任何一个都会导致「消息静默丢失」或「消息被处理两遍」。

---

## 6. Gate（确定性层）与 Assessment（模型层）的代码边界

### 6.1 职责对照表

| 维度 | Gate（准入） | Assessment（研判） |
| --- | --- | --- |
| 实现位置 | `src/policy.ts` `settingsPolicy().gate()` | `src/app/assessment.ts` `MasterAssessor` |
| 形态 | **纯函数**，同步，无 I/O，无模型 | 异步，有 I/O，**唯一调模型处** |
| 输入 | `Trigger, Thread, Ticket[]` + 闭包里的 `Settings`/`SourceScope` | `Candidate {evalId, thread, triggers, strength, policyVersion, tickets}` |
| 输出 | `{admissible:false, reason:str}` 或 `{admissible:true, strength:Strength, basis:MandateBasis}` | `AssessmentOutcome {kind: ignore|unavailable|accept|stop, reason, audit, ...}` |
| 决定什么 | **谁**可以发起工作、以什么**强度** | **要不要**做、做什么（title/scope）、命中哪条规则 |
| 能不能拒绝 | 能（返回 reason 码，不抛异常） | 能（`ignore` / `unavailable`） |
| 能不能授权 | **能**（产出 `basis`，是 Mandate 的种子） | **不能**（`accept` 只是请求，由 Gate 复核后才成 Mandate） |
| 失败语义 | 无「失败」，只有 `admissible: false` | `unavailable` + 重排队 / 需人工 |

端口定义原文（`src/core/ports.ts` L29）：
> `/** What the assessor receives: bounded facts, never authority. */`

**这句话是整个架构的支点**：模型拿到的 `Candidate` 里没有任何写权限、没有 Policy 内部、没有其它 Ticket 的 Mandate，只有「这一批 Trigger + 这个 Thread 的快照 + 这个 Thread 上所有 Ticket（含已结束的，用来判重）+ 策略版本号」。

### 6.2 调用链（谁调谁）

```
Source.receive(trigger)
   └─► Engine.receive ─► Intake.receive ─► queueForEval
                                            │
Engine.evaluateDue（每 2s + 每次 receive）  │
   └─► Evals.start ◄───────────────────────┘
         ├─ policy.gate(trigger, thread, tickets)        ← Gate，纯函数，逐条
         ├─ 写 thread.evaluating = Evaluation（含 strength/basis/actor）
         └─ assessor.request(candidate)
               └─► effects.enqueue('assessment', evalId, {triggerId})   ← 落库
                     └─► Effects.dispatch → deliver 卡片到主会话
                           └─► engine.candidateDelivered(evalId)         ← deadline 从此刻算
                                 │
                                 ▼
                        主会话推理 → 调 orchestrator_eval_decide
                                 │
                    AssessmentSchema 校验（zod）
                                 │
                    assessmentOutcome(assessment, settings, strength)    ← 纯函数，把模型输出转成域指令
                                 │
                                 └─► Engine.decide(triggerId, outcome)
                                       └─► Evals.decide
                                             ├─ 已有 accepted → 返回旧 Decision（幂等闸 3）
                                             ├─ 无 Eval 持有 → directEval()（**再过一次 Gate**）
                                             ├─ 校验 policyVersion 是否仍是最新 → 否则 reassess
                                             └─ decideEval → finish → record(Decision)
                                                   └─ accept → tickets.create(...)
```

**关键点**：Assessment 的输出**必须回到 `Engine.decide`**，不能直接写库。`decide` 内部会：
- 重新核对 `policyVersion`（`if self.policy().version != evaluation.policy_version: reassess(...)`，`src/core/evals.ts` L285-288）
- 对 `stop` 重新校验强度与目标（L293-301）
- 用 **Evaluation 里 Gate 算好的 `strength`/`basis`/`actor`** 铸造 Mandate，**而不是用 Assessment 声称的**：
```python
basis = {**evaluation.basis, **({'rules': outcome.rules} if outcome.rules else {})}
mandate = Mandate(strength=evaluation.strength, policy_version=evaluation.policy_version,
                  basis=basis, actor=evaluation.actor, scope=outcome.scope)
```
即：**模型只能提供 `scope` 与 `rules`，不能提供 `strength`/`actor`/`policy_version`。** 这是防止提示词注入提权的结构性保证。

### 6.3 Gate 内部判定顺序（`src/policy.ts` L191-211，逐行）

```python
def gate(trigger, thread, tickets):
    if not trigger.human: return machine_gate(trigger)          # ① 机器消息走独立分支

    allowed = allowed_sender(settings, trigger.actor)            # ② 查该 Source 的 allowed 名单
    scope   = listened(sources, trigger.source, trigger.meta)   # ③ 查 Source 接收范围
    row     = scope.row if scope.listened else None
    recorded = {source, scope: trigger.meta, row?, sender?}

    if allowed and trigger.addressed:                            # ④ 名单内 + 指向 agent → explicit
        return admissible(strength='explicit', basis={...via:'addressed'})

    cell = admission_cell(bool(allowed), bool(trigger.addressed))  # ⑤ 选表单元
    receivable = bool(trigger.addressed) or (scope.listened and scope.observed)
    admission  = admission_of(settings, source, row, cell) if receivable else 'ignore'

    if admission == 'explicit':                                  # ⑥ **先判表格 explicit**（防止追问弱化明确请求）
        return admissible('explicit', basis={...via:'admission'})

    if not (trigger.addressed=='direct' and not allowed) and (allowed or mode!='off') and participating(thread, tickets):
        return admissible('explicit' if allowed else 'inferred', basis={...via:'followup'})   # ⑦ 追问路径

    if admission == 'inferred' and mode != 'off':                # ⑧ 表格 inferred
        return admissible('inferred', basis={...observation:{mode}})

    return {'admissible': False, 'reason': 'not_a_member' if not allowed
              else ('admission_ignored' if receivable and admission=='ignore' else 'no_mention')}
```

`machine_gate()`（L171-188）：`actor.id` 非空但不在 bot 名单 → `not_human` 拒绝；在名单 → 必须 `addressed` 或 `scope.observed`，且 `mode=='off'` 时拒绝；无 `actor.id`（纯系统/CI）→ 只能用 `unlistedUnaddressed` 单元且须为 `inferred`。

`participating(thread, tickets)`（L131-132）：该 Thread 是否有「本 thread 自己的」Ticket（排除 context-only 订阅）——决定是否走追问路径。默认值表在 `src/admission.ts`：
```python
ADMISSION_DEFAULTS = {'listedUnaddressed': 'inferred', 'unlistedAddressed': 'ignore', 'unlistedUnaddressed': 'ignore'}
```

### 6.4 Assessment 输出的**硬校验与软降级**

`assessmentOutcome()`（`src/policy.ts` L40-61）——**这是「模型输出 → 域指令」的翻译器，纯函数，无 I/O**：

| 情况 | 处置 | 是抛异常还是降级 |
| --- | --- | --- |
| `rule_assessments` 未对每条 `enabled` 规则**恰好一次**覆盖 | **throw** | 硬失败 → `unavailable` |
| `decision != 'accept'` | 原样返回 `ignore`/`unavailable` + audit | —— |
| `accept` 但 `directionAllowed()` 为假（未表达实现意图 / 目标未定 / 仍有设计决策） | **降级** `ignore(reason=directionUnsettled)` | 软降级 |
| `accept` 但无匹配规则 | **降级** `ignore(reason=noRuleMatched)` | 软降级 |
| `accept` 但缺 `intent_evidence` | **throw** | 硬失败 |
| `accept` + `strength=='inferred'` + `mode != 'auto'` | **降级** `ignore(reason=observed, audit.would_accept=true, audit.proposal={title,scope,name})` | 软降级（observe 模式的核心） |
| 正常 | 返回 `accept` | —— |

`directionAllowed()`（`src/policy.ts` L26-29）是三个布尔的结构化合取：
```python
implementation_requested and target_basis != 'undetermined' and not requires_design_decision
```
**这是把「够不够格开工」拆成可校验的结构化字段、而不是让模型自由发挥的范例**，AstrBot 完全可以照搬成「任务是否明确/目标是否可验证/是否需要产品决策」。

### 6.5 失败降级链（Assessment 侧，完整）

| 失败点 | 降级动作 | 依据 |
| --- | --- | --- |
| 卡片投递失败（Session 不可达） | effect 重试 `[2000, 60000]`，并发 4 | `src/app/assessment.ts` L101 |
| 卡片一直没送达 | **deadline 不启动 → 永不超时** | `src/core/evals.ts` `due()` L43 |
| 主会话超时（默认 120s，可配 30–600s） | `expire()` → 重排队；连续第 3 次 → `unavailable` | `REQUEUES=2`；`LIMITS.evalTimeoutSeconds` |
| 模型调用报错 | `unavailable`（`docs/002` §When Eval does not finish） | docs |
| 规则覆盖不全 / 缺 evidence | throw → 上层记 `unavailable` | `src/policy.ts` L44-54 |
| 决策时 Policy 版本已变 | `reassess()` → 立即 `unavailable` 并按**新版本**重排队 | `src/core/evals.ts` L285-288 |
| Policy 变更事件 | 所有进行中的 Eval `reassess` | `src/core/evals.ts` L218-221 |
| 处理暂停 | `paused_at` 记时，恢复时补回 deadline | `src/core/evals.ts` L39-42 |
| 人工告警门槛 | `unavailable` 且 <24h 且仍是该 Thread 最新 Decision 且未 dismiss 且该 Thread 不在排队/评估中 | `docs/002` 末段 |

`LIMITS`（`src/limits.ts`）里的相关数字：`evalTimeoutSeconds {min:30, max:600}`（默认 120）、`ruleName 100`、`ruleInstruction 2000`、`maxWorkers {min:1, max:16}`（默认 3）、`prepareCommand 4000`。

---

## 7. 会话编队与并发（对应插件的 Router/Worker 隔离）

### 7.1 两种 Session，两种预设

`src/app/sessions.ts`：
```python
WORKER_PRESET = 'dsh-superagent-worker'   # 执行器
ROUTER_PRESET = 'dsh-superagent-router'   # 主会话（orchestrator）
```
- Worker Session 命名 `worker-<ticket id>`，目录名 = Ticket 短名（`docs/003-execution.md` §The Executor）。
- **"Sessions retain the preset selected at creation"** —— 预设不可中途更换。
- Worker 的工具集是白名单（`WORK_TOOLS`，`src/app/sessions.ts` L61-62）：`read/write/edit/read_image/glob/grep/bash/pwsh/job_output/job_list/job_kill/web_search/web_fetch/skill/todo_write/subagent/list_subagent_models/send_message/list_agents/interrupt_agent`。**明确排除**：向人提问/展示的、改配置的、调度工作的、起 goal 的、启停或给其它 Session 发消息的、编辑文档的（同上注释）。
- 主会话的**防等待守卫** `noWait()`（L77-86）：拒绝含 `sleep` 的 bash、拒绝 `timeoutMs > 30000` 的 bash、拒绝 `job_output wait` 超 5s。理由：**一个停在等待上的 step 会挡住后续候选的投递**（`NO_WAIT` 常量注释）。这是一个非常实用的「常驻 Router 不许阻塞」机制。

### 7.2 Worker 槽位与并发（`docs/003-execution.md` §Concurrency）

- 槽位从 **launch 开始（含 prepare）** 或从「唤醒 idle Executor 的那次投递」开始持有；整个活跃 turn 期间持有。
- 配置上限 `maxWorkers`（默认 3，范围 1–16）作用于这些槽位。
- **额外保留 1 个槽位**给「人的消息」或「状态查询」——正常槽位满时可占用额外槽，且**优先于新 launch 拿下一个空槽**。规范原话：「New work therefore cannot prevent a person from continuing an existing Ticket.」
- 其它 Input（如 CI 结果）只能等普通槽位；launch 在 `working / queued` 等待，因此**慢的 prepare 只延迟自己的 Ticket**。

### 7.3 后台工作与 turn 结束语义

- turn 结束时仍有 shell job / subagent 在跑 → **保持槽位并维持 `working`**，不上报 `waiting`（`docs/003-execution.md` §Background work and worker slots）。
- 后台工作上限 **30 分钟**：到点 kill shell job 并上报 `waiting`；**subagent 允许继续**。
- 交还给人（`ready` 报告或提问，或 `blocked`/`failed` 报告）**立即释放槽位**；此时 subagent 停止，shell job 可继续直到 Assignment 结束且**不唤醒** Executor。
- Assignment 结束时：kill shell job（不唤醒）、释放 subagent、丢弃其完成唤醒；**替换用的新 Assignment 在其关闭完成后才开始**。

### 7.4 重启恢复（`src/core/assignments.ts` `interrupted()`）

```python
busy    = running.delivered_at is not None and report is not None and running.delivered_at >= report.at
settled = report is not None and report.state in ('waiting','ready','blocked','failed') and not busy
if not settled: tickets.restart(ticket, running, 'hostRestarted', RECOVERY_NOTE)
elif ticket.undelivered: executor.deliver(ticket, running)     # 保留 Session 与状态，只补投递
```
即：**「已 settled 且无新投递」→ 原地续；否则重启一个 Assignment 并塞 recovery note。** 恢复按 Ticket 最旧优先。

---

## 8. 参考实现**没有**的东西（避免过度拟合）

依据 `docs/000-philosophy.md` §What follows for the design：「**Few concepts.** The design has only its domain concepts... A business need becomes an attribute of one of them, or a prompt the agent follows. The design does not add a concept for it.」

| 插件想要的东西 | 参考实现的做法 | 判定 |
| --- | --- | --- |
| **经验沉淀 / 记忆库** | **不存在实体**。`src/core/memory.ts` 只是「内存版仓储（测试用）」。可复用的只有：`Policy.rules`（带 `id/name/enabled/instruction` 的结构化规则表，随 `policy_version` 参与哈希）+ `prompts/team/*.md`（scope/delivery/communication 三类团队约定，按 Ticket 分节注入）+ `skills/`（`src/app/skill-files.ts`，运行时技能注册）。 | 这是**参考实现的真实空缺**，必须在插件里自建。但注意：规则表本身已经是「经验的形式化载体」——把「这类请求该怎么办」写成 `Rule` 而不是写进一个自由文本记忆库，能被 Gate 校验、能被 `policyEffects` 追踪「规则变了哪些 Ticket 受影响」。**建议经验沉淀先落成 Rule 表 + 审计，而不是向量库。** |
| Principal / Membership 实体 | **拒绝**（`docs/001-domain.md` §Rejected concepts）：「A person is Trigger metadata; an allowed list is a Source's part of the Policy; a cross-Source mapping is prompt text」 | 插件里不要建 `User` 表做跨平台身份映射。允许名单是**每个 Source 各自一份**，身份不互相映射。 |
| Thread 上「最多一个活跃 Ticket」 | **拒绝**：「One Thread can carry several independent pieces of work」 | 不要加这个约束。防重复靠 Assessment 读该 Thread 全部 Ticket。 |
| Eval 的「等待中 / 已撤销」状态 | **拒绝**：「Waiting belongs to Sources; an Eval that runs out of time queues its Triggers again, or ends `unavailable`」 | 状态机里不要给 Eval 加 `waiting`。要等就用一个 Source 去等。 |
| Ticket 状态跟随 Session 状态 | **拒绝**：「a Ticket can be handed off, resumed, or run by several Executors, so it defines its own state」 | **这条对本插件特别重要**：Ticket 状态不能由 AstrBot 的会话对象反推。 |
| 跨源「同一个人」推断 | **拒绝**，属提示词文本 | 同上。 |
| 独立「ExecutionHistory」表 | 无。= `assignments[]` + `decisions.by_thread` | 如需独立表，是**推断性扩展**。 |

---

## 9. 未通读部分（诚实边界）

已精读并据此下结论：`docs/000`–`003`、`src/core/{model,ids,engine,evals,intake,tickets,assignments,stage,ports,part,reasons,replies,memory}.ts`、`src/policy.ts`、`src/admission.ts`、`src/limits.ts`、`src/app/{effects,outbox,delivery,passes,assessment(前120行)}.ts`、`src/app/retry-health.ts`、`src/infra/health.ts`、`src/app/store.ts`(表结构)、`src/sources/{kind,poll}.ts`、`src/app/sessions.ts`(前160行)。

以下**未读**，相关结论若涉及则已在正文标注为「推断」或改用文档证据：
- `src/app/sessions.ts` 后 192 行（prompt/skill 装饰细节、KV.prompts 版本记录）
- `src/app/{resources,http,store-read,migration,settings,notices,cards,links,source-context,prompts}.ts`
- `src/sources/{registry,github*,feishu*,host,admission-context}.ts`（`registry.ts` 已下载但仅 grep）
- `src/executor/{dsh,background}.ts`、`src/app/tools/*`、`docs/004`–`008`
- `tests/*`（**这是一份被低估的资源**：`tests/core-engine.spec.ts` 106KB、`tests/runtime.spec.ts` 142KB、`tests/policy.spec.ts` 45KB，是机制语义的可执行规格，插件实现时值得按测试名对照）

---

## 对插件开发的直接含义

1. **先把 Gate 和 Assessment 拆成两个文件、两种纯度，再谈别的。** Gate（`src/policy.ts` 形态：纯函数、输入 `(trigger, thread, tickets)`、输出 `{admissible, strength, basis}` 或 `{admissible:false, reason}`）决定「谁有权开工」，Assessment 决定「要不要做」。插件里 `strength`/`actor`/`policy_version` **只能来自 Gate，不能来自模型输出**——这是抗提示词注入提权的结构性防线，比任何提示词加固都可靠。

2. **`Mandate` 做成一经创建不可变的冻结 dataclass，字段就照 `{strength, policy_version, basis, actor, scope}` 五个。** 三条变更规则：accept 创建、人类 stop 撤回、`revalidate` 失败撤销。没有任何「更新」路径；Worker 的一切行为以 `mandate.scope` 为界。这直接解决「Router 会话被重启/换模型/被注入后凭空多出工作」的问题。

3. **Eval 串行化不要用锁或队列，用 `Thread.evaluating` 单槽 + `pending` 只追加 + `start()` 排空取批。** 「同一 Thread 最多一个 Eval」= `evaluating` 非空就不 `start`；「期间到达合并进下一批」= `start` 里一次性 `trigger_ids = pending; pending = []`，合并窗口自动等于当前 Eval 的存活时长，零定时器。阈值照抄：连续 **3** 次超时才记 `unavailable`（`REQUEUES=2`），未送达主会话的 Eval **永不超时**，暂停期间 deadline 顺延。

4. **所有 id 用内容哈希派生，`key(*parts) = sha256(json(parts))[:32]`，父 id 进子 id。** `ticket_id = key('ticket', eval_id)`、`decision_id = key('decision', eval_id)` 一行就买到「一个 Eval 只产出一个 Ticket」，比任何分布式锁都便宜。扇出 input 的键 `key('input', ticket_id, 'trigger', trigger_id)` 让重复投递天然幂等；语义前缀（`'attached'/'note'/'restart'/'resumed'`）制造合法的「同父不同义」新行。持久层再加主键 + `ON CONFLICT DO UPDATE ... generation+1` 兜底。

5. **可靠投递三件套必须整套搬：落库的 effect 队列（先落库后执行）+ 逐 (ticket, conversation) 顺序闸 + `read_at/delivered_to/undelivered` 三元组。** 退避参数照抄：Outbox `5s→300s`、其它 effect `2s→60s`、环境性阻塞用 `Later` 异常**不增加失败计数**、连续 **3** 次真失败才置健康为 error、`quiet` kind 不置 error。丢弃只在「Source 不再监听」或「目标永久不可达」时发生且**保留记录**。这三件缺一件，AstrBot 里就会出现「消息丢一半」或「同一条消息处理两遍」——后者在 Worker 有副作用时是灾难。

---

*报告完。证据可复核：仓库 MIT，`README.md` 明确声明「Research prototype for reference only, not an official DeepSeek product... Review and adapt it before using it in your own environment.」，本报告只抽取机制与数值，未复制任何提示词正文。*
