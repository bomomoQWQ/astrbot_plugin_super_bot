# 03 · 触发器全景与 Pipeline 中断条件调查

- 调查对象：`F:\理解痞老板\AstrBot`（AstrBot v4.28.1，commit `ab42c0d9b`，Python 3.13）
- 方式：只读源码，未修改仓库任何文件。
- 证据格式：`相对路径:行号`（相对 AstrBot 根目录）。标注「推断」的条目无直接代码证据。

---

## 0. 速览：到底有哪些东西能让 agent 跑起来

| 触发源 | 是否经过 EventBus/Pipeline | 是否能被插件 handler 看到 | 是否走主 Agent（LLM） | 关键证据 |
|---|---|---|---|---|
| 平台消息（18 个适配器） | 是 | 是 | 视唤醒条件 | `astrbot/core/platform/platform.py:147-149` |
| Dashboard 网页聊天 / `/api/v1/chat`（OpenAPI） | 是（走 webchat 适配器） | 是 | 是 | `astrbot/core/platform/sources/webchat/webchat_adapter.py:283-284` |
| `cron` 的 `active_agent` 任务 | **否**（直接 build_main_agent） | AdapterMessageEvent handler 看不到；**Agent 生命周期钩子能看到** | 是 | `astrbot/core/cron/manager.py:398-541`；`astrbot/core/astr_agent_hooks.py:14-41` |
| Agent 的 `future_task` 工具 → cron | 间接（先写 cron，再由 cron 直接跑） | 否 | 是 | `astrbot/core/tools/cron_tools.py:101-191` |
| 群聊「主动回复」active_reply | 是 | 是 | 是 | `astrbot/builtin_stars/astrbot/main.py:196-265` |
| 插件 `StarTools.create_event` 造事件 | 是 | 是 | 是（可设 `is_wake`） | `astrbot/core/star/star_tools.py:103-134` |
| 内置星「空 @ 等待器」重投事件 | 是 | 是 | 是 | `astrbot/builtin_stars/astrbot/main.py:114-131` |
| 平台 Webhook（统一入口，无需鉴权） | 是（平台自建事件） | 是 | 视平台 | `astrbot/dashboard/api/platform.py:90-119` |

一句话：**只有「平台适配器 commit 出来的事件」会进 pipeline；cron 是唯一的旁路，它完全绕过全部 9 个 stage。**

---

## 1. 事件从哪来：`astrbot/core/event_bus.py` 的完整机制

### 1.1 结构

整个文件只有 83 行，机制极简：

- 构造：`EventBus(event_queue, pipeline_scheduler_mapping, astrbot_config_mgr)`（`astrbot/core/event_bus.py:26-37`）。
- 队列是普通 `asyncio.Queue`，由 `core_lifecycle` 创建并注入：`self.event_queue = Queue()`（`astrbot/core/core_lifecycle.py:210`），随后传给 `PlatformManager`（`:224`）、`Context`（`:243`）、`EventBus`（`:277-281`）。
- `dispatch()` 是一个 `while True` 的消费者（`astrbot/core/event_bus.py:39-54`）：

```python
event: AstrMessageEvent = await self.event_queue.get()
conf_info = self.astrbot_config_mgr.get_conf_info(event.unified_msg_origin)
scheduler = self.pipeline_scheduler_mapping.get(conf_id)
task = asyncio.create_task(scheduler.execute(event))
```

要点：
1. **按 umo 选配置文件**：`get_conf_info(umo)` 决定该事件落到哪个「配置档」的 pipeline（`astrbot/core/astrbot_config_mgr.py:172-177`）；每个 conf_id 一份 `PipelineScheduler`（`astrbot/core/core_lifecycle.py:455-469`）。
2. **每个事件一个 task**，并发执行，**不保证入队顺序 = 完成顺序**（`astrbot/core/event_bus.py:52-54`）。唯一的串行化在 LLM 阶段（见 §3.6）。
3. 找不到 scheduler 就丢弃并打日志（`astrbot/core/event_bus.py:46-51`）。
4. 任务异常只记日志，不影响总线（`astrbot/core/event_bus.py:56-63`）。
5. 启动时机：`_load()` 里 `asyncio.create_task(self.event_bus.dispatch(), name="event_bus")`（`astrbot/core/core_lifecycle.py:301-304`）。

### 1.2 有没有对外/对插件的「投递」接口？

- **没有订阅 API**。`EventBus` 只有 `event_queue` 与 `dispatch()`，不存在 `subscribe/publish/register` 之类方法（见整个 `astrbot/core/event_bus.py`）。`tests/unit/test_event_bus.py` 里的 `test_subscriber_registration` 只是历史测试，不代表现有 API。
- 对插件的事件「投递」实际发生在 pipeline 内部：`star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent, plugins_name=...)`（`astrbot/core/pipeline/waking_check/stage.py:174-177`），再在 `StarRequestSubStage` 里 `call_handler`（`astrbot/core/pipeline/process_stage/method/star_request.py:36-53`）。
- 但插件**能拿到原始队列**：`Context.get_event_queue()`（`astrbot/core/star/context.py:733-735`）。任何插件都可以自己 `put_nowait` 一个事件，效果等价于「伪造一条消息」。

### 1.3 事件对象的构造

- 基类构造：`AstrMessageEvent(message_str, message_obj, platform_meta, session_id)`（`astrbot/core/platform/astr_message_event.py:38-104`）。`unified_msg_origin = str(MessageSession(platform_name, message_type, session_id))`（`:70-74, 105-108`）。
- 通用工厂：`Platform.create_event(message)`（`astrbot/core/platform/platform.py:151-165`），但几乎所有适配器都重写成专用子类（如 `AiocqhttpMessageEvent`，`aiocqhttp_platform_adapter.py:492`）。
- 合成事件：`CronMessageEvent` 手工构造 `AstrBotMessage`（`astrbot/core/cron/events.py:28-51`），并**强制** `is_at_or_wake_command = True`、`is_wake = True`（`:50-51`）——因为 cron 想复用 Agent 而不想被唤醒逻辑拦。

