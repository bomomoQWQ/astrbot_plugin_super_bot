# 01 · Agent 运行时（流 A）

> 基线：AstrBot **v4.28.1**（`ab42c0d9b`，2026-09-14），路径相对 `<AstrBot 仓库>`。
> 所有结论均来自源码实读；无证据的推断标注「推断」。

---

## 1. 三条跑道：插件让 agent 跑起来的三种方式

这是本次调查最重要的一张表。**插件不是只有一种方式驱动 agent。**

| | 跑道 A：pipeline 主 agent | 跑道 B：`tool_loop_agent()` | 跑道 C：`request_llm()` 借嘴 |
|---|---|---|---|
| 入口 | 第 7 站 `AgentRequestSubStage` | `Context.tool_loop_agent()`（`star/context.py:215`） | `event.request_llm(...)`（`platform/astr_message_event.py:425`）→ yield 出去 |
| 谁能用 | 只能由 pipeline 触发 | 插件直接调 | 插件 handler 里 yield |
| 自带工具 | 是（配置的） | ✅ `tools: ToolSet` 参数 | ✅ `tool_set` 参数 |
| 自带上下文 | 否（读 DB） | ✅ `contexts: list[Message]` | ✅ `contexts` 参数 |
| 指定会话 | 否 | 否 | ✅ `conversation: Conversation` |
| **流式输出** | ✅ | ❌ **拿不到增量** | ✅ |
| 结果落库 | ✅ 自动 | ❌ 要自己调 `update_conversation` | ✅ 自动（指定 conversation 时） |
| 走装饰链 | ✅ | ❌ | ✅ |
| 取消 | 软/硬停都支持 | 只有 `task.cancel()` | 同跑道 A |

**关键差异（源码证据）**：

跑道 B **不支持流式**。`star/context.py:322-323`：

```python
async for _ in agent_runner.step_until_done(max_steps):
    pass                      # ← 中间的增量全部丢弃
llm_resp = agent_runner.get_final_llm_resp()
```

而跑道 C 走 `AgentRequestSubStage`，是生成器，增量会逐段 `yield` 到下游 `ResultDecorate` / `Respond`。

> **由此得到一条清晰的分工，正好对应 Router 与 Worker 的差别：**
> **Router（人要看它回话）用跑道 C** —— 要流式、要落库、要装饰；
> **Worker（后台干活，人看不到过程）用跑道 B** —— 不要流式，但需要精确控制 contexts。

---

## 2. "借嘴"是官方能力，不是野路子

`platform/astr_message_event.py:425-479`，docstring 给的例子就是：

```python
yield event.request_llm(prompt="hi")
```

完整签名：

```python
def request_llm(self, prompt: str, func_tool_manager=None, tool_set: ToolSet | None = None,
                session_id: str = "", image_urls=None, audio_urls=None,
                contexts: list | None = None, system_prompt: str = "",
                conversation: Conversation | None = None) -> ProviderRequest
```

两个细节值得记：

- **`conversation` 参数**（`:457`）：「对话的人格会被用于 LLM 请求，并且结果将会被记录到对话中」。→ 这是把 Router 的回复纳入 DB 历史、并且套用人格的官方途径。
- **`contexts` 与 `conversation` 互斥**（`:466-467`）：传了 contexts 就会把 conversation 置空。→ **想要持久化就别传 contexts，想要自定义上下文就别指望自动落库。**

消费端在 `pipeline/process_stage/stage.py:40-42`：

```python
if isinstance(resp, ProviderRequest):
    event.set_extra("provider_request", resp)
    async for _ in self.agent_sub_stage.process(event):
```

生产端在 `star_request.py:48-50`：handler 的产出被原样 `yield` 上去，不做类型过滤。**所以 yield ProviderRequest 能一路传到 ProcessStage 并被识别。**

### 2.1 补：为什么 `yield event.request_llm(...)` 一定有效

`pipeline/context_utils.py:47-61` 的 `call_handler` 是真正的分拣处：

```python
async for ret in ready_to_call:
    if isinstance(ret, MessageEventResult | CommandResult):
        event.set_result(ret)
        yield                    # ← 不带值
    else:
        yield ret                # ← 原样上传
```

所以：

| handler 产出 | 走向 | 结果 |
|---|---|---|
| `event.plain_result(...)`（MessageEventResult） | `set_result` + `yield`（空） | 下游发送它 |
| `event.request_llm(...)`（ProviderRequest） | `yield ret` 原样上传 | `ProcessStage` 识别 → 交给 `AgentRequestSubStage` |
| 什么都不 yield（裸 `return`） | `context_utils.py:62-64` 兜底 `yield` 一次 | 下游无事可做 |

