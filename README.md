<p align="center">
  <img src="logo.png" width="220" alt="Super Bot">
</p>

# Super Bot

> AstrBot 插件 · 把"每条消息各回各的"改造成**有调度感的助手**

装进 AstrBot 后，它会给你的机器人加一层**调度**：先判断该不该理、把同一话题的
几句话合并成一批、需要干活时派工单在沙箱里跑、发不出去就退避重试、
卡住了有人管。

**默认全部安全关闭**：装上不会改变任何现有会话的行为，开关要你自己开。

---

## 它解决什么

AstrBot 默认的链路是"一条消息 → 一次模型调用 → 一次回复"。群聊里这很吵，
多任务时也很难管。这个插件在中间加一层：

```
群消息 ──► Gate（纯程序判定，不烧模型）
             ├─ 自消息 / 指令 / 白名单外 / 超频 ──► 直接丢
             └─ 其余 ──► Router（同一话题合并成一批）
                            └─► Worker（一个任务一张工单，沙箱里执行）
                                    └─► Outbox（同会话串行、失败重试）
                                            └─► 发回群里
```

---

## 特性

| 模块 | 做什么 |
|---|---|
| **Gate** | 四层确定性准入：自消息防回环、指令放行、来源白名单、滑动窗口限流。纯程序判定，**不调模型** |
| **Router** | 单槽 + 只追加的批处理。评估进行期间到达的消息自动合并；不用定时器，也没有"窗口多长"这种拍脑袋参数 |
| **Worker** | 一个任务一张工单。模型在**真沙箱**里调工具干活（`run_python` / `run_shell` / 读写文件），并发上限、协作式取消 |
| **Outbox** | 同一会话串行发送、失败退避重试、幂等入队（重试不会重复刷屏） |
| **Watchdog** | 任务停滞检测 → 退避重试 → 逐级升级 → 判死并通知。不靠"看起来在跑" |
| **Thread 指纹** | 同一话题自动归拢：显式标记 > 引用消息 > 话题 > 按天 |

发送 `/sb` 可以随时看系统状态（排队多少、在跑多少、库里有几条）。

### 三个入口，各管一段

| 入口 | 触发方式 | 谁在判断 | 什么时候用 |
|---|---|---|---|
| **Router** | 群里一条没 @ 也没前缀的消息 | 模型（一次，`max_steps=1`） | 平时的群聊：要不要接话、接什么话 |
| **命令** | `/sb do <任务>` | 不判断，直接派 | 你已经明确要它干活，不想再花一次模型调用去"决定要不要理" |
| **工具** | 原生 agent 自己调 `superbot_dispatch` | 模型（就是 agent 本身） | 私聊 / @机器人 时，agent 觉得"这事得真跑一下"，就把活交给沙箱 |

最后一条是关键：**SB 和路由不分开**。原生 agent 手里多了一把叫
`superbot_dispatch` 的工具，它想动手就调工具，沙箱干完把结果还给它，
它再组织语言回复——这样私聊不会出现"插件回一条、原生又回一条"。

```bash
/sb              看状态
/sb status       同上
/sb do <任务>    跳过判断，直接派给沙箱子代理
/sb help         看说明
```

命令全部收在 `/sb` 下面，不往全局命令空间里扔裸命令。

### 技能（SKILL.md）

AstrBot 自带技能系统：`data/skills/<名字>/SKILL.md`，或插件自带的
`data/plugins/<插件>/skills/<名字>/SKILL.md`，用 `data/skills.json` 的
`{"active": false}` 关掉。这套目录约定**原样沿用**，不另造一套。

本插件补了两个缺口：

| 缺口 | 补法 |
|---|---|
| 框架只把技能清单注入**主 agent**，Worker 一行都没有 | 把同一份清单追加到 Worker 的 system prompt |
| 框架让模型**用 shell 读** `SKILL.md`，`runtime=sandbox` 时等于先开一个沙箱 | 注册 `read_skill` 工具，直接从宿主读正文返回；技能目录里的 `scripts/`、`assets/` 也能按需取 |

```markdown
---
name: my-skill
description: 一句话说清"这技能干什么、什么时候该用"
---

# 正文：具体怎么做
```