### 1.4 生产者清单（完整）

A. **平台适配器**（全部经 `Platform.commit_event` → `put_nowait`，`astrbot/core/platform/platform.py:147-149`）：

| 适配器 | 证据 |
|---|---|
| aiocqhttp | `.../sources/aiocqhttp/aiocqhttp_platform_adapter.py:510` |
| telegram | `.../sources/telegram/tg_adapter.py:871` |
| slack | `.../sources/slack/slack_adapter.py:437` |
| discord | `.../sources/discord/discord_platform_adapter.py:347, 392` |
| lark | `.../sources/lark/lark_adapter.py:725` |
| dingtalk | `.../sources/dingtalk/dingtalk_adapter.py:792` |
| kook | `.../sources/kook/kook_adapter.py:514` |
| satori | `.../sources/satori/satori_adapter.py:758` |
| misskey | `.../sources/misskey/misskey_adapter.py:315, 343` |
| weixin_official_account | `.../sources/weixin_official_account/weixin_offacc_adapter.py:543` |
| weixin_oc | `.../sources/weixin_oc/weixin_oc_adapter.py:1577` |
| mattermost | `.../sources/mattermost/mattermost_adapter.py:330` |
| line | `.../sources/line/line_adapter.py:514` |
| qq_official | `.../sources/qqofficial/qqofficial_platform_adapter.py:257` |
| qq_official_webhook | `.../sources/qqofficial_webhook/qo_webhook_adapter.py:94` |
| wecom | `.../sources/wecom/wecom_adapter.py:536` |
| wecom_ai_bot | `.../sources/wecom_ai_bot/wecomai_adapter.py:674` |
| webchat（Dashboard 聊天，常驻创建） | `.../sources/webchat/webchat_adapter.py:284`；常驻见 `astrbot/core/platform/manager.py:100-102` |

B. **插件/内置星主动造事件**：
- `StarTools.create_event(abm, platform=..., is_wake=...)` → `adapter.commit_event(event)`（`astrbot/core/star/star_tools.py:103-134`）。这是**官方给的、插件可用的注入通道**。
- 内置星「空 @ 等待器」把收到的下一条消息 `copy.copy(event)` 后重投队列：`self.context.get_event_queue().put_nowait(new_event)`（`astrbot/builtin_stars/astrbot/main.py:125-127`）。

C. **非 EventBus 的「伪生产者」**（不进队列）：
- cron 的 `CronMessageEvent`：只被 `CronJobManager._woke_main_agent` 直接使用（`astrbot/core/cron/manager.py:427-433`）。
- wecom 主动发送为了复用 `event.send`，临时造一个空 event（`.../sources/wecom/wecom_adapter.py:283-294`）——这不是事件触发，只是发送适配。

---

## 2. 平台消息之外的触发源

### 2.1 `astrbot/core/cron/` 完整机制

**调度器**：APScheduler 的 `AsyncIOScheduler`（`astrbot/core/cron/manager.py:9-11, 105`）。`CronJobManager.start(ctx)` 在 `_load()` 中作为 `cron_manager` 任务启动（`astrbot/core/core_lifecycle.py:305-310`），启动时 `scheduler.start()` + `sync_from_db()`（`astrbot/core/cron/manager.py:112-121`）。

**两类任务**：
- `basic`：注册一个 Python handler，`add_basic_job(...)`（`astrbot/core/cron/manager.py:148-173`），执行时 `handler(**payload)`（`:353-360`）。**handler 只存在于内存字典 `_basic_handlers`**（`:106, 170`）。
- `active_agent`：`add_active_job(...)`（`astrbot/core/cron/manager.py:175-204`），到点唤醒主 Agent。

**CronMessageEvent 怎么造**：`_run_active_agent_job` 取 `payload["session"]`，没有就用 `cron:{job_id}` 合成会话（`astrbot/core/cron/manager.py:362-371`），然后把 `cron_job` 元信息塞进 `extras`（`:374-389`），交给 `_woke_main_agent`；后者自己 `CronMessageEvent(context=..., session=..., message=note, extras=..., message_type=...)`（`:427-433`）。

**关键事实：active_agent 完全不经过 pipeline。** `_woke_main_agent` 直接：
```python
conv = await _get_session_conv(event=cron_event, plugin_context=self.ctx)
result = await build_main_agent(event=cron_event, plugin_context=self.ctx, config=config, req=req)
runner = result.agent_runner
async for _ in runner.step_until_done(agent_max_step):
```
（`astrbot/core/cron/manager.py:484, 504-513`）

因此 cron 触发**不经过** `WakingCheck`（即不看 `plugin_set`、不看 `SessionPluginManager` 的会话级禁用）、不看限流、不做内容安全检查、不调用 `EventType.AdapterMessageEvent` 的任何 handler、也不走 `ResultDecorateStage`。它自己判权限：`role = admin if sender_id in admins_id`，且 `cron_payload["origin"] == "api"` 一律 admin（`astrbot/core/cron/manager.py:436-444`）。失败判定看 `runner.state == AgentState.ERROR`（`:515-525`），历史落库走 `persist_agent_history`（`:536-541`）。