### 2.2 关键机制：`_has_send_oper` 是在"洋葱中间"被置真的

很多人会误判这里。`ProcessStage` 的判定在 `stage.py:56-66`：

```python
if (not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm):
    if (event.get_result() and not event.is_stopped()) or not event.get_result():
        ... 跑主 agent ...
```

而 `_has_send_oper` 只在 `event.send()` / `send_streaming()` 里置真（`astr_message_event.py:297,500`），**发送发生在第 9 站**（`RespondStage`），在第 7 站的判定之后……

**那为什么 handler yield 一条回复后主 agent 不会跑？** 因为**洋葱模型**：`ProcessStage` 是生成器，它 `yield` 的瞬间，调度器就把后面的 `ResultDecorate` + `Respond` 整条跑了一遍（`scheduler.py:51-63`）——**回复在这时已经发出去了**，`_has_send_oper` 变真，然后控制权回到 `ProcessStage`，判定时才读到 True。

所以三条规则是精确的：

| 你想要 | 写法 | 为什么 |
|---|---|---|
| **吞掉事件，不让主 agent 参与** | `yield` 一个 `MessageEventResult`（哪怕一句"收到"） | 洋葱中间已发送 → `_has_send_oper=True` |
| **让主 agent 用你的请求回答** | `yield event.request_llm(...)` | 不 set_result、不发东西 → `_has_send_oper` 仍为假 → 主 agent 跑，且用你的 ProviderRequest |
| **放行给原生流程** | 裸 `return` | `call_handler` 兜底 yield 一次空的 |

> ⚠ 反例：`event.stop_event()` 之后**什么都不 yield**。此时 `get_result()` 为 None → `stage.py:63` 的 `or not event.get_result()` 为真 → **主 agent 照样跑**。这是最容易踩的坑，表现是"我明明 stop 了，它还是回了一句"。

---

## 3. 停止/取消：三层机制，语义不同

| 层 | 机制 | 生效时机 | 证据 |
|---|---|---|---|
| **软停止** | `event.set_extra("agent_stop_requested", True)` | **下一步轮询时**，能收尾、能落库 | `astr_agent_run_util.py:26-27` `_should_stop_agent()` = `is_stopped() or agent_stop_requested` |
| **硬停止** | `event.stop_event()` | 当前 stage 返回后立即中断传播 | `pipeline/scheduler.py:56 / 66 / 76` |
| **任务级** | `task.cancel()` → `CancelledError` | 协作式，**必须自己在 except 里收尾再 raise** | asyncio 语义 |

`ActiveEventRegistry` 把前两者包成了按 UMO 分桶的统一入口（`utils/active_event_registry.py`）：

- `stop_all(umo)` → 对所有活跃事件调 `stop_event()`（**会中断传播**）
- `request_agent_stop_all(umo)` → 只设标记 + 调 callback（**不中断传播，历史仍会保存**）

**对插件的直接要求**：Worker 也要登记进同一个 registry（用它被触发时那个 event 作 key），否则用户 `/stop`、WebUI 停止都停不掉后台任务——而"以为停掉了其实没停"比"停不掉"更糟。

> 一个结构性副作用：registry 的 key 是 **event 对象**，分桶按 `umo`。所以**定时发起、没有 event 的 Worker 登记不进去**。这与 `tool_loop_agent` 需要 event 是同一个根因：**event 是 AstrBot 里 Agent 的身份载体。**

---

## 4. hooks 只有 4 个点（不是"每步"）

`agent/hooks.py` 全文只有 30 行、4 个方法：

```python
async def on_agent_begin(run_context)                                   # :12
async def on_tool_start(run_context, tool, tool_args)                  # :13-18
async def on_tool_end(run_context, tool, tool_args, tool_result)        # :19-25
async def on_agent_done(run_context, llm_response)                     # :26-30
```

**拿不到原始 `messages` 列表**，但能拿到**工具级证据**（工具名 / 参数 / 结果）。

→ 所以 01 之前假设的"用 agent_hooks 攒完整消息列表"要下调预期：
**持久化粒度的真实上限 = 回合（prompt + 最终回复）+ 工具调用证据链**，不是完整消息流。

这个粒度够用吗？对"接着干同一件事"够——因为工具证据里已经有"我调了什么、拿到什么"。对"逐字复盘"不够。

---

## 5. 上下文落库是显式调用，插件可以做同样的事

主 agent 把整轮消息写回数据库就在这一处（`pipeline/process_stage/method/agent_sub_stages/internal.py:644-649`）：

```python
await self.conv_manager.update_conversation(
    event.unified_msg_origin,
    req.conversation.cid,
    history=message_to_save,      # list[dict]，由 Message(...).model_dump() 拼成
    token_usage=token_usage,
)
```

