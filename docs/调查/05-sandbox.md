# 05 · 沙箱 / Shipyard Neo 接入调查

> 调查范围：AstrBot v4.28.1 源码部署（`F:\理解痞老板\AstrBot`）+ AstrBotDevs/shipyard-neo 官方文档。
> **路径约定**：本文所有 `相对路径:行号` 均相对 AstrBot 仓库根 `F:\理解痞老板\AstrBot`；网页证据用 `URL#小节`。
> 标注「推断」的段落是基于证据的推理，未经运行验证（本次按要求未启动 Docker、未改仓库）。

---

## 0. 结论速览

| 问题 | 结论 | 关键证据 |
|---|---|---|
| 插件能否独立接 Neo | **能**，而且有两条路：① 直接用官方 Python SDK `shipyard_neo`（AstrBot 自己就是这么做的）② 裸 HTTP 打 `/v1/*` | `astrbot/core/computer/booters/shipyard_neo.py:20-26`、`requirements.txt:55` |
| `get_booter` 能否独立调用 | **能**，前置条件只有三样：一个 `Context` 实例、一个任意 `session_id` 字符串、`provider_settings` 里写好 sandbox 配置 | `astrbot/core/computer/computer_client.py:553-668` |
| 但 `get_booter` 适合我们吗 | **不适合做"每 Ticket 一环境"**：它的隔离粒度是 UMO（会话），不是任务；且 endpoint/token 只能来自全局配置，不能按调用传参 | `astrbot/core/astr_main_agent.py:1631-1632`、`computer_client.py:609-626` |
| 隔离的真实单位 | AstrBot 层 = 一个 UMO（聊天会话）一个常驻 Sandbox；Bay 层 = Sandbox（稳定）+ Session（可被回收重建） | `computer_client.py:22,569`、[`doc/bay_abstract_entities.md`#2.1](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_abstract_entities.md) |
| Windows 可行性 | **可行但不是默认配置**：必须 `driver.docker.socket=npipe://...` + `connect_mode: host_port` + `publish_ports: true`；`container_network` 在"Bay 跑在 Windows 宿主机"时不可用 | `pkgs/bay/app/config.py`#DockerConfig（见 §B.4） |
| `MAX_SHIP_NUM` | **Neo 里不存在**；那是旧版 `soulter/shipyard-bay` 的环境变量。Neo 的限额机制见 §C.4 | `compose-with-shipyard.yml:23,31-32` vs `pkgs/bay/app/config.py`#Settings |
| 最大坑 | AstrBot **从不调用** `keepalive` / `extend_ttl`；TTL 一到 Sandbox 连 Cargo 一起被 GC，长任务必挂 | 全仓库 grep 无命中（见 §A.3） |

---

## A. 本地 AstrBot 侧

### A.1 `get_booter(...)` 完整签名与依赖

**签名**（`astrbot/core/computer/computer_client.py:553-556`）：

```python
async def get_booter(
    context: Context,              # astrbot.core.star.context.Context
    session_id: str,
) -> ComputerBooter:
```

**内部依赖链**（逐行）：

| 行 | 行为 | 依赖 |
|---|---|---|
| `:557` | `config = context.get_config(umo=session_id)` | 需要一个 `Context` 实例；`Context.get_config` 定义在 `astrbot/core/star/context.py:597-612`，内部走 `self.astrbot_config_mgr.get_conf(umo)` |
| `:559-563` | 读 `provider_settings.computer_use_runtime`；`"none"` 直接抛 `RuntimeError` | **全局配置开关**，不是参数 |
| `:565-566` | `sandbox_cfg = config["provider_settings"]["sandbox"]`；`booter` 默认 `"shipyard_neo"` | 全局配置 |
| `:606-626` | `shipyard_neo` 分支：读 `shipyard_neo_endpoint` / `_access_token` / `_ttl` / `_profile`，构造 `ShipyardNeoBooter(...)` | endpoint+token 只能来自配置或 `credentials.json` 自动发现（`:144-200`） |
| `:591` | `uuid_str = uuid.uuid5(uuid.NAMESPACE_DNS, session_id).hex` | 仅对 local/boxlite 有意义；Neo 的 `boot()` 直接丢弃（`shipyard_neo.py:396-397`） |
| `:643-665` | `await client.boot(uuid_str)` → `await _sync_skills_to_sandbox(client)` → 存入 `session_booter[session_id]` | 会顺带做技能同步（`:493-550`，需要本地 skills 目录与 zip 上传） |

**是否依赖全局状态**：**是，两处**：
1. 模块级字典 `session_booter: dict[str, ComputerBooter] = {}`（`computer_client.py:22`）——所有调用方共享；`:569-590` 的"复用或重建"逻辑读的就是它。
2. 配置来自 `AstrBotConfigManager`（`context.astrbot_config_mgr`，`context.py:163,612`）——进程级单例。

**能否在一个插件里独立调用并拿到 booter**：**能**。插件基类 `Star` 持有 `self.context`（`astrbot/core/star/base.py:24,29`），类型正是 `Context`；因此：

```python
booter = await get_booter(self.context, some_session_id)
# 注意：session_id 会被拿去 get_config(umo=...)，
# 非法 UMO 不会抛异常，会 fallback 到 default 配置（astrbot_config_mgr.py:152-165）
```

**前置条件清单**（缺一不可）：

- `provider_settings.computer_use_runtime == "sandbox"`（`computer_client.py:559`）
- `provider_settings.sandbox.booter == "shipyard_neo"`（`:566`，默认值即此）
- `provider_settings.sandbox.shipyard_neo_endpoint` 非空，或置空走 `__auto__` 自动拉起 Bay 容器（`shipyard_neo.py:391-414`）
- 能拿到 token：显式配置，或 `BAY_DATA_DIR` / `../pkgs/bay/credentials.json` / `cwd/credentials.json`（`computer_client.py:144-200`）
- 已安装 `shipyard-neo-sdk>=0.2.0`（`requirements.txt:55`、`pyproject.toml:66`）——否则 `shipyard_neo.py:20-26` 只打 warning，`BayClient` 名字不存在，`boot()` 会 NameError

**模块级函数清单**（`astrbot/core/computer/computer_client.py`，共 711 行）：

| 行 | 符号 | 类型 | 说明 |
|---|---|---|---|
| `:22` | `session_booter` | 变量 | 全局 session→booter 表 |
| `:23` | `local_booter` | 变量 | 本地 runtime 单例 |
| `:24` | `_MANAGED_SKILLS_FILE` | 常量 | `.astrbot_managed_skills.json` |
| `:27-30` | `_CUAIdleState` | dataclass | CUA 空闲回收状态 |
| `:33` | `cua_idle_state` | 变量 | 仅 CUA 用 |
| `:36-43` | `_get_cua_idle_timeout` | 函数 | 读 `sandbox.cua_idle_timeout` |
| `:46-49` | `_clear_cua_idle_state` | 函数 | |
| `:52-88` | `_schedule_cua_idle_cleanup` | 函数 | **唯一的空闲回收机制**，且只对 CUA 生效 |
| `:91-99` | `_list_local_skill_dirs` | 函数 | |
| `:102-135` | `_collect_sync_skill_dirs` | 函数 | 依赖 `star_registry` 全局注册表 |
| `:138-141` | `_normalize_shell_exec_result` | 函数 | |
| `:144-200` | `_discover_bay_credentials` | 函数 | token 自动发现（3 处候选路径） |
| `:203-211` | `_build_python_exec_command` | 函数 | 生成 heredoc 命令（bash） |
| `:214-287` | `_build_apply_sync_command` | 函数 | 技能包落地脚本 |
| `:290-401` | `_build_scan_command` | 函数 | 技能元数据扫描脚本 |
| `:404-409` | `_build_sync_and_scan_command` | 函数 | 兼容旧路径 |
| `:412-416` | `_shell_exec_succeeded` | 函数 | |
| `:419-429` | `_format_exec_error_detail` | 函数 | |
| `:432-445` | `_decode_sync_payload` | 函数 | |
| `:448-454` | `_update_sandbox_skills_cache` | 函数 | |
| `:457-471` | `_apply_skills_to_sandbox` | 异步函数 | |
| `:474-490` | `_scan_sandbox_skills` | 异步函数 | |
| `:493-550` | `_sync_skills_to_sandbox` | 异步函数 | boot 后自动执行 |
| **`:553-668`** | **`get_booter`** | **异步函数（导出）** | |
| `:671-686` | `sync_skills_to_active_sandboxes` | 异步函数 | 遍历 `session_booter` |
| `:689-698` | `get_local_booter` | 函数 | |
| `:701-711` | `shutdown_local_booter` | 异步函数 | |

> 注意：`_sync_skills_to_sandbox` 会在每次新建 booter 时跑一遍（`:648`），里面会 `shutil.make_archive` 打包本地 skills 并 `upload_file` 到沙箱（`:514-523`）。插件复用时**这一步也会发生**，是隐形成本（推断：首次建沙箱会多几百毫秒到数秒）。

### A.2 `ComputerBooter` 抽象接口

`astrbot/core/computer/booters/base.py:10-62`，5 个能力组件 + 4 个生命周期方法：

| 成员 | 签名 | 行 |
|---|---|---|
| `fs` | `-> FileSystemComponent`（property） | `:11-12` |
| `python` | `-> PythonComponent`（property） | `:14-15` |
| `shell` | `-> ShellComponent`（property） | `:17-18` |
| `capabilities` | `-> tuple[str, ...] \| None`（property）；`None` 表示不支持能力自省 | `:20-27` |
| `browser` | `-> BrowserComponent \| None`（默认 `None`） | `:29-31` |
| `gui` | `-> GUIComponent \| None`（默认 `None`） | `:33-35` |
| `boot` | `async def boot(self, session_id: str) -> None` | `:37` |
| `shutdown` | `async def shutdown(self, **kwargs) -> None`（子类可加 `delete_sandbox`） | `:39-47` |
| `upload_file` | `async def upload_file(self, path: str, file_name: str) -> dict`（返回 `success`/`file_path`） | `:49-54` |
| `download_file` | `async def download_file(self, remote_path: str, local_path: str) -> None` | `:56-58` |
| `available` | `async def available(self) -> bool` | `:60-62` |

**各能力组件的真实方法签名**（`astrbot/core/computer/olayer/`，全是 `Protocol`）：

`filesystem.py:8-61` — 7 个方法：

```python
create_file(path, content="", mode=0o644)
read_file(path, encoding="utf-8", offset=None, limit=None)
search_files(pattern, path=None, glob=None, after_context=None, before_context=None)
edit_file(path, old_string, new_string, replace_all=False, encoding="utf-8")
write_file(path, content, mode="w", encoding="utf-8")
delete_file(path)
list_dir(path=".", show_hidden=False)
```

`python.py:8-20` — 1 个方法：`exec(code, kernel_id=None, timeout=30, silent=False, cwd=None)`

`shell.py:8-21` — 1 个方法：`exec(command, cwd=None, env=None, timeout=300, shell=True, background=False)`

`browser.py:8-46` — 3 个方法：`exec(cmd, timeout=30, description=None, tags=None, learn=False, include_trace=False)` / `exec_batch(commands, timeout=60, stop_on_error=True, ...)` / `run_skill(skill_key, timeout=60, stop_on_error=True, include_trace=False, description=None, tags=None)`

`gui.py:8-25` — 4 个方法：`screenshot(path=None)` / `click(x, y, button="left")` / `type_text(text)` / `press_key(key)`

> **坑（签名不一致）**：`PythonComponent.exec` 协议里声明了 `cwd`（`olayer/python.py:17`），但 Neo 实现没有该参数（`shipyard_neo.py:55-61`）。`Protocol` 不做运行时校验，所以只在类型检查/统一封装时暴露：**如果把"包成 LLM 工具"这一层做成统一入口，`cwd` 必须按能力区别对待**。
> **坑（GUI 不可用）**：`ShipyardNeoBooter` 没有覆写 `gui`，继承 base 返回 `None`（`base.py:33-35`）。Ship 运行时只提供 python/shell/filesystem（[`README.md`#1](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/README.md)），**Neo 无桌面 GUI 能力**，只有 browser（Gull）。

### A.3 `ShipyardNeoBooter`：怎么连、怎么建会话、有没有回收

**连接方式：不是裸 HTTP，是官方 SDK。**（`astrbot/core/computer/booters/shipyard_neo.py:20-26`）

```python
from shipyard_neo import BayClient
from shipyard_neo.sandbox import Sandbox
```
import 失败只打 warning，不抛错（`:23-26`）。

**endpoint + access token 来源（4 条路，按优先级）**：

| 来源 | 证据 |
|---|---|
| `provider_settings.sandbox.shipyard_neo_endpoint` / `_access_token` | `computer_client.py:609-610` |
| 留空 → 自动发现 `credentials.json`：`$BAY_DATA_DIR/credentials.json`、`<AstrBot 根>/../pkgs/bay/credentials.json`、`cwd/credentials.json` | `computer_client.py:144-200` |
| endpoint 为空或 `"__auto__"` → 用 `aiodocker` 拉起 Bay 容器（镜像 `ghcr.io/astrbotdevs/shipyard-neo-bay:latest`，容器名 `astrbot-bay`，端口 8114），再读容器内 `/app/data/credentials.json` | `shipyard_neo.py:391-414`、`bay_manager.py:25-28,50-119,147-208` |
| SDK 自身也支持 `BAY_ENDPOINT` / `BAY_TOKEN` 环境变量兜底（AstrBot 未使用） | [`shipyard-neo-sdk/README.md`#environment-variables](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/shipyard-neo-sdk/README.md) |

**建会话流程**（`shipyard_neo.py:396-462`）：

1. `boot()` 完全忽略传入的 `session_id`（`:396-397` `_ = session_id`）——**沙箱命名与 UMO 无绑定关系**。
2. `BayClient(endpoint_url=..., access_token=...)` + `await client.__aenter__()`（`:428-432`）
3. `_resolve_profile()`（`:536-589`）：用户显式配置非空则原样用；空则 `GET /v1/profiles` 并按 `(有 browser, 能力数)` 打分取最优；401/403 立即上抛；默认回退 `python-default`。
4. `create_sandbox(profile=..., ttl=self._ttl)`（`:439-442`）
5. **就绪门**（`:464-534`）：`sandbox.refresh()` 轮询，`READINESS_TIMEOUT = 180s`、`POLL_INTERVAL = 2s`；`failed`/`expired` 或超时会 `sandbox.delete()` 后抛错（`:489-527`）。
6. 组件装配（`:447-454`）：shell / fs / python 必有；`browser` 仅当 `capabilities` 含 `"browser"` 才挂（`:451-454`）。

**生命周期与回收（这是最大的坑）**：

| 机制 | 是否存在 | 证据 |
|---|---|---|
| `shutdown(delete_sandbox=True)` 删除沙箱 | 有，但**只在 boot 失败或 `available()` 判定失效时调用** | `shipyard_neo.py:591-631`；调用点 `computer_client.py:577-581,651-655` |
| 空闲回收（idle timeout） | **AstrBot 侧没有**。`:666-667` 的 `_schedule_cua_idle_cleanup` 条件是 `if booter_type == "cua"` | `computer_client.py:666-667` |
| `keepalive` / `extend_ttl` | **全仓库零调用**（对 `astrbot/` 全量 py 文件 grep `keepalive|extend_ttl|extend_by`，命中的全是 weixin_oc 的 typing keepalive，与沙箱无关） | 本仓库检索结果 |
| 谁真正回收沙箱 | Bay 的 GC（`expired_sandbox` / `idle_session`），或沙箱 TTL 到期 | [`doc/bay_api_v1.md`#7-admin-api](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_api_v1.md) |
| 健康检查 | `available()`：`refresh()` 后 status ∉ {failed, expired} 即健康；**`idle` 也算健康** | `shipyard_neo.py:686-702` |
| 连接关闭 | `shutdown()` 里先 delete（若要求）再 `__aexit__`，且**明确不停止 Bay 容器**（复用） | `shipyard_neo.py:592-631` |

> **推论（重要）**：`shipyard_neo_ttl` 默认 3600s（`default.py:165`）。AstrBot 从不续期 → **一个会话的沙箱在创建后 1 小时被 Bay 回收，工作区（managed Cargo）随之级联删除**（[`doc/bay_api_v1.md`#17-删除沙箱](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_api_v1.md)）。对"长跑 Ticket"这是硬性风险。

### A.4 `_apply_sandbox_tools`：往 ToolSet 塞了什么、能不能复用

位置 `astrbot/core/astr_main_agent.py:1062-1158`，签名：

```python
def _apply_sandbox_tools(
    config: MainAgentBuildConfig,   # 只用 config.sandbox_cfg（:1071-1074）
    req: ProviderRequest,           # 只写 req.func_tool / req.system_prompt
    session_id: str,
) -> None:
```

调用条件：`if config.computer_use_runtime == "sandbox"`（`:1640-1641`）。

**工具清单（真实名字，来自 `astrbot/core/tools/computer_tools/*` 的 `name` 字段）**：

基础 8 个（所有 sandbox 类型都加，`:1082-1089`）：

| 类 | 工具名 | 定义位置 |
|---|---|---|
| `ExecuteShellTool` | `astrbot_execute_shell` | `computer_tools/shell.py:62` |
| `PythonTool` | `astrbot_execute_ipython` | `computer_tools/python.py:84` |
| `FileUploadTool` | `astrbot_upload_file` | `computer_tools/fs.py:805` |
| `FileDownloadTool` | `astrbot_download_file` | `computer_tools/fs.py:871` |
| `FileReadTool` | `astrbot_file_read_tool` | `computer_tools/fs.py:309` |
| `FileWriteTool` | `astrbot_file_write_tool` | `computer_tools/fs.py:406` |
| `FileEditTool` | `astrbot_file_edit_tool` | `computer_tools/fs.py:478` |
| `GrepTool` | `astrbot_grep_tool` | `computer_tools/fs.py:569` |

`shipyard_neo` 专属追加（`:1090-1140`）：

- 条件注册（`capabilities is None` 或含 `browser`）：`astrbot_execute_browser`（`shipyard_neo/browser.py:40`）、`astrbot_execute_browser_batch`（`:98`）、`astrbot_run_browser_skill`（`:163`）
- 恒加 11 个 Neo 技能生命周期工具（`:1130-1140`）：`astrbot_get_execution_history`（`neo_skills.py:75`）、`astrbot_annotate_execution`（`:122`）、`astrbot_create_skill_payload`（`:160`）、`astrbot_get_skill_payload`（`:208`）、`astrbot_create_skill_candidate`（`:235`）、`astrbot_list_skill_candidates`（`:289`）、`astrbot_evaluate_skill_candidate`（`:327`）、`astrbot_promote_skill_candidate`（`:368`）、`astrbot_list_skill_releases`（`:439`）、`astrbot_rollback_skill_release`（`:480`）、`astrbot_sync_skill_release`（`:507`）

同时注入 3 段 system prompt：Neo 路径规则（`:1093-1098`，**必须相对路径**）、Neo 技能生命周期工作流（`:1100-1110`）、通用 `SANDBOX_MODE_PROMPT`（`:1158`，定义于 `astr_main_agent_resources.py:14-23`）。

**可复用性判定（关键）**：

- `_apply_sandbox_tools` 是**模块级私有函数**（下划线开头），非公开 API。技术上可 `from astrbot.core.astr_main_agent import _apply_sandbox_tools`，前置条件是造一个 `MainAgentBuildConfig(tool_call_timeout=<int>, sandbox_cfg=<dict>)`（`:157-206`，`tool_call_timeout` 无默认值）和一个 `ProviderRequest`。**能跑，但等于锁定内部实现，v4.29 一改就断**。
- **更好的复用点**：工具本身是全局注册的 builtin tool，`tool_mgr = llm_tools`（`:1081`）其实就是 `astrbot.core.provider.register.llm_tools`（`register.py:11`）这个进程级单例；而 `Context.get_llm_tool_manager()` 返回的正是同一个对象（`provider/manager.py:76`：`self.llm_tools = llm_tools`；`star/context.py:356`）。所以插件可以：

```python
from astrbot.core.tools.computer_tools import ExecuteShellTool, PythonTool, FileReadTool
tm = self.context.get_llm_tool_manager()
tool = tm.get_builtin_tool(ExecuteShellTool)   # 拿到单例工具对象，绕过私有函数
```

- **但工具的执行依赖 `ContextWrapper[AstrAgentContext]`**：每个工具的 `call()` 第一件事就是 `get_booter(context.context.context, context.context.event.unified_msg_origin)`（如 `python.py:97-100`、`shell.py:105-108`、`fs.py:377-380`）。也就是说**工具的 session_id 被硬绑在 `event.unified_msg_origin` 上**，插件若想要"每 Ticket 一沙箱"，用这些现成工具是做不到的——除非伪造一个假的 `AstrMessageEvent`（推断：脆且脏）。
- **权限门**：所有工具都先过 `check_admin_permission`（`computer_tools/util.py:57-71`），默认 `computer_use_require_admin=True`（`default.py:155`），非 admin 直接拒。插件自建链路可自行决定是否保留这层。

### A.5 olayer → LLM 工具的映射关系

olayer 是**能力协议层**（5 个 `Protocol`，`olayer/__init__.py:1-13`），本身不含任何工具名；映射发生在 `astrbot/core/tools/computer_tools/`：

| olayer 能力 | 方法 | 对应 LLM 工具 | 语义差异 |
|---|---|---|---|
| `python` | `exec` | `astrbot_execute_ipython` | IPython 内核语义，支持富输出（图片经 `data.output.images` 回传，`shipyard_neo.py:66-77`） |
| `shell` | `exec` | `astrbot_execute_shell` | Neo 只支持 `shell=True`（`shipyard_neo.py:105-111` 显式拒绝 `shell=False`）；`background=True` 靠 `build_detached_shell_command` 包装并解析末行 pid（`:120-121,133-152`） |
| `filesystem` | 7 方法 | `astrbot_file_read_tool` / `_write_` / `_edit_` / `astrbot_grep_tool` / `astrbot_upload_file` / `astrbot_download_file` | `search_files` **没有独立工具**，走 `GrepTool`→`search_files_via_shell`（`shipyard_neo.py:199-214`，用 shell grep 实现）；`list_dir` 也无独立工具，由 shell 承担 |
| `browser` | `exec`/`exec_batch`/`run_skill` | `astrbot_execute_browser` / `_batch` / `astrbot_run_browser_skill` | 仅 profile 含 `browser` 时挂载 |
| `gui` | 4 方法 | 仅 CUA 用（`astrbot_cua_screenshot`/`_mouse_click`/`_keyboard_type`，`computer_tools/cua.py:53,111,148`） | Neo 无 gui |

**Neo 的文件路径语义**（必须记住）：所有路径相对沙箱工作区根，**禁止绝对路径**——AstrBot 专门写进 prompt（`astr_main_agent.py:1093-1098`），Bay 侧有强校验直接 400 `invalid_path`（[`doc/bay_error_codes.md`#invalid_path](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_error_codes.md)）。`upload_file` 会把前导 `/` 剥掉（`shipyard_neo.py:662`）来适配这条规则。

### A.6 sandbox 配置项全集

**默认值**（`astrbot/core/config/default.py:154-172`）：

```yaml
provider_settings:
  computer_use_runtime: "none"          # :154  取值 none | local | sandbox（:3761）
  computer_use_require_admin: true      # :155
  sandbox:
    booter: "shipyard_neo"              # :157  取值 shipyard_neo | shipyard | cua（:3773；代码里还认 boxlite，:636）
    shipyard_endpoint: ""               # :158  旧版 Shipyard
    shipyard_access_token: ""           # :159
    shipyard_ttl: 3600                  # :160
    shipyard_max_sessions: 10           # :161  ★旧版专用，Neo 不用
    shipyard_neo_endpoint: ""           # :162  留空/__auto__ = 自动拉起 Bay 容器
    shipyard_neo_access_token: ""       # :163  sk-bay-...；留空自动发现
    shipyard_neo_profile: "python-default"  # :164  留空 = 自动挑能力最全的
    shipyard_neo_ttl: 3600              # :165
    cua_image / cua_os_type / cua_idle_timeout / cua_telemetry_enabled / cua_local / cua_api_key  # :166-171
```

**UI schema**（`default.py:3758-3815`）给出的额外信息：`shipyard_neo_endpoint` 的默认提示是 `http://127.0.0.1:8114`（`:3782`）；`shipyard_neo_access_token` 标 `secret: True`（`:3792`）；`shipyard_neo_profile` 留空时代码走"打分选最优"（`:3801` 与 `shipyard_neo.py:536-589` 一致）。

**读取方**（`computer_client.py:565-626`）——只有这里读，没有第二处。**没有** `sandbox.*` 下的并发上限、CPU/内存、镜像覆盖等键；这些在 Neo 侧的 Profile 里。

**部署编排**：`astrbot/compose-with-shipyard.yml` 是**旧版 Shipyard（bay）**的编排，不是 Neo：

- 镜像 `soulter/shipyard-bay:latest`（`:23`），环境变量 `MAX_SHIP_NUM=10` / `BEHAVIOR_AFTER_MAX_SHIP=reject` / `DEFAULT_SHIP_CPUS=1.0` / `DEFAULT_SHIP_MEMORY=512m`（`:31-37`），挂 `docker.sock`（`:41`），网络 `astrbot_network`（bridge，`:45-48`）。
- **Neo 没有任何随仓库分发的 compose 文件**——必须自己用 shipyard-neo 仓库的 `deploy/docker/`（见 §B.4）。

---

## B. Shipyard Neo 侧

### B.1 Bay API v1 端点全集

来源：[`doc/bay_api_v1.md`](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_api_v1.md)（FastAPI，全部挂在 `/v1`）

**鉴权**（#认证）：`Authorization: Bearer <api_key>`；DB 中无 key hash 且 `allow_anonymous=true` 时免鉴权（可选 `X-Owner`）；key 优先级 `BAY_API_KEY` 环境变量 > `security.api_key` > 首次启动自动生成 `sk-bay-...` 并写 `credentials.json`。
**幂等**（#幂等性支持）：`Idempotency-Key` 头，仅部分写操作（创建沙箱 / 创建 Cargo / 延长 TTL）支持。
**错误体**（#通用约定）：`{"error": {"code", "message", "request_id", "details"}}`。
**分页**（#分页）：`limit`（默认 50，1-200）+ `cursor`，响应含 `next_cursor`。

| # | 方法 | 路径 | 用途 | 关键参数 |
|---|---|---|---|---|
| 1.1 | POST | `/v1/sandboxes` | 创建沙箱（warm pool 优先 claim，未命中则普通创建） | `profile`（默认 `python-default`）、`cargo_id`、`ttl`（null/0=永不过期）；201 返回 `SandboxResponse` |
| 1.2 | GET | `/v1/sandboxes` | 列出（默认不含 warm pool 实例） | `limit`/`cursor`/`status` |
| 1.3 | GET | `/v1/sandboxes/{id}` | 详情；`status=ready` 时额外返回 `containers[]`（name/runtime_type/version/healthy） | — |
| 1.4 | POST | `/v1/sandboxes/{id}/extend_ttl` | 延长 TTL | `extend_by`(int)；过期→409 `sandbox_expired`；无限 TTL→409 `sandbox_ttl_infinite` |
| 1.5 | POST | `/v1/sandboxes/{id}/keepalive` | **只重置空闲计时器**（`idle_expires_at`），不延长 TTL，不隐式启动会话 | — |
| 1.6 | POST | `/v1/sandboxes/{id}/stop` | 停会话（销毁容器）**保留 Cargo**，幂等 | — |
| 1.7 | DELETE | `/v1/sandboxes/{id}` | 永久删除；级联删 `managed=true` 的 Cargo | 204 |
| 2.1 | POST | `/v1/sandboxes/{id}/python/exec` | 执行 Python（IPython kernel，支持图片富输出） | `code`(必填)、`timeout`(默认 30，1-300)、`include_code`、`description`、`tags` |
| 2.2 | POST | `/v1/sandboxes/{id}/shell/exec` | 执行 Shell | `command`(必填)、`timeout`(30，1-300)、`cwd`(相对 `/workspace`)、`description`、`tags` |
| 2.3 | POST | `/v1/sandboxes/{id}/browser/exec` | 浏览器命令（路由到 Gull） | `cmd`(必填)、`timeout` |
| 2.4 | POST | `/v1/sandboxes/{id}/browser/exec_batch` | 批量浏览器命令（整批算一条历史） | `commands[]`、`timeout`(60，1-600)、`stop_on_error` |
| 2.5 | GET | `/v1/sandboxes/{id}/filesystem/files?path=` | 读文件（相对 `/workspace`） | `path` |
| 2.6 | PUT | `/v1/sandboxes/{id}/filesystem/files` | 写文件 | `path`、`content` |
| 2.7 | GET | `/v1/sandboxes/{id}/filesystem/directories?path=` | 列目录 | `path`（默认 `.`） |
| 2.8 | DELETE | `/v1/sandboxes/{id}/filesystem/files?path=` | 删文件 | `path` |
| 2.9 | POST | `/v1/sandboxes/{id}/filesystem/upload` | 上传二进制（multipart） | form: `file`、`path` |
| 2.10 | GET | `/v1/sandboxes/{id}/filesystem/download?path=` | 下载二进制流 | `path` |
| 3.1-3.4 | POST/GET/GET/DELETE | `/v1/cargos[/{id}]` | 外部持久卷管理 | `size_limit_mb`(1-65536) |
| 4.1-4.4 | GET/GET/GET/PATCH | `/v1/sandboxes/{id}/history[/last\|/{exec_id}]` | 执行历史查询与标注 | `exec_type`/`success_only`/`limit`/`offset`/`tags`/`has_notes` |
| 5.x | POST/GET | `/v1/skills/payloads`、`/candidates`、`/candidates/{id}/evaluate`、`/promote`、`/releases`、`/releases/{id}/rollback` | 技能生命周期 | `skill_key`、`source_execution_ids`、`payload_ref`、`stage`(canary/stable) |
| 6.1 | GET | `/v1/profiles` | Profile 只读查询 | `detail=true` 时含 `description` + `containers` |
| 7.1-7.2 | POST/GET | `/v1/admin/gc/run`、`/v1/admin/gc/status` | 手动/查看 GC | `tasks[]`(idle_session/expired_sandbox/orphan_cargo/orphan_container) |
| — | GET | `/health` | 健康检查（**无需认证**） | [`deploy/docker/README.md`#运维](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/deploy/docker/README.md) |

**路径校验**（#路径校验）：不接受绝对路径、`../` 穿越、null 字节。

**状态机**（#1.1 状态说明）：`idle`（无运行会话）/ `starting` / `ready` / `failed` / `expired`。

### B.2 关键错误码语义

来源：[`doc/bay_error_codes.md`](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_error_codes.md)

| code | HTTP | 对客户端的含义 / 该怎么做 |
|---|---|---|
| `session_not_ready` | 503 | 容器冷启动中，`details.retry_after_ms` 指定重试间隔 → **必须重试，不能当失败** |
| `capability_not_supported` | 400 | 该 Profile 不含此能力（`details.available` 给出可用能力）→ 换 profile 或降级 |
| `invalid_path` | 400 | 绝对路径 / `../` / null 字节 → 改成相对 `/workspace` 的路径 |
| `sandbox_expired` | 409 | 沙箱已过期，**不能复活**，只能新建 |
| `sandbox_ttl_infinite` | 409 | TTL 无限，无需 extend |
| `quota_exceeded` | 429 | 配额/速率超限 → 退避重试 |
| `ship_error` | 502 | Ship 运行时错误 → 代码/命令本身的问题 |
| `timeout` | 504 | 执行超时 |
| `not_found` / `file_not_found` | 404 | 资源/文件不存在 |
| `conflict` | 409 | 幂等键请求体不匹配，或删除仍被引用的 Cargo |
| `unauthorized` / `forbidden` | 401/403 | token 错误 |

### B.3 Docker 单机部署要求

来源：[`deploy/docker/README.md`](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/deploy/docker/README.md) + [`deploy/docker/docker-compose.yaml`](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/deploy/docker/docker-compose.yaml) + [`deploy/docker/config.yaml`](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/deploy/docker/config.yaml)

- **前置**：Docker Engine 24+ / Compose v2；镜像来自 GHCR `ghcr.io/astrbotdevs/shipyard-neo-{bay,ship,gull}`。
- **网络模式**：`driver.type: docker` + `driver.docker.connect_mode: container_network`，`network: bay-network`，`publish_ports: false`（sandbox 容器**不向宿主机暴露端口**）。
- **docker.sock**：**必须挂**——`- /var/run/docker.sock:/var/run/docker.sock`（Bay 用它动态创建 sandbox 容器）。README 安全建议里明确"建议用 Docker Socket Proxy"。
- **端口暴露**：只有 Bay 的 `8114:8114` 需要暴露给客户端；sandbox 不需要。
- **环境变量**：`BAY_CONFIG_FILE=/app/config.yaml`、`BAY_DATA_DIR=/app/data`、可选 `BAY_API_KEY`；Gull 服务用 `GULL_MODE=shared`、`GULL_CDP_PORT=9222`、`AGENT_BROWSER_IDLE_TIMEOUT_MS=600000`。
- **AstrBot 联合部署**：overlay 文件 `docker-compose.with-astrbot.yaml`，AstrBot 里填 Endpoint `http://bay:8114`、**访问令牌留空**（通过只读挂载 bay-data 卷 + `BAY_DATA_DIR=/bay-data` 自动发现）。
- **⚠️ 文档自相矛盾**：`README.md`#7.1 说"在 `config.yaml` 中设置 `driver.type: container_network`"，但代码里 `DriverConfig.type` 的 `Literal` 只有 `"docker"` / `"k8s"`，`container_network` 是 `DockerConfig.connect_mode` 的取值。**以 `config.yaml` / `config.py` 为准**（见 §B.4）。

### B.4 Bay 配置模型（权威来源，用于 §C.3/§C.4）

来源：`pkgs/bay/app/config.py`（`https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/pkgs/bay/app/config.py`），类 `DockerConfig`：

```python
socket: str = "unix:///var/run/docker.sock"
network: str | None = None
connect_mode: Literal["container_network", "host_port", "auto"] = "auto"
host_address: str = "127.0.0.1"      # host_port 模式下 Bay 连 runtime 的地址
publish_ports: bool = True          # host_port / auto 回退依赖它
host_port: int | None = None
```

`DriverConfig.type: Literal["docker", "k8s"] = "docker"`。
`ProfileConfig`：`resources`（`cpus=1.0` / `memory="1g"`）、`capabilities`、`idle_timeout=1800`、`warm_pool_size=0`、`warm_rotate_ttl=1800`、`warm_claim_timeout_ms=200`、`browser: shared|isolated|None`。
`CargoConfig`：`root_path="/var/lib/bay/cargos"`、`host_root_path=None`、`default_size_limit_mb=1024`、`mount_path="/workspace"`。
`GCConfig`：`enabled`、`run_on_startup`、`interval_seconds=300`、`instance_id`、4 个子任务（`orphan_container` 默认 **disabled**）。
`SecurityConfig`：`api_key`、`allow_anonymous`（代码默认 true，生产 yaml 设 false）、`Phase 2 blocked_hosts`（含 `172.16.0.0/12`、`10.0.0.0/8`）。
`WarmPoolConfig`：`enabled`、`warmup_queue_workers=2`、`warmup_queue_max_size=256`、`drop_policy=drop_newest`、`interval_seconds=30`。
`IdempotencyConfig`：`enabled`、`ttl_hours=1`。
**`Settings` 顶层字段全集**：`server / database / driver / cargo / security / idempotency / gc / warm_pool / browser_learning / browser_service / proxy / browser_auto_release_enabled / profiles` —— **没有** `max_sandboxes`、`max_ship_num`、并发上限之类的字段。

---

## C. 必须产出

### C.1 插件自建 HTTP 客户端的最小可行调用序列

**先给结论**：不建议裸写 HTTP。AstrBot 本身就是用官方 SDK 的（`shipyard_neo.py:20-26`），SDK 已经封装了重试、幂等、错误类型映射（[`shipyard-neo-sdk/README.md`#reliability--retry-policy](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/shipyard-neo-sdk/README.md)）。下面两版都给，HTTP 版用于"绝对不想加依赖"的场景。

**版本 A：官方 SDK（推荐）**

```python
# 插件 requirements.txt 里加：shipyard-neo-sdk>=0.2.0
# AstrBot 会在插件加载时 pip install 插件目录下的 requirements.txt
# （astrbot/core/star/star_manager.py:353-366）

from shipyard_neo import BayClient, SandboxExpiredError, SessionNotReadyError, QuotaExceededError

async def run_ticket(endpoint: str, token: str, profile: str, ttl: int, code: str):
    # 0) client 生命周期 —— 建议按 Ticket 建、用完 aexit；不要每次调用都建
    async with BayClient(endpoint_url=endpoint, access_token=token,
                         timeout=30.0, max_retries=3) as client:
        sandbox = None
        try:
            # 1) 创建沙箱：ttl 必填，idempotency_key 让创建可安全重试
            sandbox = await client.create_sandbox(
                profile=profile,          # 例 "python-default"
                ttl=ttl,                  # 秒；null/0 = 永不过期（危险）
                idempotency_key=f"ticket-{ticket_id}",
            )
            # SDK 的 create 不保证 READY，冷启动要靠 session_not_ready 重试兜底
            #   等价于 AstrBot 的 _wait_until_ready（shipyard_neo.py:464-534）

            # 2) 执行 python（首次调用会自动拉起容器会话）
            r = await sandbox.python.exec(code, timeout=min(timeout, 300),
                                          description=f"ticket {ticket_id}")
            if not r.success:
                raise RuntimeError(f"exec failed: {r.error}")

            # 3) 读文件（路径必须相对 /workspace，禁止前导 /）
            content = await sandbox.filesystem.read_file("out/result.json")

            # 4) 长任务保命：续命（AstrBot 自己不做这一步）
            await sandbox.keepalive()        # 重置 idle 计时，不延 TTL
            await sandbox.extend_ttl(1800)   # 延 TTL；已过期会抛 SandboxExpiredError

            return content

        except SessionNotReadyError as e:     # 503，冷启动中
            await asyncio.sleep(e.details.get("retry_after_ms", 2000) / 1000); retry()
        except QuotaExceededError:            # 429
            backoff_and_retry()
        except SandboxExpiredError:           # 409，不可复活，只能重建
            recreate_sandbox()
        finally:
            # 5) 销毁：删除沙箱（级联删 managed Cargo）
            if sandbox is not None:
                try:
                    await sandbox.delete()
                except Exception:
                    ...   # Bay GC 可能已回收，容忍
```

**版本 B：裸 HTTP（不引入 SDK 时）**

```python
import httpx

H = {"Authorization": f"Bearer {token}"}      # Bay API v1 唯一鉴权头
BASE = endpoint.rstrip("/") + "/v1"

async with httpx.AsyncClient(base_url=BASE, headers=H, timeout=30.0) as c:
    # 1) 创建（201）
    r = await c.post("/sandboxes",
                     json={"profile": "python-default", "ttl": 3600},
                     headers={"Idempotency-Key": f"ticket-{tid}"})
    if r.status_code == 401: raise AuthError(...)          # unauthorized
    if r.status_code == 429: raise QuotaError(...)          # quota_exceeded，退避
    r.raise_for_status()
    sbx = r.json(); sid = sbx["id"]                         # "sbx_..."
    try:
        # 2) 等 ready（避免每个调用都撞 503）
        for _ in range(90):                                  # 180s / 2s，对齐 AstrBot
            d = (await c.get(f"/sandboxes/{sid}")).json()
            if d["status"] == "ready": break
            if d["status"] in ("failed", "expired"):
                raise RuntimeError(f"sandbox terminal: {d['status']}")
            await asyncio.sleep(2)

        # 3) 执行 python（200）
        r = await c.post(f"/sandboxes/{sid}/python/exec",
                         json={"code": code, "timeout": 60})
        if r.status_code == 503 and r.json()["error"]["code"] == "session_not_ready":
            wait_ms = r.json()["error"]["details"].get("retry_after_ms", 2000)
            await asyncio.sleep(wait_ms / 1000); retry()
        payload = r.json()                # {success, output, error, data, execution_id, ...}

        # 4) 读文件（200）—— path 必须相对 /workspace，绝对路径会 400 invalid_path
        r = await c.get(f"/sandboxes/{sid}/filesystem/files",
                        params={"path": "out/result.json"})
        if r.status_code == 400 and r.json()["error"]["code"] == "invalid_path":
            fix_path()                    # 去掉前导 "/"、去掉 ".."
        content = r.json()["content"]

        # 5) 续命（长任务必做）
        await c.post(f"/sandboxes/{sid}/keepalive")           # 只重置 idle
        await c.post(f"/sandboxes/{sid}/extend_ttl",
                     json={"extend_by": 1800},
                     headers={"Idempotency-Key": f"ext-{tid}-{n}"})
    finally:
        # 6) 销毁（204）
        resp = await c.delete(f"/sandboxes/{sid}")
        # 404 说明已被 Bay GC 回收，视为成功
```

**错误处理要点（两版通用）**：
1. `session_not_ready`(503) 是**常态不是异常**，必须按 `retry_after_ms` 重试。
2. `Idempotency-Key` 只对"创建沙箱 / 创建 Cargo / 延 TTL"有效，**对 exec 无效**——别指望 exec 幂等。
3. 销毁用 `DELETE`；只想释放算力、保留工作区用 `POST /stop`。
4. 客户端侧要自己做 `finally` 清理——**Bay 不会因为 HTTP 连接断开就回收沙箱**。

### C.2 `get_booter` 能否绕过 AstrBot 私有 API 独立使用

**明确结论：能，但只适合"跟 AstrBot 共享同一套沙箱配置与会话键"的场景；不适合"每 Ticket 一环境"。**

能用的理由（均为公开可见的调用面，不涉及私有函数）：

| 前置条件 | 是否满足 | 证据 |
|---|---|---|
| 拿到 `Context` 实例 | ✅ 插件 `self.context` 即 `Context` | `astrbot/core/star/base.py:24,29` |
| `get_booter` 是模块级公开函数（无下划线） | ✅ | `computer_client.py:553` |
| `session_id` 可以是任意字符串（非法 UMO 会 fallback 默认配置，不抛） | ✅ | `astrbot_config_mgr.py:152-165` |
| 配置需已写入 `provider_settings.sandbox.*` | ⚠️ 必须先由用户/插件写全局配置 | `computer_client.py:609-626` |
| 依赖 `shipyard-neo-sdk` 已安装 | ⚠️ 插件可用自己的 `requirements.txt` 装 | `star_manager.py:353-366` |
| 能指定"用哪个 endpoint/token/profile/ttl" | ❌ **不能按调用传参**，只能读全局配置 | 同上 |
| 能绕开 `_sync_skills_to_sandbox` 的额外开销 | ❌ 每次新建 booter 都会跑 | `computer_client.py:648` |

**不适合的四个理由（关键风险）**：

1. **隔离粒度错位**：AstrBot 的键是 UMO（`astr_main_agent.py:1631-1632`：`req.session_id = event.unified_msg_origin`），即"一个聊天会话一个沙箱"，**不是"一个 Ticket 一个沙箱"**。想按 Ticket 隔离只能自己编 `session_id`（`f"{umo}#ticket:{id}"`），会往共享的 `session_booter` 全局字典里塞条目（`computer_client.py:22,665`）。
2. **无自动回收**：`session_booter` 只增不减（除 boot 失败/健康检查失败），插件创建的沙箱会一直占资源直到 Bay TTL/GC 到期。`:666-667` 的空闲回收**只管 CUA**。
3. **会污染主 Agent**：插件塞进 `session_booter` 的 booter，会被 `sync_skills_to_active_sandboxes()`（`:671-686`）遍历到；主 Agent 若用同一 UMO 就会命中插件的沙箱。
4. **多租户不成立**：所有 Ticket 共享同一个 Bay token 与 owner（`allow_anonymous=false` 时 owner 由 key 决定），**Bay 侧没有"按 Ticket 鉴权隔离"的机制**——隔离只能靠"不同 sandbox_id"。若要租户级隔离，需要多个 Bay 实例或自己在上层做 ACL（推断）。

**推荐方案**：插件**直接用 `shipyard_neo` SDK**（版本 A），自己维护 `ticket_id → sandbox_id` 映射与 TTL 续期；只在"想复用 AstrBot 已有工具链"时用 `context.get_llm_tool_manager().get_builtin_tool(...)` 那套（见 §A.4），而那套又硬绑 `unified_msg_origin`，所以两者不要混用。

### C.3 Windows + Docker Desktop 可行性评估

**总评：可行，但默认配置在 Windows 上大概率失败；需要改 3 个参数。**

**障碍 1：`docker.sock` 路径是硬编码 Linux 路径**

- `compose-with-shipyard.yml:41` 与 neo 的 `deploy/docker/docker-compose.yaml` 都是 `- /var/run/docker.sock:/var/run/docker.sock`。
- AstrBot 的自动拉起 Bay 也硬编码了同一路径：`bay_manager.py:106-109`（`"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]`）。
- **在 Windows 宿主机上**：这个路径不存在 → 挂载点创建为空目录 / 报错。Docker Desktop 的引擎 socket 是命名管道 `//./pipe/docker_engine`。
  - **若 Bay 跑在容器里**（推荐）：Docker Desktop 对 container 内的 `/var/run/docker.sock` 是支持的（LinuxKit VM 视角），**compose 那行不用改**。
  - **若 Bay 以原生 Windows 进程运行**（AstrBot 源码部署时更贴近现实）：`driver.docker.socket` 必须设为 `npipe:////./pipe/docker_engine`（`config.py`#DockerConfig 的 `socket` 字段），且无法通过 `Binds` 挂载 —— 也就是说 **AstrBot 的 `__auto__` 自动拉起模式（`bay_manager.py`）在 Windows 上是坏的**（推断：`aiodocker.Docker()` 无参默认走 `unix:///var/run/docker.sock`，Windows 需 `DOCKER_HOST` 显式指定 npipe）。**结论：Windows 上不要用 `__auto__`，必须显式配 endpoint。**

**障碍 2：`container_network` 在"原生 Bay + Docker Desktop"下不通**

- `config.py` 注释写得很清楚：`container_network` 需要"Bay 也在该 network 内"才能用容器 IP 直连；否则用 `host_port`。
- Docker Desktop for Windows 的容器跑在 WSL2 的 LinuxKit VM 里，其 bridge 网络地址（172.x）**对 Windows 宿主机不可路由**（推断，但这是 Docker Desktop 的通行行为）。因此 Bay 若在宿主机，必须：
  ```yaml
  driver:
    type: docker
    docker:
      socket: "npipe:////./pipe/docker_engine"
      connect_mode: host_port      # 或 auto（先试 container_network 再回退）
      host_address: "127.0.0.1"     # publish 的端口在 Windows 上是 127.0.0.1 可达
      publish_ports: true          # 必须开！默认 true，但生产 yaml 里被设成 false
      host_port: null              # 随机宿主端口
  ```
- **副作用**：`publish_ports: true` 会把每个 sandbox 的 runtime 端口（`runtime_port: 8123`）随机映射到宿主机 → 攻击面变大（这与官方"`publish_ports: false` 减攻击面"的建议相反，需要 Windows 防火墙配合）。
- **注意 `blocked_hosts`**：`SecurityConfig.blocked_hosts` 默认含 `172.16.0.0/12`（Phase 2 标注）→ 若日后启用沙箱出网黑名单，`container_network` 的 172.x 直连模式可能与它冲突（推断，当前 Phase 2 未生效）。

**障碍 3：Cargo 存储路径与共享浏览器**

- `cargo.host_root_path` 为 `None` 时用 Docker named volume。官方 `deploy/docker/config.yaml` 为了"共享 Chromium"把 `./bay-cargos` bind 到 `/var/lib/bay/cargos` 并让 `gull-service` 只读挂载 —— **Windows 上 bind mount 路径需用 Windows 风格**，且 Docker Desktop 的文件共享/性能会明显变慢（推断）。
- 想省事：Windows 单机先 **关掉 `browser_service.enabled`**（用 `browser-python` 的 independent Gull），或干脆只用 `python-default`。

**能不能"换个 driver.type"**：**不能**。`DriverConfig.type` 的 `Literal` 只有 `"docker"` 和 `"k8s"`（`config.py`）。K8s 在 Windows 单机上不现实（Docker Desktop 自带 K8s 已弃用）。所以 Windows 上的可行组合只有：
`type: docker` + `socket: npipe:////./pipe/docker_engine` + `connect_mode: host_port|auto` + `publish_ports: true`，
或者 **把 Bay 本身也放进 Docker Desktop 的 Linux 容器里**（这时官方 `deploy/docker/` 原样可用，`container_network` + `/var/run/docker.sock` 都能工作）——**后者是更省心的路线**（推断）。

**还有一个"没启动 Docker"的现实提醒**：`shipyard_neo.py:457-527` 的就绪门等 180s 才超时；AstrBot 不会因为你 Docker 没开就快速失败——它会卡住 180s 再抛错并尝试 `sandbox.delete()`。插件的健康检查应先用 `GET /health`（免鉴权）探活，别直接建沙箱。

### C.4 资源与生命周期参数的真实来源

**先澄清 `MAX_SHIP_NUM`**：它是**旧版 Shipyard（bay）**的容器环境变量，出现在 `astrbot/compose-with-shipyard.yml:31`（镜像 `soulter/shipyard-bay:latest`，`:23`），AstrBot 代码里对应的读取点是 `shipyard_max_sessions`（`computer_client.py:601`，仅 `booter == "shipyard"` 分支用）。
**Neo 完全不读它**：`Settings`（`pkgs/bay/app/config.py`#Settings）字段全集里没有任何并发/数量上限项；Neo 的 `create_sandbox` 也没有 `max` 概念。

| 参数 | 真实来源 | 默认值 | AstrBot 是否暴露 |
|---|---|---|---|
| **沙箱 TTL**（`expires_at`） | `POST /sandboxes` 的 `ttl` 字段（[`bay_api_v1.md`#1.1](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_api_v1.md)） | AstrBot 传 `shipyard_neo_ttl` | ✅ `default.py:165` = 3600s；也可 `null/0` = 永不过期 |
| **空闲回收**（`idle_expires_at`） | Profile 的 `idle_timeout`（`config.py`#ProfileConfig），由 GC 任务 `idle_session` 回收 | **1800s**（代码默认 + 官方 yaml 都是 1800） | ❌ 只能改 Bay 的 `config.yaml` |
| **保活** | `POST /keepalive` 只重置 idle，不延 TTL | — | ❌ AstrBot 零调用 |
| **延长 TTL** | `POST /extend_ttl`（`extend_by`） | — | ❌ AstrBot 零调用 |
| **CPU / 内存限额** | Profile 的 `resources.cpus` / `resources.memory`（`ResourceSpec`） | `cpus=1.0`、`memory="1g"`；`python-data` 是 `2.0` / `4g` | ❌ 只能改 Bay 的 `config.yaml` |
| **工作区容量** | `cargo.default_size_limit_mb` / 创建 Cargo 时的 `size_limit_mb` | `1024` MB（请求体允许 1-65536） | ❌ |
| **并发上限** | **Neo 没有全局并发上限配置**。能起多少个沙箱取决于宿主机资源；错误码里保留了 `quota_exceeded`(429) 但 `Settings` 无对应字段 → **推断：默认不做数量限制，429 预留给未来/反向代理层** | — | ❌ |
| **GC 周期** | `gc.interval_seconds` | 300s；4 个子任务可单独开关，`orphan_container` 代码默认 disabled、生产 yaml 里启用 | ❌ |
| **Warm pool（预热，等价"池上限"）** | `profiles[].warm_pool_size` + 全局 `warm_pool.*` | 代码默认 `warm_pool_size=0`（关闭）；官方 yaml 每个 profile 设 `1`；`warmup_queue_workers=2`、`warm_queue_max_size=256`、`warm_claim_timeout_ms=200`、`warm_rotate_ttl=1800` | ❌ |
| **会话可被透明回收重建** | `Session` 是 ephemeral，`Sandbox` 是稳定 ID（[`bay_abstract_entities.md`#1.1-1.2](https://raw.githubusercontent.com/AstrBotDevs/shipyard-neo/main/doc/bay_abstract_entities.md)） | — | 语义层面 |
| **幂等缓存 TTL** | `idempotency.ttl_hours` | 1 小时 | ❌ |
| **浏览器会话空闲** | Gull 的 `AGENT_BROWSER_IDLE_TIMEOUT_MS` | 600000ms（10min） | ❌ |
| **AstrBot 侧并发** | 无：`session_booter` 是普通 dict，无上限 | — | 无 |

**由此得到的运营含义（推断）**：
- "每 Ticket 一沙箱"的资源上限**只受宿主机约束**，必须由插件自己做池化/排队（例如信号量限 4~8 个并发 Ticket）。
- `idle_timeout=1800 < ttl=3600`：**空闲 30 分钟容器就被回收**，但 Sandbox 与 Cargo 还在；下一个请求会冷启动（AstrBot 的 `available()` 判为健康，因为 status 会回到 `idle`/`starting`，`shipyard_neo.py:686-702`）——用户侧表现为"莫名卡 10~60s"。
- TTL 到期 → Sandbox 被 `expired_sandbox` GC → **managed Cargo 级联删除** → 未落盘到外部存储的产物全丢。长任务必须周期 `extend_ttl`，或改用 `POST /cargos` 创建 `managed=false` 的外部 Cargo 并复用。

---

## 对插件开发的直接含义

1. **不要走 `get_booter` 做 Ticket 隔离**：它的键是 UMO、配置只能来自全局 `provider_settings`、没有空闲回收。插件直接依赖 `shipyard-neo-sdk>=0.2.0`（写进插件自己的 `requirements.txt`，`star_manager.py:353-366` 会自动 pip 安装），自己维护 `ticket_id → sandbox_id` 映射与 TTL 续期，才能做到"一 Ticket 一环境、结束即销毁"。
2. **LLM 工具层要自己写一层薄适配，不要照抄 `_apply_sandbox_tools`**：该函数是私有且依赖 `MainAgentBuildConfig`；而现成的 builtin tool（`astrbot_execute_shell` / `astrbot_execute_ipython` / 6 个 fs 工具 / 3 个 browser 工具）全部把 session 硬绑在 `event.unified_msg_origin`（`python.py:97-100` 等），只有 5 类能力 ~15 个方法值得包成工具（filesystem 7 + python 1 + shell 1 + browser 3；**gui 不存在**，Neo 无桌面）。
3. **生命周期必须插件自己管**：建 → 轮询 ready（对齐 `_wait_until_ready` 的 180s/2s）→ 长任务周期 `keepalive`/`extend_ttl` → `finally` 里 `DELETE /sandboxes/{id}`；并把 `session_not_ready`(503) 当常态重试、`invalid_path`(400) 当编程错误、`sandbox_expired`(409) 当"不可复活只能重建"。
4. **Windows 上把 Bay 也塞进 Docker Desktop 的 Linux 容器里**（用官方 `deploy/docker/` 原样），比"原生 Bay + `npipe` + `host_port` + `publish_ports`"少踩一堆坑；无论哪条路都**别用 AstrBot 的 `__auto__` 自动拉起**（`bay_manager.py:106-109` 硬编码 Linux socket 路径）。沙箱创建前先用免鉴权的 `GET /health` 探活，否则 Docker 没开会白等 180s。
5. **`MAX_SHIP_NUM` 在 Neo 里不存在**，Neo 没有任何全局并发上限配置——并发数只受宿主机资源约束。插件必须**自己实现 Ticket 并发闸门（信号量/队列）**，并为每个 Ticket 设合理的 `ttl` 与续期策略；需要跨会话保数据时用外部 Cargo（`managed=false`），别依赖"沙箱还在"这个假设。