**但有一条「只读旁路」是通的**：主 Agent 运行时仍会调用通用事件钩子——`MainAgentHooks.on_agent_begin/on_agent_done` 分别 `call_event_hook(..., OnAgentBeginEvent / OnLLMResponseEvent / OnAgentDoneEvent)`，工具阶段还有 `OnUsingLLMToolEvent / OnLLMToolRespondEvent`（`astrbot/core/astr_agent_hooks.py:13-77`）。`call_event_hook` 用 `plugins_name=event.plugins_name` 过滤（`astrbot/core/pipeline/context_utils.py:91-94`），而 `CronMessageEvent` 未设置该字段 → `None` → **所有插件的生命周期钩子都会收到 cron 触发的 run**（`astrbot/core/platform/astr_message_event.py:99`）。因此插件可以「观测」cron 运行，但**不能**在 handler 层拦截它；`on_agent_begin` 里 `stop_event()` 的返回值也未被执行器采用（`astr_agent_hooks.py:17-21`）。

**持久化**：全部落 DB 表 `cron_jobs`（`astrbot/core/db/po.py:181-209`），字段含 `job_type / cron_expression / timezone / payload(JSON) / enabled / persistent / run_once / status / last_run_at / next_run_time / last_error`。写入路径 `astrbot/core/db/sqlite.py:2504-2530`。启动时 `sync_from_db()` 只恢复 `enabled and persistent` 的任务（`astrbot/core/cron/manager.py:132-146`）。

**粒度与时区**：
- 周期任务：标准 5 段 crontab，`minute hour day month day_of_week`，**最小粒度 1 分钟**（`astrbot/core/cron/manager.py:256-268`）。
- 一次性任务：`run_once=True` 走 `DateTrigger(run_date=run_at)`，ISO 时间戳，精度到秒（`:242-252`）。
- 时区：每任务 `timezone` 字段 → `ZoneInfo`；非法则回落系统时区（`:232-241`）。crontab 星期做了「crontab 周日=0/7 → APScheduler 名字」的归一（`:34-91`）。
- 容错：`misfire_grace_time=30`（`:275`）。

**插件能不能注册自己的定时任务：能，且是官方暴露的。** `Context.cron_manager`（`astrbot/core/star/context.py:167`）。插件可以：
- `await self.context.cron_manager.add_basic_job(name=..., cron_expression=..., handler=my_coro, payload={...})` → 到点只跑你自己的协程，**不碰 Agent、不碰 pipeline**；
- `await ...add_active_job(...)` → 到点唤醒主 Agent（旁路，见上）。

注意一个坑：插件的 basic job handler 是内存对象，若 `persistent=True` 落库，重启后 `sync_from_db` 会因 `job.job_id not in self._basic_handlers` 而跳过并告警（`astrbot/core/cron/manager.py:137-142`）。**推断**：插件应在 `initialize()` 里自己重建 basic job，而不是依赖持久化。

**其他入口**：WebUI 的 `/api/cron/jobs`（`astrbot/dashboard/api/cron.py:72-116`）走 `CronService.create_job`，payload 里 `origin="api"`（`astrbot/dashboard/services/cron_service.py:88-95`），因此该任务到点时事件 `role="admin"`。

### 2.2 内置「未来任务 / 主动型能力」实现位置

| 能力 | 位置 |
|---|---|
| `future_task` 工具（create/edit/delete/list） | `astrbot/core/tools/cron_tools.py:54-62`，实现在 `:101-321`，写库走 `cron_mgr.add_active_job`（`:153-161`） |
| 工具挂载开关 | 配置 `provider_settings.proactive_capability.add_cron_tools`，默认 `True`（`astrbot/core/config/default.py:151-153`），挂载点 `astrbot/core/astr_main_agent.py:1651-1652` → `_proactive_cron_job_tools`（`:1161-1165`） |
| 主动型系统提示 | `PROACTIVE_AGENT_*` 在 `astrbot/core/astr_main_agent_resources.py:91` 与 `:105`，拼进 cron 的 `req.system_prompt`（`astrbot/core/cron/manager.py:488-490`） |
| 主动发送工具 `send_message_to_user` | `astrbot/core/tools/message_tools.py:80-81`；按平台能力挂载 `if event.platform_meta.support_proactive_message`（`astrbot/core/astr_main_agent.py:1654-1661`）；cron 场景按 `delivery_session_str` 挂载（`astrbot/core/cron/manager.py:497-502`） |
| 群聊「主动回复」active_reply | `astrbot/builtin_stars/astrbot/main.py:196-265`（`@filter.platform_adapter_type(ALL)` 收全量群消息，`need_active_reply` 命中后 `yield event.request_llm(...)`）；概率/方法配置在 `astrbot/builtin_stars/astrbot/group_chat_context.py:66-83` |
| 群聊上下文注入 | `astrbot/builtin_stars/astrbot/group_chat_context.py:142-198` |

注意：`active_reply` 这条路径是**经过完整 pipeline** 的，而且它依赖一个「对所有消息都放行的 filter」（`platform_adapter_type(ALL)`，`astrbot/core/star/filter/platform_adapter_type.py:64-69`）来让 `WakingCheckStage` 不 `stop_event`（见 §4.1）。这是「不 @ 也能触发 Agent」的唯一内置机制。

### 2.3 有没有 webhook 入口

**有，但只有三类，且都不是「给插件用的通用 webhook」。**

1. **平台统一 Webhook（无鉴权）**：Dashboard 暴露 `/api/platform/webhook/{webhook_uuid}`（`astrbot/dashboard/api/platform.py:90-119`，legacy 路由 `/api/platform/webhook/{webhook_uuid}` 见 `astrbot/dashboard/server.py:235` 的鉴权白名单）。分发逻辑：按 `webhook_uuid` 找适配器 → `platform_adapter.webhook_callback(request)`（`astrbot/dashboard/services/platform_service.py:42-57`）；基类接口在 `astrbot/core/platform/platform.py:170-185`。适配器内部把它转成消息再 `commit_event`（如 `.../sources/line/line_adapter.py:142-158`）。
   → 这条路 **只能触发「伪造的某平台消息」**，不能直接触发自定义逻辑；要用它必须让平台配置 `unified_webhook_mode: True` 并有 `webhook_uuid`（`astrbot/core/utils/webhook_utils.py:79-80`）。