含义有三层：

1. **消息格式是可读的 `list[dict]`**，不是黑盒；
2. **插件自己的 Router 会话也可以用同一个 API 落库** —— 跑完 `tool_loop_agent` 后把历史写进 `conv_manager`，于是 Router 的对话在 WebUI 里可见、也能被 `conv.history` 读回。**这是 Router 持久化的现成方案，不用自己造一套。**
3. 反过来：**如果你不调它，直调 `tool_loop_agent` 的对话不会进历史**（跑道 B 的第 6 行"结果落库：要自己调"）。

---

## 6. 用量统计与中止状态已经在库里（成本闸门有数据源）

`db/po.py:43` 有 `ProviderStat` 表；写入在 `db/__init__.py:113` `insert_provider_stat(...)`，调用点在 `internal.py:688-696`：

```python
status = "aborted" if agent_runner.was_aborted() else ("error" if final_resp.role == "err" else "completed")
await db_helper.insert_provider_stat(
    umo=..., conversation_id=..., provider_id=..., provider_model=...,
    status=status, stats=stats.to_dict(), agent_type="internal",
)
```

三件事：

- `agent_type` 字段**天然可扩展** → 你的 Worker 用 `agent_type="super_bot_worker"` 就能分开统计；
- `stats` 里有 token 用量 → **成本闸门不用自己埋点**；
- `was_aborted()`（`tool_loop_agent_runner.py:1504`）是现成的中止查询。

---

## 7. 待验证（本次未解决）

| # | 问题 | 现状 |
|---|---|---|
| 1 | `tool_loop_agent(agent_context=...)` 复用是否跨调用保有 runner 状态 | 推断**不能**，`agent_context` 只是 config/event 注入（`star/context.py:287-291`）。若要"同一次调用内多轮"必须在一次 `tool_loop_agent` 里给足 contexts |
| 2 | 跑道 B 能否拿到流式 | **已确认不能**（`star/context.py:322-323` 丢弃增量） |
| 3 | `request_llm` 产出 ProviderRequest 之后，`conversation` 与 `contexts` 都为空时的行为 | 会读当前 UMO 的默认会话（推断，需实测） |
| 4 | 插件能否给 `ProviderStat` 加自己的 `agent_type` 而不改核心 | 参数已存在，但 `internal.py` 是私有模块；插件需自己调 `insert_provider_stat`（公开 db 方法） |

---

## 8. 工具体系：`FunctionTool` / `ToolSet` 是公开 API

`astrbot/api/__init__.py:5` 直接 import，并在 `__all__`（`:73-83`）里导出：

```python
from astrbot.api import FunctionTool, ToolSet   # 公开，不需要碰 core 私有路径
```

`ToolSchema` 三个必填字段：`name` / `description` / `parameters`（JSON Schema，构造时用 `jsonschema` 校验，`tool.py:32-37`）。

`FunctionTool` 的结构（`tool.py:40-74`）：

| 字段 | 说明 |
|---|---|
| `handler: Callable[..., Awaitable[str \| None] \| AsyncGenerator[MessageEventResult, None]]` | **handler 优先**；不写 `call()` 也能用。返回 `str` 或异步生成器（可流式） |
| `handler_module_path` | MCP 来源的工具为空 |
| `active: bool` | AstrBot 专用，可忽略 |
| **`is_background_task: bool`** | **声明这是一个后台工具**：立即返回 task id，真正的工作异步继续 |

### 8.1 `is_background_task=True` 就是文章 4.1 的 `start()` / `stop()`

`tool.py:61-65` 的注释原文：

> Declare this tool as a background task. Background tasks return immediately with a task identifier while the real work continues asynchronously.

配套机制在 `astr_agent_tool_exec.py`：`_execute_background`（`:478-515`）执行 + `_wake_main_agent_for_background_result`（`:517-...`）**完成后唤醒主 agent**，做法是构造 `CronMessageEvent` + 读 `conv.history` + 直接把结果塞进 `system_prompt`。

**结论：全双工异步工具在 AstrBot 里是「给工具打个标记」的事，不用自己实现。** 这是本次调查最大的意外收获。

### 8.2 `ToolSet` 的能力（做 Worker 会用到）

`add_tool`（同名时优先 `active=True`）/ `remove_tool` / `get_tool` / `names` / `merge(other)` / `func_list`，
以及两个裁剪视图：

- `get_light_tool_set()`（`:121`）—— 轻量视图
- `get_param_only_tool_set()`（`:141`）—— 只保留参数

→ **给 Worker 配工具时，可以直接在 `get_full_tool_set()` 的结果上裁剪**，不必从零拼。