启动时会扫一遍：名字不合法、缺 `description`、`SKILL.md` 读不了，都会打 WARN；
`/sb` 里能看到当前有几个技能可用。

插件自己也带了一个技能：`skills/super-bot/SKILL.md`，教模型什么时候该把活派给
沙箱子代理、任务怎么描述、结果怎么用。它跟你的技能平级，改不改随你。

---

## 安装

**方式一：插件市场**（推荐）
在 AstrBot Dashboard 的插件市场里搜索 **Super Bot** 安装。

**方式二：手动**
把仓库放到 `data/plugins/astrbot_plugin_super_bot/`，重启 AstrBot。

> 目录名必须是 `astrbot_plugin_super_bot`（插件加载器按 `metadata.yaml` 的
> `name` 生成目录名，且要求是合法 Python 标识符）。
>
> 开发时可以用目录联接（Windows `mklink /J` / Linux `ln -s`）指向源码，
> 但**正式部署请用复制或 clone**，别依赖软链。

依赖只有两个（`requirements.txt`，安装时自动装）：

```
aiosqlite>=0.22
httpx>=0.27
```

---

## 配置

**在 Dashboard 里配置**（推荐）：安装后打开插件配置页，会看到「准入规则」和「任务执行」
两组设置，改完保存即可，不用重启。

也可以用环境变量覆盖（优先级：**环境变量 > Dashboard 配置 > 默认值**）。
环境变量是给容器编排 / CI 用的覆盖手段；正常使用只改 Dashboard 就行。

| 变量 | 默认 | 作用 |
|---|---|---|
| `SUPERBOT_EVAL_MODE` | `ignore` | `ignore` 只判定不回话 / `echo` 确定性回显（调试用）/ `live` 走真模型 |
| `SUPERBOT_SANDBOX` | 关 | `1` = Worker 在 Shipyard Neo 沙箱里真干活 |
| `SUPERBOT_SANDBOX_PROXY` | 空 | 沙箱出口代理，如 `http://192.168.1.254:5078`；留空＝直连 |
| `SUPERBOT_AGENT_STEPS` | `6` | Worker 的工具循环上限（步数越多越贵） |
| `BAY_ENDPOINT` | `http://localhost:8114` | Bay 地址 |
| `BAY_API_KEY` | 空 | Bay 的 API key |
| `SUPERBOT_KEEPALIVE_SEC` | `1200` | 沙箱保活间隔（**必须**小于 TTL，否则工作区会被级联删除） |

### 配置项参考

Dashboard 表单里用的是**中文短名**，配置文件里用的是英文键名，对照表如下。

**`gate`（准入规则，纯程序判定，不消耗模型）**

| 配置键 | 界面名称 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `bot_ids` | 自身身份 | list | `["astrbot"]` | 额外当成“自己”的身份，用来丢弃机器人自己发的消息（防回环） |
| `wake_prefixes` | 唤醒前缀 | list | `["/"]` | 以此开头的消息视为指令，直接交给 AstrBot 原生流程 |
| `command_names` | 免前缀指令 | list | `[]` | 不用前缀也算指令的首词，一般留空 |
| `allowed_sources` | 来源白名单 | list | `[]` | 允许的来源，格式 `平台:群号`（如 `aiocqhttp:123456`）；留空＝全部放行 |
| `rate_limit` | 限流上限 | int | `30` | 同一来源在窗口内最多放行几条，超出直接丢弃 |
| `rate_window_sec` | 限流窗口 | float | `60` | 限流窗口长度（秒） |

**`agent`（任务执行）**

| 配置键 | 界面名称 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `eval_mode` | 评估模式 | string | `ignore` | `ignore` 只判定不回话 / `echo` 确定性回显（调试）/ `live` 用真模型判断怎么回 |
| `sandbox` | 沙箱执行 | bool | `false` | 开启后 Worker 在 Shipyard Neo 沙箱里真干活 |
| `sandbox_proxy` | 沙箱代理 | string | 空 | 沙箱里跑命令时的出口代理，如 `http://192.168.1.254:5078`；留空＝直连 |
| `agent_steps` | 循环步数 | int | `6` | Worker 的工具循环最多几步，越多越贵 |
| `bay_endpoint` | 沙箱地址 | string | `http://localhost:8114` | Shipyard Neo（Bay）的地址 |
| `bay_api_key` | 沙箱密钥 | string | 空 | Bay 的 API key；留空则读环境变量 `BAY_API_KEY` 或 `.runtime/bay_key.txt` |
| `keepalive_sec` | 保活间隔 | float | `1200` | 沙箱保活间隔（秒），**必须**小于 Bay 的 TTL |