2. **插件自带 Web API（需 Dashboard JWT）**：`context.register_web_api(route, view_handler, methods, desc)`（`astrbot/core/star/context.py:705-727`），实际路由挂在 `/api/plug/{plugin_path}`（`astrbot/dashboard/api/plugins.py:1506-1512`）。该前缀**不在** `allowed_endpoint_prefixes` 白名单里（`astrbot/dashboard/server.py:225-242`），因此必须登录态。→ 可以当「半公开 webhook」，但要自己处理 token。
3. **OpenAPI `/api/v1/chat`（API Key）**：`POST /api/v1/chat`（`astrbot/dashboard/api/open_api.py:173-188`，前缀 `/api/v1` 见 `astrbot/dashboard/api/router.py:34,38`），`_open_api_chat_response` 在 `auth.via == "api_key"` 时做会话准备后走 `chat_service.build_chat_stream`（`:92-119`），后者把消息塞进 webchat 队列（`astrbot/dashboard/services/chat_service.py:1177-1196`），再经 `WebChatAdapter.handle_msg → commit_event`（`.../webchat_adapter.py:283-284`）进入正常 pipeline。
   → **这是目前唯一「带鉴权、可编程、会走完整 pipeline」的外部触发入口**，umo 形如 `webchat:FriendMessage:webchat!{user}!{session}`（`astrbot/dashboard/services/open_api_service.py:323`）。

---

## 3. Pipeline 的中断条件全景

### 3.0 执行模型（决定「return 算不算中断」）

stage 顺序固定为 9 个（`astrbot/core/pipeline/stage_order.py:3-13`），由 `ensure_builtin_stages_registered` 导入（`astrbot/core/pipeline/bootstrap.py:7-17`），插件**无法**插入自己的 stage（`register_stage` 仅内部使用，`astrbot/core/pipeline/stage.py:13-16`）。

`_process_stages` 是洋葱模型（`astrbot/core/pipeline/scheduler.py:44-80`）：

```python
if isinstance(coroutine, AsyncGenerator):
    async for _ in agen:
        if event.is_stopped(): break
        await self._process_stages(event, i + 1)   # 后续所有 stage 在「yield 点」被插入执行
```

三个必须知道的推论：
1. **普通协程（无 yield）的 stage 不会「进入下一层」**——但如果它只 `await` 完继续循环，后续 stage 仍会一个个被处理（`astrbot/core/pipeline/scheduler.py:71-80`）。`PreProcessStage` 就是纯协程（`astrbot/core/pipeline/preprocess_stage/stage.py` 全文无 `yield`），所以它里面的 `return`（如无 STT provider，`:194`）只是**跳过本 stage 剩下逻辑，pipeline 继续**。
2. **生成器 stage 若不 yield 就 return，后面所有 stage 都不会执行**（因为递归在 `async for` 体内）。例如 `ResultDecorateStage` 在「无 result」时直接 return（`astrbot/core/pipeline/result_decorate/stage.py:130-132`），`RespondStage` 也就不会跑——语义上没问题，但排查时要认清这是「结构性跳过」而非 stop。
3. **每次 yield 都会完整跑一遍下游**：`ProcessStage` 里每 `yield` 一次（`process_stage/stage.py:46,50,66`），ResultDecorate+Respond 就跑一遍。所以一个事件可能被装饰/发送多次。

`execute()` 的收尾：`finally: event.cleanup_temporary_local_files(); active_event_registry.unregister(event)`（`astrbot/core/pipeline/scheduler.py:98-100`）；webchat/wecom 事件在最后补发一个空消息（`:93-95`）。

### 3.1 `WakingCheckStage`（唤醒判定；中断点最多）

文件：`astrbot/core/pipeline/waking_check/stage.py`

会 `stop_event()` 的四种情况：
1. **忽略机器人自己发的消息**：`ignore_bot_self_message` 且 `self_id == sender_id`（`:95-100`）。
2. **某个 filter 抛异常**：给用户回一条错误消息后 `stop_event()`（`:201-211`）。
3. **权限不足且 `raise_error`**：可配置回不回提示，然后 `stop_event()` 并 return（`:213-230`）。注意此处还顺带 `_umo_auto_name_recorder.schedule(event)`（`:229`）。
4. **最终未唤醒**：`if is_wake: schedule(...) else: event.stop_event()`（`:256-259`）。

判定 `is_wake` 的三条来源（任一成立即可）：
- 消息以某个 `wake_prefix` 开头（且群聊首个段不是「@别人」）（`:114-131`）；
- 任意 `At` 机器人 / `AtAll`（除非 `ignore_at_all`）/ 引用回复机器人（`:132-150`）；
- 私聊且 `friend_message_needs_wake_prefix=False`（webchat 永久豁免）（`:151-159`）；
- **或者任意一个插件 handler 的 filter 全部通过**（`:212-233`，`is_wake = True; event.is_wake = True`）。

⚠️ 关键区别：filter 通过只设 `is_wake`，**不设 `is_at_or_wake_command`**（对比 `:128-129`、`:147-149`、`:157-158` 与 `:232-233`）。而默认 LLM 调用和指令匹配都要求 `is_at_or_wake_command`（见 §3.6、§5）。

另外两个易忽略的约束：
- `if len(handler.event_filters) == 0: continue`（`:189-190`）：**没有 filter 的 handler 永远不会被激活**。
- 指令组（`CommandGroupFilter`）的 handler 不进 `activated_handlers`，由父指令接管（`:235-243`）。

### 3.2 `WhitelistCheckStage`

文件：`astrbot/core/pipeline/whitelist_check/stage.py`