### 8.3 `astrbot.api` 还导出了 `sp`

`api/__init__.py:4` `from astrbot.core import html_renderer, sp`，并在 `__all__` 里。`sp` 即 `SharedPreferences` —— **持久化有官方公开入口**（细节见流 B 的报告）。

---

## 9. 合成事件：没有真实消息时怎么造一个 event

"event 是 Agent 的身份载体"是本调查贯穿始终的约束（`tool_loop_agent` 要它、`ActiveEventRegistry` 要它）。**官方已经给出解法**：`astrbot/core/cron/events.py` 的 `CronMessageEvent`，全文仅 67 行。

```python
class CronMessageEvent(AstrMessageEvent):
    """Synthetic event used when a cron job triggers the main agent loop."""

    def __init__(self, *, context, session: MessageSession, message: str,
                 sender_id: str = "astrbot", sender_name: str = "Scheduler",
                 extras: dict | None = None,
                 message_type: MessageType = MessageType.FRIEND_MESSAGE): ...
```

它做了四件关键的事：

| 行为 | 位置 | 意义 |
|---|---|---|
| 构造 `AstrBotMessage` + `PlatformMetadata(name="cron")` | `:28-45` | 满足 `AstrMessageEvent` 抽象基类的全部要求 |
| `self.session = session` | `:48` | **回复地址可任意指定**（一个 UMO） |
| `is_at_or_wake_command = True`、`is_wake = True` | `:50-51` | 视为已唤醒，不会被第 1 站丢弃 |
| 覆写 `send()` → `context_obj.send_message(session, message)` | `:56-60` | **回复直接投到指定会话**，不依赖平台适配器 |
| `send_streaming()` 拆成多次 `send()` | `:62-64` | 合成事件也能"流式"发（只是逐段发） |

**结论：任何没有真实消息的触发器（定时、Webhook、队列里的 Trigger），都可以造一个合成事件当载体** —— 于是它同时获得了身份、回复地址、唤醒状态，以及可被 `ActiveEventRegistry` 登记的能力。

两条落地建议：

1. **照抄这 67 行**到自己插件里改名（如 `SyntheticEvent`）。它在 `astrbot.core.cron` 下，不在 `astrbot.api` 的公开导出里；复制可完全去掉对 core 私有路径的依赖，成本极低。
2. **`sender_id` 默认是 `"astrbot"`** —— 正好可以被 Gate 的"丢弃 self 消息"规则识别，天然防回环。

### 9.1 更正：事件**可以**注入队列

早前我据 `EventBus.dispatch()` 无参数（`event_bus.py:39`）判断"没有公开的事件注入入口"——**不准确**。

`Context` 直接暴露了原始队列：

```python
# star/context.py:733-735
def get_event_queue(self): ...
```

于是插件能 `queue.put_nowait(自己造的合成事件)`，让它**走完完整 pipeline**（含 9 站过滤、装饰链、RespondStage）。

三条注入路径的取舍：

| 方式 | 是否走 pipeline | 适用 |
|---|---|---|
| `context.get_event_queue().put_nowait(event)` | ✅ 全流程 | 想让合成事件也享受过滤/装饰/限流 |
| 直接调 `tool_loop_agent(event=合成事件)` | ❌ | Worker 内部步进，不需要 pipeline |
| `context.send_message(umo, chain)` | ❌（只发） | 纯输出，不产生事件 |

> 注意 `EventBus` 的消费是**每个事件一个 `asyncio.create_task`**（`event_bus.py:39-54`），**跨事件无顺序保证**。

---

## 对插件开发的直接含义

1. **Router 用跑道 C（`request_llm`），Worker 用跑道 B（`tool_loop_agent`）** —— 前者要流式与落库，后者要上下文控制权，互不将就。
2. **Worker 必须登记进 `ActiveEventRegistry`**，否则 `/stop` 与 WebUI 停止按钮停不掉它。
3. **区分软停与硬停**：用户改主意用 `request_agent_stop_all`（能收尾），清场用 `stop_all`；插件自己的 `task.cancel()` 必须 `except CancelledError → 收尾 → raise`。
4. **Router 的历史用 `conv_manager.update_conversation` 落库**，不要另造一套；这样 WebUI 里能看到它在跟谁说了什么。
5. **成本统计复用 `ProviderStat`**，`agent_type` 填自己的值，不要重复埋点。
6. **后台工具用 `is_background_task=True`**，不要自己造 `start()`/`stop()`；AstrBot 的 `_execute_background` + 唤醒机制已经把这套做完了。
7. **工具与工具集从 `astrbot.api` 取**（`FunctionTool` / `ToolSet`），不必 import `astrbot.core.agent.tool`。
