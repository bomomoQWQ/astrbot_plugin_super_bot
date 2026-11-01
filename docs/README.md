# Astrbot-Super-Bot · 文档总入口

> 目标形态：**AstrBot 插件**（`astrbot_plugin_super_bot`），不 fork 核心。
> 设计参照：《Super-agent 设计探索》（DeepSeek Harness 团队）+ `dsh-superagent-prototype`（MIT）。
> 基线：AstrBot **v4.28.1**（`ab42c0d9b`）。
> 全部结论来自源码实读，带 `file:line` 证据；推断显式标注。

---

## 先读这三份

| 文档 | 为什么先读 |
|---|---|
| [调查/00-index.md](调查/00-index.md) | **24 条已确认事实速查** + 三个意外收获 + 三个待验证。10 分钟看完整个结论面 |
| [设计/10-architecture.md](设计/10-architecture.md) | 修正后的总体结构（一个 Router 人格、两条入口、一个出口）+ 数据模型 + 状态机 |
| [设计/30-decisions.md](设计/30-decisions.md) | 12 条 ADR + Rejected 表。**改任何一条都会连带影响其他条** |

---

## 全部文档

### 调查（事实层）

| 文档 | 内容 | 行数 |
|---|---|---|
| [00-index.md](调查/00-index.md) | 索引与结论速查 | 58 |
| [01-runtime.md](调查/01-runtime.md) | 三条跑道（`request_llm` / `tool_loop_agent` / pipeline）、取消、hooks、工具体系、合成事件 | 204 |
| [02-state.md](调查/02-state.md) | 持久化：KV / `plugin_data` / conversation / 迁移 / 并发 | 261 |
| [03-triggers.md](调查/03-triggers.md) | 触发面全景、cron、pipeline 各阶段中断条件 | — |
| [04-reference-model.md](调查/04-reference-model.md) | 参考实现的可移植机制（数据模型 / 状态机 / 算法 / 阈值） | 944 |
| [05-sandbox.md](调查/05-sandbox.md) | Shipyard Neo 接入、Bay API、Windows 障碍、保活陷阱 | 465 |

> `调查/_ref/` 是参考实现的关键文档本地镜像（33 个文件），可离线复核。

### 设计（决策层）

| 文档 | 内容 | 行数 |
|---|---|---|
| [10-architecture.md](设计/10-architecture.md) | 总体结构、领域模型、模块边界、状态机、三条关键时序、沙箱策略 | 268 |
| [20-p0-spec.md](设计/20-p0-spec.md) | P0 规格：`gate.py` / `thread_key.py` / `store.py` + 12 条单测 | 208 |
| [30-decisions.md](设计/30-decisions.md) | 12 条 ADR + Rejected Concepts | 125 |

### 早期整理（仍有价值）

| 文档 | 内容 |
|---|---|
| [改造落点清单.md](改造落点清单.md) | 从"要不要做插件"到"怎么接"的完整心路，含 §1–§14：三个决定性发现、五模块落点、工程启示九条、难度重估、沙箱选型、未解问题清单 |

---

## 三个最容易踩的坑（无论先读哪份，都记住这三条）

1. **异步路径只 `stop_event()` 不 `yield`，主 agent 照样会跑。**
   机制在洋葱模型中间：`_has_send_oper` 是在下游发送时置真的（`process_stage/stage.py:56-66`）。→ 详见 [01-runtime.md §2.2](调查/01-runtime.md)

2. **插件自己的状态不能放插件目录。**
   插件更新会 `remove_dir()` 整目录重放（`star_manager.py:2115-2119`）。→ 必须放 `data/plugin_data/<plugin>/`，且自带 `schema_version` 表。→ 详见 [02-state.md](调查/02-state.md)

3. **用了 Shipyard Neo 就必须自己保活。**
   AstrBot 全仓库从不调 `keepalive`/`extend_ttl`，TTL 3600s 到期 managed Cargo 级联删除。→ 详见 [05-sandbox.md](调查/05-sandbox.md)

---

## 下一步（P0，不依赖沙箱与 Router）

1. `gate.py` —— 四条规则（自消息 / 指令放行 / 来源白名单 / 节流），纯函数
2. `thread_key.py` —— 两模式 + 优先级链 + 幂等键，纯函数
3. `store.py` —— `plugin_data` 下的 SQLite + `schema_version` + 单写者队列
4. 12 条单测全绿 → 装进 `data/plugins/` 验证不被主 agent 误触发

规格见 [20-p0-spec.md](设计/20-p0-spec.md)。