- 关闭检查 / 白名单为空 / 平台是 `webchat` → 直接 return（不中断）（`:35-45`）；
- 管理员且开了 `wl_ignore_admin_on_group|friend` → 放行（`:48-59`）；
- 否则 `umo` 与 `group_id` 都不在白名单 → `stop_event()`（`:60-70`）。

### 3.3 `SessionStatusCheckStage`

文件：`astrbot/core/pipeline/session_status_check/stage.py`

- `SessionServiceManager.is_session_enabled(umo)` 为假 → 顺手为无会话的 umo 建一个空会话（issue #2309 workaround），然后 `stop_event()`（`:24-40`）。
- 数据源：`sp` 的 umo 作用域键 `session_service_config.session_enabled`，缺省 True（`astrbot/core/star/session_llm_manager.py:161-186`）。
- **注意**：这一关拦的是所有事件（含指令、含插件 handler）。

### 3.4 `RateLimitStage`（Fixed Window）

文件：`astrbot/core/pipeline/rate_limit_check/stage.py`

- `count <= 0` → 不限流（`:66-67`）；
- 窗口内未超 → 记时间戳继续（`:68-70`）；
- 超限且 `strategy == "stall"` → `await asyncio.sleep(stall_duration)` **卡住**（不 stop，`+:71-82`）；
- 超限且 `strategy == "discard"` → `return event.stop_event()`（`:83-89`）；
- 锁是**每 umo 一把**（`:59`），默认 `time=60, count=30, strategy=stall`（`astrbot/core/config/default.py:69-73`）。
- 对 webchat 没有豁免 → **Router 会话高频收发会被 stall**。

### 3.5 `ContentSafetyCheckStage`

文件：`astrbot/core/pipeline/content_safety_check/stage.py`

- 输入 = 消息文本 + 被引用消息文本（`:31-43`）；策略由 `internal_keywords` / `baidu_aip` 开关决定（`.../strategies/strategy.py:8-29`）。
- 不通过：若 `is_at_or_wake_command` 先 `set_result("...blocked")` 并 `yield`（于是这句提示会被发回去），然后 `stop_event()` + return（`:45-56`）。
- 该 stage 也被 `ResultDecorateStage` 复用来检查**模型回复**（`astrbot/core/pipeline/result_decorate/stage.py:91-99, 140-156`）。

### 3.6 `PreProcessStage`

文件：`astrbot/core/pipeline/preprocess_stage/stage.py`（纯协程，无 yield）

- 无 `stop_event()`；唯一的提前 `return` 是 **STT 开启但当前会话没有可用的 STT provider**（`:189-194`）——只跳过剩余预处理，pipeline 继续（见 §3.0 推论 1）。
- 其余是副作用：`pre_ack_emoji`（`:55-75`）、路径映射（`:77-114`）、语音/图片本地化（`:116-180`）、STT 文本回填（`:182-245`）。

### 3.7 `ProcessStage`（插件 handler + 主 Agent 的分流点）

文件：`astrbot/core/pipeline/process_stage/stage.py`

结构：
```python
if activated_handlers:
    async for resp in self.star_request_sub_stage.process(event):
        ...
# 调用 LLM
if not provider_settings.enable: return
if (not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm):
    async for _ in self.agent_sub_stage.process(event): yield
```
（`:36-66`）

- **`provider_settings.enable` 为假 → return**，默认 LLM 完全不跑（`:53-54`）。
- 默认 LLM 的三个前置条件（`:56-61`）：
  1. `not event._has_send_oper`——本次事件**从未调用过 `event.send()`**（`_has_send_oper` 在 `astrbot/core/platform/astr_message_event.py:500` 与 `:297` 被置 True）；
  2. `event.is_at_or_wake_command`——只有 @ / 唤醒前缀 / 私聊成立；
  3. `not event.call_llm`——插件可 `event.should_call_llm(False)` 关掉（`astr_message_event.py:372-377`）。
  外加 `(event.get_result() and not event.is_stopped()) or not event.get_result()`（`:62-64`）。
- 插件 handler 侧（`.../method/star_request.py`）：
  - 逐个 handler 执行，`event.is_stopped()` 就 `break`（`:36-38, 51-52`）；
  - 每个 handler 后 `event.clear_result()`（`:53`）；
  - handler 抛异常 → `call_event_hook(OnPluginErrorEvent)`，若 `is_at_or_wake_command` 则把异常文本作为回复 `yield`，最后 `event.stop_event()`（`:54-74`）。
- Agent 侧（`.../method/agent_request.py`）：
  - `provider_settings.enable` 假 → return（`:40-44`）；
  - **`SessionServiceManager.should_process_llm_request(event)` 假 → return（`:46-50`）**。这条对「插件 handler 自己产出的 `ProviderRequest`」同样生效，因为 `ProcessStage` 也把它送进同一个 sub stage（`stage.py:44`）。→ **会话级 LLM 开关能掐掉插件发起的 LLM 调用。**
- 内部 Agent 侧（`.../agent_sub_stages/internal.py`）的静默退出点：
  - 消息为空且无 `provider_request`、无媒体、无引用 → `return`（`:184-191`）；
  - follow-up ticket 已被消费 → `return`（`:200-210`）；
  - `OnWaitingLLMRequestEvent` 钩子把事件 stop → `return`（`:217-218`）；
  - 找不到 provider / `build_main_agent` 失败 → 回错误消息后 `return`（`:233-240, 321-329`）；
  - API base 命中黑名单 → 回错误后 `return`（`:336-345`）；
  - `OnLLMRequestEvent` 钩子 stop → `return`（`:352-353`）。
  - 串行锁：`async with session_lock_manager.acquire_lock(event.unified_msg_origin)`（`:220`），按 umo 串行化 LLM 阶段（实现见 `astrbot/core/utils/session_lock.py:16-55`）。
  - 历史保存条件：`if not event.is_stopped() or agent_runner.was_aborted()`（`:517`），且要求 `req.conversation` 存在（`:571-572`）。