> 环境变量与配置项是同一套开关，优先级：**环境变量 > Dashboard 配置 > 默认值**。

---

## 兼容性

- AstrBot `>=4.28,<5`
- 平台适配器：`aiocqhttp`（OneBot v11）已测；其它适配器走同一套事件抽象，未逐一验证
- Python 3.12 / 3.13

---

## 已知限制

- `SUPERBOT_EVAL_MODE=live` 与 `SUPERBOT_SANDBOX=1` 需要你自己配好 provider 与 Shipyard Neo；
  默认是 `ignore`，装上不会碰模型、也不会开容器
- 沙箱能力依赖外部 Shipyard Neo 部署，不配也能跑（Worker 退回桩执行）
- **重启不续跑**：排队的消息、跑着的工单都在内存里，进程重启就没了。
  重启时会把上一轮没跑完的工单如实记成 `blocked(restart)`——**不假装还在跑**，
  免得你等一个永远不来的回复
- Router 的"要不要理、怎么理"还是很朴素的一句话判断；复杂编排留给后续版本

---

## 文档

本仓库的 `docs/` 里有完整的源码级调研与设计记录，每条结论都带 `file:line` 证据：

- `docs/测试环境.md`：本地怎么跑起来（真 AstrBot + 假模型 + 假 OneBot）
- `docs/沙箱与真模型.md`：沙箱部署、真模型接入、踩过的坑
- `docs/设计/`：架构、P0 规格、决策记录（ADR）

---

## 致谢

**AstrBot** —— 这个插件是长在它上面的。写的时候大量直接读了它的源码来对齐行为
（`Star` 的生命周期、`@filter` 的注册方式、`ToolSet`/`FunctionTool` 的调用约定、
`ActiveEventRegistry` 的停止回调、技能系统的目录约定），每一处结论都在
`docs/` 里标了 `file:line`。没有它这套东西无从谈起。

**Shipyard Neo / Bay** —— 沙箱执行层。Worker 的工具循环跑在它提供的隔离环境里，
`/v1/sandboxes`、`python/exec`、`filesystem`、`keepalive` 这几条接口是插件能"真干活"的前提。

**《Super-agent 设计探索》** —— 本项目的设计出发点。准入、路由、编队、投递这条链路
的取舍基本来自那篇文章，`docs/设计/30-decisions.md` 里逐条记了哪些照做、哪些改了主意。

**《为人民服务》**（我的一个弃坑的项目）—— `superbot/watchdog.py` 连同
`STALL_THRESHOLD_SEC`、`MAX_RETRIES`、`RETRY_BACKOFF_SEC`、`MAX_ESCALATION`、
`WATCHDOG_TICK_SEC` 这五个阈值都源自它。从 TypeScript 重写成了 Python，语义保持一致
（唯一的主动偏离见 `docs/设计/30-decisions.md`）。

**`docs/调查/_ref/`** —— 从上游镜像过来的参考实现，MIT 许可，版权归
DeepSeek Harness Team，原样保留其 `LICENSE`。

**依赖** —— 只有两个：`aiosqlite`、`httpx`。其余全部基于 Python 标准库与 AstrBot 自身。

---

## 免责声明

使用第三方协议实现（OneBot 等）接入 QQ 存在**账号风险**，这是平台规则决定的，
与本插件无关。请自行评估后使用，作者不对账号处罚承担责任。

---

## 许可证

[MIT](LICENSE) © bomomoQWQ

`docs/调查/_ref/` 下是从上游镜像过来的参考实现，保留它自己的 MIT 声明，
版权归原作者（DeepSeek Harness Team），详见该目录里的 `LICENSE`。