### 3.8 `ResultDecorateStage`（发送前装饰）

文件：`astrbot/core/pipeline/result_decorate/stage.py`

全部是 `return`（提前退出 generator），**没有 `stop_event()`**；但**返回即意味着 `RespondStage` 不执行**：
- 无 result / 空 chain → return（`:130-132`）；
- `STREAMING_RESULT` → return（`:134-135`）；
- 内容安全不通过 → 内部的 ContentSafety 会 `stop_event()`（`:151-156` + content_safety stage `:54`）；
- `OnDecoratingResultEvent` 钩子把事件 stop → `return`（`:184-189`）；
- 流式结果（`STREAMING_FINISH`）→ return（`:191-193`）；
- 钩子清空了 result → return（`:196-198`）；
- T2I 渲染抛异常 → return（`:388-392`）。

装饰内容：回复前缀（`:202-206`）、分段（`:208-267`）、TTS（`:269-366`）、T2I（`:368-410`）、长文转合并转发（`:412-424`）、`@`/引用前缀（`:426-445`）。

### 3.9 `RespondStage`（真正发送）

文件：`astrbot/core/pipeline/respond/stage.py`

提前 return（发送跳过）：
- 无 result（`:173-175`）；
- 已因流式结束标记过 → return（`:176-181`）；
- 空 chain → return（`:182-187`）；
- **与 `send_message_to_user` 已发文本重复**且只含 Plain/Reply/At → return（去重，`:188-210`）；
- 流式 → `event.send_streaming(...)` 后 return（`:217-231`）；
- 空消息链 / 只剩 Reply+At → return（`:242-245, 292-301`）；
- 分段时有效链为空 → return（`:268-275`）；
- 发送后钩子 stop → return（`:328-329`）；
- 正常结束 `event.clear_result()`（`:331`）。

发送细节：分段回复按 `log` 或 `random` 计算间隔并 `await asyncio.sleep(i)` 后逐段 `await event.send(...)`（`:276-290`）；Record 强制单独发送（`:261, 280-281`）。

---

## 4. 插件启用的粒度

### 4.1 两层机制，作用完全不同

**第一层：`plugin_set`（配置档级白名单）**

`waking_check/stage.py:166-172` 正是这里：
```python
enabled_plugins_name = self.ctx.astrbot_config.get("plugin_set", ["*"])
if enabled_plugins_name == ["*"]:
    event.plugins_name = None
else:
    event.plugins_name = enabled_plugins_name
```
作用：把「本配置档启用了哪些插件」写到 `event.plugins_name` 上（`None` = 全部）。默认 `["*"]`（`astrbot/core/config/default.py:295`）。

后续所有 handler 检索都用它过滤：
- `get_handlers_by_event_type(..., plugins_name=event.plugins_name)`（`astrbot/core/pipeline/waking_check/stage.py:174-177`、`astrbot/core/pipeline/context_utils.py:91-94`、`astrbot/core/pipeline/result_decorate/stage.py:159-162`）；
- 过滤逻辑在 `astrbot/core/star/star_handler.py:169-184`：按 `plugin.name not in plugins_name → continue`，**但 `plugin.reserved`（系统插件）豁免**（`:182`），且 `OnAstrBotLoaded/OnPlatformLoaded/OnPluginLoaded/OnPluginUnloaded` 四类事件不参与该过滤（`:176-181`）。

**第二层：`SessionPluginManager`（会话级启停）**

- 存储：`sp` 的 umo 作用域键 `session_plugin_config`，形如 `{umo: {"enabled_plugins": [...], "disabled_plugins": [...]}}`（写入见 `astrbot/dashboard/services/session_management_service.py:258-261`）。
- 读取与过滤：`SessionPluginManager.filter_handlers_by_session(event, activated_handlers)`（`astrbot/core/star/session_plugin_manager.py:48-102`）：
  - 找不到插件元数据 → 放行（`:79-83`）；
  - `plugin.reserved` → 放行（`:86-88`）；
  - `plugin.name in disabled_plugins` → 剔除并 debug 日志（`:94-100`）。
- 调用点：`waking_check/stage.py:247-254`，结果写进 `event.set_extra("activated_handlers", ...)`，再由 `ProcessStage` 取用（`process_stage/stage.py:33-38`）。

### 4.2 「某会话把插件禁用，我们的 handler 还会被激活吗？」

**不会被执行，但「事件是否被唤醒」已经受影响。**

分两种情况：
1. 如果插件被 `plugin_set` 排除（或插件未 `activated`）：`waking_check` 的 handler 枚举阶段就看不到它（`star_handler.py:164-184`），`activated_handlers` 里没有它 → 不执行；同时它也**无法**通过 filter 把 `is_wake` 抬起来 → 群聊里不 @ 的消息会直接 `stop_event()`（`waking_check/stage.py:256-259`）。
2. 如果只是该会话 `disabled_plugins` 里被禁用：`waking_check` 的 filter 仍会跑（`activated_handlers` 先收进它，`:232-243`），`is_wake` 仍会被抬高（**因为这一步在会话过滤之前**），然后才在 `:247-251` 被剔除 → **handler 不执行，但这条消息不会因此被 stop**，会继续流向 LLM（若 `is_at_or_wake_command` 成立）。

「推断」：这意味着「会话级禁用」并不能阻止消息继续触发默认 LLM；若 Router 依赖自身 handler 独占消息，必须在 handler 里 `event.stop_event()` / `should_call_llm(False)` 显式收口。

---

## 5. 指令系统

### 5.1 `@filter.command` 的注册与解析

- 装饰器入口就是 `astrbot/api/event/filter/__init__.py:12` → `astrbot/core/star/register/star_handler.py:75-126`。
- `register_command` 生成 `CommandFilter(command_name, alias, None)` 并 `handler_md.event_filters.append(new_command)`（`astrbot/core/star/register/star_handler.py:108, 121-123`）；子指令则挂到父 `CommandGroupFilter`（`:84-96`，且打 `sub_command` 标记 `:112-115`）。
- Handler 注册进 `star_handlers_registry`，`EventType.AdapterMessageEvent`（同上 `:116-120`）；注册表按 `priority` 降序排序（`astrbot/core/star/star_handler.py:19-26`）。
- 指令元数据/改名/改权限/冲突检测在 `astrbot/core/star/command_management.py`：`list_commands`（`:191-226`）、`rename_command`（`:94-143`）、`toggle_command`（`:63-91`）、`update_command_permission`（`:146-188`）、冲突分组 `_group_conflicts`（`:474-481`）。

### 5.2 指令 vs 普通消息的分流判定

**唯一的硬分流点在 `CommandFilter.filter()`：**

```python
if not event.is_at_or_wake_command:
    return False
...
if message_str.startswith(f"{full_cmd} ") or message_str == full_cmd:
```
（`astrbot/core/star/filter/command.py:191-206`；参数解析 `:208-219`，参数写进 `event.set_extra("parsed_params", params)`）

`CommandGroupFilter` 同样要求 `is_at_or_wake_command`（`astrbot/core/star/filter/command_group.py:117-135`），且「只有组名没有子指令」时直接抛 `ValueError` 打印指令树（`:125-133`）——该异常会被 `waking_check` 捕获并回给用户（`waking_check/stage.py:201-211`）。

**`is_at_or_wake_command` 之外的其他判定**（都不是「指令/非指令」分流，而是「是否参与唤醒」）：
- `RegexFilter`：**明确不受 wake_prefix 约束**（`astrbot/core/star/filter/regex.py:9-18`）；
- `EventMessageTypeFilter`：只判消息类型（`.../filter/event_message_type.py:28-32`），不判唤醒；
- `PlatformAdapterTypeFilter(ALL)`：恒 True（`.../filter/platform_adapter_type.py:64-69`）——内置星就是靠它收全量消息；
- `PermissionTypeFilter`：只判角色，`raise_error` 决定要不要回提示（`.../filter/permission.py:26-53`）；
- `CustomFilter`：插件自定义（`waking_check/stage.py:192-200`）；
- 内置指令可被 `disable_builtin_commands` 整体关闭（`waking_check/stage.py:72-74, 178-183`；默认 False，`astrbot/core/config/default.py:300`）。

**因此**：`@filter.command` 在群聊里「不 @ 就不响应」是设计使然；想让 Router 吃到所有群消息，必须另加一个「恒通过」的 filter（regex/ALL），并且知道此时 `is_at_or_wake_command` 仍为 False（默认 LLM 不会自动跑，需在 handler 里 `yield event.request_llm(...)`）。

### 5.3 内置指令清单

全部在 `astrbot/builtin_stars/builtin_commands/main.py`：

| 指令 | 权限 | 证据 |
|---|---|---|
| `help` | 所有人 | `:28-31` |
| `sid` | 所有人 | `:33-36` |
| `name` | ADMIN | `:38-42` |
| `reset` | SHARED_GROUP_ADMIN | `:44-52` |
| `stop` | 所有人 | `:54-57` |
| `new` | SHARED_GROUP_ADMIN | `:59-67` |
| `stats` | 所有人 | `:69-72` |
| `provider` | ADMIN | `:74-83` |
| `dashboard_update` | ADMIN | `:85-89` |
| `set` / `unset` | 所有人 | `:91-99` |

实现分散在 `astrbot/builtin_stars/builtin_commands/commands/`（`admin.py / conversation.py / help.py / name.py / provider.py / setunset.py / sid.py`）。`/reset`、`/stop` 这类会通过 `active_event_registry` 打断该 umo 下的活跃事件（`astrbot/core/utils/active_event_registry.py:52-91`）；`/stop` 走 `request_agent_stop_all`，不置 `stop_event()`，因此历史仍会保存（`:73-91`）。

---

## 6. 回复链：`ResultDecorateStage` vs `RespondStage`，以及两条发送路径

### 6.1 两个 stage 的职责边界

| | `ResultDecorateStage` | `RespondStage` |
|---|---|---|
| 定位 | 「改内容」 | 「真发送」 |
| 是否可能 stop_event | 不主动 stop（只 return；内容安全那支才会 stop） | 不主动 stop（只 return） |
| 典型动作 | 前缀、分段、TTS、T2I、合并转发、`@`/引用、`OnDecoratingResultEvent` 钩子 | 空链校验、去重、逐段发送、`send_streaming`、`OnAfterMessageSentEvent` 钩子、清空 result |
| 证据 | `astrbot/core/pipeline/result_decorate/stage.py:126-445` | `astrbot/core/pipeline/respond/stage.py:169-331` |

### 6.2 `event.send()` 与 `context.send_message()` 的差别

| 维度 | `event.send(MessageChain)` | `context.send_message(session, chain)` |
|---|---|---|
| 实现 | 适配器子类重写（如 `AiocqhttpMessageEvent.send` → `send_message(bot, chain, event=raw_message, ...)`），基类只做 metric + 置 `_has_send_oper` | 遍历 `platform_manager.platform_insts`，按 `platform.meta().id == session.platform_name` 调 `platform.send_by_session(...)` |
| 证据 | `astrbot/core/platform/astr_message_event.py:483-500`；`.../aiocqhttp/aiocqhttp_message_event.py:184-198` | `astrbot/core/star/context.py:614-677`；`astrbot/core/platform/platform.py:134-145` |
| 是否经过 pipeline | 直接发时**不经过**（handler 内调用即发）；作为 result 时才由 `RespondStage` 发 | **完全不经过** pipeline，也不经过 ResultDecorate |
| 是否触发装饰/钩子 | 仅当走 RespondStage 时才有 `OnAfterMessageSentEvent` | 无任何钩子 |
| 是否落「对话历史」 | 落库在 agent 阶段 `_save_to_history`（`internal.py:562-604`），与 send 无关 | **不写对话历史**；仅当群聊且 `group_message_history_enable` 时写平台消息历史（`context.py:648-672`） |
| 是否置 `_has_send_oper` | 是（`astr_message_event.py:500`）→ 会抑制默认 LLM | 否；只有 `send_message_to_user` 工具在「同会话」时手动置（`astrbot/core/tools/message_tools.py:347-348`） |
| 返回值 | `None` | `bool`（是否找到平台，`context.py:673-677`） |
| 上下文依赖 | 需要原始事件（引用回复、`self_id`、群号），webchat 还需队列 | 只需要可持久化的 `MessageSession` 字符串 |
| 顺序/队列 | `RespondStage` 内逐段 `await`，段间 `sleep`（`respond/stage.py:276-283`）；aiocqhttp 对 Node/File 逐段发且 `sleep(0.5)`（`aiocqhttp_message_event.py:145-182`） | 单次 `await platform.send_by_session`，无排队、无重试、无顺序保证 |
| 限流 | 已被 `RateLimitStage` 作用于入站事件 | 完全不受限流管辖 |

补充：
- `context.send_message` 找不到平台时只告警并返回 False（`context.py:674-677`）——**静默失败**，Router 广播必须检查返回值。
- `CronMessageEvent.send` 是先 `context.send_message` 再 `super().send`（`astrbot/core/cron/events.py:56-60`），而基类 `send` 只上传 metric、置标志（`astr_message_event.py:483-500`），所以不会重复发。
- 事件内并发：EventBus 每事件一个 task（`event_bus.py:52`），**跨事件的发送顺序无保证**；唯一串行化是 LLM 阶段的 umo 锁（`internal.py:220`），而 `RespondStage` 不在锁内。

---

## 6.5 未查清 / 边界（避免误用）

以下条目受本轮时间限制**未查清**，已标注实际检索范围，结论请勿当作已确认事实：

1. **`follow_up`（追问票据）机制的完整生命周期未查清**——只确认了它存在于 `astrbot/core/pipeline/process_stage/follow_up.py`，以及 `internal.py:194-210` 处的「ticket 已消费则 return」分支；未追到票据的签发方、TTL 与去重键。检索范围：`process_stage/**`、`grep follow_up`。
2. **`subagent_orchestrator` / `core/subagent_orchestrator.py` 是否构成第 4 类触发源未查清**——只确认它在 `core_lifecycle` 中被初始化并注入 `Context`（`astrbot/core/core_lifecycle.py:238-239, 254`），未读其内部是否自行 `create_event`/`commit_event`。检索范围：`grep subagent`、目录列表；未通读文件。
3. **`live_chat_service` / `chat_projects` 两条 Dashboard 侧链路是否复用 `build_chat_stream` 未逐一核对**——只确认它们同样经 `webchat_queue_mgr`（`astrbot/dashboard/services/live_chat_service.py:558-563`）。若它们绕开队列直接建 Agent，则「外部触发」清单需要增加一行。
4. **`AgentRunner.was_aborted()` 与 `active_event_registry` 回调的精确联动点未查清**——`/stop` 走 `request_agent_stop_all`（`astrbot/core/utils/active_event_registry.py:73-91`），但回调是在哪注册的（`register_agent_stop_callback` 的调用者）未定位。
5. **多配置档（abconf）下的 `plugin_set` 隔离度**：已确认 `event.plugins_name` 来自事件所属配置档（`waking_check/stage.py:166`），但未验证「同一 umo 在不同档间切换」时是否存在缓存/竞态。检索范围：`astrbot_config_mgr.py` 的映射读取函数，未做运行时验证。

---

## 7. 对插件开发的直接含义

1. **把 Router 做成「恒通过 filter + 显式 `request_llm`」的 handler**：`@filter.platform_adapter_type(ALL)`（或 `@filter.regex(r".*")`）让 `WakingCheckStage` 不 `stop_event`，然后在 handler 里自己决定是否 `yield event.request_llm(...)`，并显式 `event.stop_event()` 收口，避免默认 LLM 二次抢答（依据 `waking_check/stage.py:232-259`、`command.py:191-193`、`process_stage/stage.py:56-65`）。
2. **不要指望 cron 帮你唤起插件**：`active_agent` 直连 `build_main_agent`，绕过所有 stage 与 handler（`cron/manager.py:398-541`）；定时唤醒 Worker 请在插件里用 `context.cron_manager.add_basic_job(...)`（`context.py:167`、`cron/manager.py:148-173`），并自己重建以避免重启后 handler 丢失（`manager.py:137-142`）。
3. **必须用 `event.send()` 或 result 链走 `RespondStage` 才能享受装饰/钩子；用 `context.send_message()` 则是「裸发」**——不写对话历史、不置 `_has_send_oper`、不受限流，但要自行检查返回 `bool` 并自己做顺序控制（`context.py:614-677`、`respond/stage.py:328-331`）。
4. **会话级禁用插件不会阻止消息继续触发默认 LLM**（过滤发生在 `is_wake` 之后，`waking_check/stage.py:232-251`），所以 Router 必须在自己的 handler 里主动 `stop_event()`/`should_call_llm(False)`，否则会出现「插件被禁用但 bot 仍在回复」。
5. **对延迟敏感的设计要避开两个 stall 点**：`rate_limit.strategy=stall`（默认 30 次/60 秒，`default.py:69-73`、`rate_limit_check/stage.py:71-82`）与 per-umo LLM 锁（`internal.py:220`）——Router 与 Worker 若共用 umo，会被串行化，建议 Worker 使用独立 umo 或独立会话标识。
