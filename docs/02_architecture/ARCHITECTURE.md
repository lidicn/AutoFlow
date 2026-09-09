# AutoFlow Gateway — 架构与设计说明（开发者向）

> 面向用户的上手文档见仓库根 [README.md](../../README.md)，部署见 [DEPLOY.md](../../DEPLOY.md)。
> 本文读者假定为**要改网关代码或做二次开发的人**（dw / wb1）。
>
> **本文版本**：2026-09-02 合并定稿。合并了两份冲突的旧架构文档：
> - E 版（`docs/ARCHITECTURE.md`，2026-08-03）：偏「怎么跑起来」，事实较新但结构不全。
> - D 版（`D:\Documents\HAOS\AutoFlow\docs\ARCHITECTURE.md`，2026-07-25，已冷存）：偏「结构地图」，
>   章节更全但**含已过期事实**（详见 §0.1）。
>
> **维护约定**：代码事实以 `src/autoflow_gateway/` 实际源码为准。本文所有模块清单、mode 取值、
> 行数统计均于 2026-09-02 对源码核对过。**改了代码结构请同步改本文**，否则下一个人会照着错的地图走。

---

## 0.1 合并时剔除的过期事实（防止旧知识复活）

| 旧文档的说法 | 现状（2026-09-02 核对） |
|---|---|
| agent mode = `black`/`white`/`dual`/`both`/`admin`（D 版标注「真实代码值，禁止改名」） | ❌ **已迁移为 `normal`/`expert`/`developer`**。`identity.py` 带迁移 SQL（black/both→normal，white/dual→expert，admin→developer）。旧值仅存在于历史数据库行 |
| 网关是 Windows 服务 `AutoFlowGateway`（nssm 自启） | ❌ 现部署于 **NAS Docker 容器 `autoflow_gateway`**；自更新走 `self_update.py` |
| 模块清单（D 版 30 项 / E 版 20 项） | ❌ 实际 **39 个模块**，新增 10 个两版均未记录（见 §11） |
| `gateway.py` 8010 行 / `dsl_engine.py` 3020 行 | ❌ 实际 **8885 / 3816** 行 |
| 用户工具「约 17 个」 | ❌ 实际 `mcp_server.py` 定义 **47 个** `autoflow_*`。**清单不写死在本文**，见 §5 |

---

## 1. 一句话定位

AutoFlow 网关是 **agent 与「Home Assistant + Node-RED」之间的唯一中介层**：独占凭证、集中落地安全策略、
把高层语义（自然语言 / DSL / 手写 flow）编译成真实可跑的 Node-RED 流。**agent 永不直连 HA/NR**，
可随时替换（WorkBuddy / dw / 其他 agent）。

对**用户**而言，AutoFlow 只暴露**一个概念——Automations（自动化）**；编译路径与原生手写路径是
内部引擎，不是用户要选的盒子（见 §2）。

---

## 2. 单入口产品模型（核心认知）

> **根因**：早期 black / white 是把内部「NL 编译器 vs 原生手写」的实现差异**错误地泄露成了两个产品盒子**，
> 导致用户选择焦虑、稀释价值。单入口架构让盒子对用户不可见，**保留两引擎**。

- **用户面只有一个概念：Automations**。界面只提供「描述框 + 画布 + 同一个部署按钮」，
  全界面不出现 black / white 字样。
- **内部两条路径，是引擎而非分盒**：
  - **编译器路径（`normal`）**：自然语言 → DSL → `dsl_engine` 编译为干净、可维护、
    **无 spaghetti Function** 的 flow。它是**质量 / 安全层级**，不是「弱白箱」。
  - **原生手写路径（`expert` 及以上）**：agent 直写 raw flow（允许 Function），
    作为逃生舱与语料探针反哺编译器。
- **安全来自闸门，而非分盒**：lint（R13/R15/R17/R20/R22 硬拦）/ E2E 仿真 / 重试预算
  对**所有 flow 一视同仁**，无论产自哪条路径。
- **统一提案闸**：两条路径产出的 flow 都落为**提案**进入 WebUI，由人审阅后才部署到 NR——
  不存在「一条路径能无人值守写 NR」。

### ★ 三路径产品定位（2026-09-05 更新，原双路径）

| | 安全路径（完整版 MCP） | **Pro 路径（AutoFlow Pro）★新增** | 专家路径（AutoFlow Core） |
|---|---|---|---|
| 面向 | 公众 / 普通用户 agent | 追求省 token 的日常用户 | 信任主体（项目所有者本人） |
| 交互方式 | MCP 协议，40+ 工具 | 轻量 skill + 网关 REST API | 专用 skill + `nr_client.py` 直连 NR |
| 固定开销 | ~15K token（工具定义） | ~3K token（SKILL.md） | ~2K token（SKILL.md） |
| 输出格式 | DSL（推荐）/ raw JSON | DSL（强制优先）/ raw JSON（逃生舱） | raw JSON |
| 流程 | 写 DSL → 编译 → vhass 重放 → WebUI 批准 → 部署 | 写 DSL → 网关编译 → 闸门校验 → 部署（可授权码自动部署） | agent 直连 NR 写 JSON |
| 护栏 | WebUI 批准闸 + 编译闸门 + 快照 | 编译闸门 + 快照 + 授权码越界检查 | 内置（快照 / lint / 节点数熔断），无批准闸 |
| 经验复用 | ✅ 全量（提案/部署/快照） | ✅ 全量（统一经过网关） | ❌ 直连 NR，数据丢失 |
| 定位 | **功能最全** | **性价比最高（省 token + 安全）** | **专家模式 / 离线可用** |

**三者并存非替代。**
- 完整版：功能最全，适合复杂场景和深度定制
- **Pro 版（v1.5.0+）**：日常场景首选，兼顾省 token 和安全性，是经验复用的数据入口
- Core 版：极客/离线场景，直连 NR 不经过网关

**Pro 版核心设计原则：DSL 优先，raw 补充。** SKILL.md 明确引导 Agent 用 `propose-dsl`，`deploy-raw` 仅作为逃生舱并附带警告。网关侧统计 DSL/raw 使用比例，提供 raw→DSL 自动转换引导。

**并存非替代。** Core 版真相源在 `core/`，红线见其 SKILL.md 黄金法则。Pro 版真相源在 `core/`（复用 nr_client.py，增加 `--gateway` 模式）。

---

## 3. 运行时拓扑

```
                        ┌────────────────────────────────────────────────────────┐
   agent (任意身份) ───► │  /mcp          用户面（任何 active 身份，用户工具集）        │
   (Bearer 身份码)       │  /mcp-white    专家面（expert/developer，+原生手写部署刀）   │
        │                │  /mcp-admin    开发者面（仅 developer，+运维/测试杠杆）      │
        │                │  /api/core/*   Pro 面（轻量 Agent 客户端，REST API）★新增  │
        │                │  /             WebUI 控制面（人审批 / 治理 / 诊断 / 统计）    │
        │                └────────────────────────┬───────────────────────────────┘
        │                                          │  Gateway 门面（唯一聚合点）
        │                ┌─────────────────────────┼──────────────────────────────┐
        │                │  HA 访问层   NR 访问层（细粒度单 flow）                    │
        │                │  防御层 / 确认闸 / 各 Store / DSL 编译 / 遥测 / 诊断环      │
        │                │  Token 统计 / 经验库（模板 / 错误知识 / 实体关联）★新增      │
        │                └─────────┬────────────────────────────┬───────────────────┘
   staging(默认)                                        prod(确认后 promotion)
  ┌──────────────┐                                  ┌──────────────────────┐
  │ NR 1990       │                                  │ NR 1880              │
  │ + vhass 虚 HA  │                                  │ + 真实 HA             │
  │ （练手/验证）  │                                  │ （生产，慎碰）         │
  └──────────────┘                                  └──────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  AutoFlow Pro 客户端（Agent 侧）★新增                                    │
  │  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────┐   │
  │  │ SKILL.md     │───►│ nr_client.py │───►│ 网关 REST API            │   │
  │  │ (~3K token)  │    │ --gateway    │    │ /api/core/propose-dsl    │   │
  │  │ DSL优先引导   │    │ 模式          │    │ /api/core/deploy-raw    │   │
  │  └──────────────┘    └──────────────┘    │ /api/core/templates     │   │
  │                                          │ /api/core/token-stats   │   │
  │  离线降级：网关不可达时 fallback 到直连 NR  │ /api/core/raw-to-dsl    │   │
  └─────────────────────────────────────────────────────────────────────────┘
```

- **默认动 1990(staging)+vhass**；动 1880(prod) 需用户明确授权。
  ⚠️ **1880 与 1990 是两个独立实例，不共享 flows 数据**，改一处不会同步到另一处。
- WebUI 已退化为**治理 / 控制面**（审批、部署、诊断、统计）；MCP 是当前主交互面。
- **Pro 面（/api/core/*）是 v1.5.0 新增**：为轻量 Agent 客户端提供 REST API，统一经过网关，享受编译闸门、快照、Token 统计、经验库等能力。
- 代码以**三个 FastMCP path + 一组 REST API** 实现能力分层（过渡态），产品契约是「按 mode 分层、不分盒」。

---

## 4. 分层（依赖严格自上而下）

| 层 | 模块 | 职责 |
|---|---|---|
| **入口层** | `mcp_server.py` / `webui.py` / `cli.py` | 三个对等入口（MCP / Web / 无头 JSON），都依赖 Gateway |
| **门面层** | `gateway.py` | **唯一聚合点**：编排访问层 + 防御 + 确认 + Store + 编译 + 遥测 + 诊断 |
| **访问层** | `ha_layer.py` / `nr_layer.py` | 网关与底层客户端的唯一边界；HA 读开放、写经确认；NR 只暴露单 flow 细粒度写 |
| **编译链** | `dsl_engine.py` → `subflows.py`+`api_specs.py` → `flow_linter.py`；`flow_diff.py` / `flow_simulator.py` 独立 | DSL 文本 → Scene 模型 → NR 节点 JSON；lint / golden diff / L2 仿真 |
| **叶子层** | `config` / `schemas` / `state` / `defense` / `confirm` / `identity` / 各 `*_store` / `template_lib` / `errors` / `lib/*` | 被多方依赖，是稳定底座 |
| **lib（vendored）** | `lib/nr_client.py` / `lib/ha_client.py` / `lib/affordance.py` | 复制进包内分发，**不依赖 skills 目录存在**。加载顺序：vendored → `HA_CLIENT_PATH`/`NR_CLIENT_PATH` → skills 目录 |

> `subflows.py` ↔ `api_specs.py` 互相引用，已用**加载顺序**解环（subflows 先定义 `Param/SubflowSpec/SUBFLOWS`，
> api_specs 反向 import 时前者已就绪）。

---

## 5. 三端点与 agent mode

传输：Streamable HTTP（`/mcp`、`/mcp-white`、`/mcp-admin` 同端口 `:8000` 三个 path）。
身份：原生 ASGI 中间件（`AgentAuthMiddleware`）强制 `Authorization: Bearer <身份码>`，匿名即 401；
能力由 **path + mode 双因子**判定。

| mode | 可连面板 | 能力（`_MODE_CAP` 原文） |
|---|---|---|
| `normal` | `/mcp` | 普通模式：只能走编译器路径（`autoflow_propose_dsl`），无原生手写部署刀；可写 DSL、查实体、领任务。上线须经 WebUI 人工批准 |
| `expert` | `/mcp-white` | 专家模式：可直写 Node-RED flow（`autoflow_deploy_raw`/`modify_flow`/`commit_ha_service`）+ 全部用户工具 + L2 逻辑仿真；双任务池都能领（auto_wb + auto） |
| `developer` | `/mcp-admin` | 开发者模式：全部用户工具 + 原生手写部署刀 + 测试杠杆（golden/acceptance 评测）+ 运维刀（重启网关/发布重置任务池/缺陷闭环）。**仅限网关自身运维身份** |

`/mcp-white` 是 `/mcp` 的**兼容别名**（专家身份旧端点不失效）。普通/专家身份连 `/mcp-admin` 会被中间件直接 403。

### ★ 工具清单为什么不写在这里

`mcp_server.py` 定义了 **47 个** `autoflow_*` 工具，且随版本增减。**把清单抄进文档必然过期**
（这正是两份旧架构文档都失准的原因）。

- 需要完整清单时，让 agent 调 **`autoflow_whoami`** —— 它实时取自网关注册表，
  返回「你此刻的身份 + 本 mode 能力 + 当前面板实际可调用的工具清单」，不会过期。
- MCP tool schema **已由函数签名装饰器自动生成**，不手工维护 `inputSchema`。

> **设计铁律**：批准 / 升格 / 管理身份**只在 WebUI（人）**，MCP 不暴露 `approve`——杜绝 agent 自己批准自己。
> 部署刀只在 `/mcp-white`，运维刀只在 `/mcp-admin`。

---

## 6. DSL → NR 编译管线

```
DSL 文本
  │  dsl_engine.parse()         → Scene（Trigger/Action/SubflowCall/Switch/Branch/
  │                              │  Delay/ReadState/HistoryQuery/HttpRequest/Extract…）
  ▼
  │  dsl_engine.compile()       → _Emitter 发射为 NR 节点 JSON（带坐标/命名）
  │     · _emit_step 返回 (head, tail)，分支/否则 上游连 head（避免首节点孤儿）
  │     · 子流程调用按 SubflowSpec.call 生成 link out / subflow 实例
  │        - http_api 类：编译器内联 change(设参)→http request→change(取 reply)，零 NR 写入
  │        - link_out 类：change(设参)+link out（指向 entry_link_id）
  ▼
NR flow JSON
  │  flow_linter.lint_flow()    → 检查 switch/jsonata 反模式/子流程端口/孤儿节点…
  │  flow_simulator.simulate_flow() → L2 逻辑可达性仿真（fail-open）
  │  flow_diff.diff_flows()     → 与 known-good(golden) 字段级 diff（golden 评测闭环）
  ▼
部署（经 确认闸 + 防火墙：nr_layer 只暴露单 flow 写）
```

**单一真相源原则**：API 能力只定义在 `api_specs.py` 的 `API_SPECS`，
`to_subflow_spec()` 派生网关注册，`build_nr_tab_flows()` 派生 NR「AutoFlow API」tab 的真实 flow——
网关注册与 NR tab 不再两处手搓。

**Link API 安装判定**：`needs_nr_flow()` 仅对 `nr_tab=True` 的 `link_out` spec 为 True
（这类才需要装 NR flow，才有安装按钮）；`http_api` 类与「从 tab 链接导入」的 `link_out` 为 False（零 NR 写入）。

---

## 7. 安全与护栏

| 机制 | 落点 | 作用 |
|---|---|---|
| 身份层 | `identity.py` + `mcp_server.AgentAuthMiddleware` | 每 agent 独立码 + 存档；`agent_id` 由网关注入，**不可伪造**；path+mode 双因子判定能力 |
| 确认闸 | `confirm.py` | 所有写操作进 `PendingOp` 待批；速率熔断 `max_pending_per_agent` |
| 防御层 | `defense.py` | 受保护流 / 爆炸半径上限 / 所有权隔离 / 高危域升级确认 |
| 设备保护 | `device_guard.py` | 受保护实体注册表（entity_id 精确 / domain 通配 / HA area），Tier-0 强制走人工确认闸，Tier-1 放行但记审计 |
| NR 护栏 | `lib/nr_client.py` | 写前快照 + prod 熔断 + `deploy_all` 缺失-id 护栏 + 子流程增量生成 |
| 自更新护栏 | `self_update.py` | 仅 developer/owner 可触发；只认 allowlist ref（`v*` tag 或显式 SHA）；**永不跑 `git clean -f`/`reset --hard`**；失败回滚 |
| 统一提案闸 | `proposals.py` | 两条路径都落提案，人审后才部署；`source` 徽章区分「编译产物(可信)/手写(需审)」 |
| lint / 仿真 / E2E 闸门 | `flow_linter.py` / `flow_simulator.py` | R13/R15/R17/R20/R22 硬拦 + L2 可达性仿真，对所有 flow 一视同仁 |
| 审计 | `audit.py` | 统一读取 apply/audit 记录入口（去人审后的唯一可追溯性来源） |

### ★ 两条用事故换来的铁律

1. **`POST /flows` 是整实例替换**（2026-07-16 事故）：只要 payload 是部分 flow（如单个子流程）
   而非完整集合，**其余全部蒸发**。
   → 永久护栏：禁止「部分 payload 的 `POST /flows`」；建 tab/子流程走 `create_or_update_flow`/`create_subflow`
   增量路径；`deploy_all` 缺失-id 即便 `force` 也拒。
2. **`restore_snapshot` 是整实例还原**：旧实现逐条 PUT 扁平 `/flows` 会把实例写崩
   （实测：全 tab 节点归零）。该路径已废弃。
   → **单条 flow 撤销应从快照取该条用 `write_flow` 写回，不要用整实例还原。**

---

## 8. 治理面（WebUI）

- **部署策略运行时可配**：`config.get_deploy_policy` / `set_deploy_policy` + `deploy_policy.json`
  落盘（fail-safe 白名单校验，未知值抛 `ValueError` 拒）。策略 `compiler_auto` 仅给提案打「可信」徽章、
  过 staging 闸门，**绝不无人值守部署**。
- **提案 source 徽章**：编译器产物=可信 / 手写=需审，审阅时一眼区分。
- **诊断查看器**：`gateway` 维护模块级 trace 环形缓冲（`_TRACE_RING`，重启即丢，符合瞬时诊断语义）；
  `/api/diagnostics` 聚合 env / health / 计数 / 最近 trace。
- **debug 旁路桥**（`debug_bridge.py`）：后台线程订阅 NR 5.x 原生 `ws://<nr>/comms` 事件流，
  **绝不往 flow 插采集/debug 节点**（侵入式改造会把干净观测变成脏数据）；只读 REST +
  `autoflow_debug_read` 工具；有界环形缓冲 + TTL + payload 截断；异常一律 fail-open。
- **统一撤回入口**：`Gateway.undeploy` 按 `deployed_node_ids` 手术式精确移除。
- **登录**：`webui_auth.py` 账号密码 + 服务端会话（pbkdf2_hmac 600k，**0 新增依赖**）。
  ★ **三套令牌隔离铁律**：WebUI 会话 / MCP `af_` 身份码 / ACP `acp_` 对等令牌互不相认。

---

## 9. 数据存储

| 位置 | 内容 |
|---|---|
| `data/<env>/autoflow.db`（SQLite，WAL） | `proposals` / `notes` / `plan` / `decision` / `command` / `api_configs` / `webui_sessions` / 身份码 |
| `data/<env>/state/` | 共享态 L1：`device_catalog` / `flow_catalog` / `entity_mapping` / `intent_log` |
| `data/<env>/connections.json` | HA / NR / Bark 连接凭据（**仅用户显式填写的字段**，`data/` 已 gitignore） |
| `data/<env>/experience/public/*.md` | 升格到 public 的提案（公用 skill） |
| `data/<env>/staging/deploy_policy.json` | 部署策略运行时配置 |
| `~/.workbuddy/nr_snapshots/` | nr_client 写前快照（救命用） |
| `data/<env>/backups/` | 部署前 flow 备份 |

环境：`staging`（默认，1990+vhass）/ `prod`（1880+真实，仅确认后 promotion）。

---

## 10. 子系统

- **`vhass.py`** — 纯标准库虚拟 Home Assistant：从真实 catalog 生成种子，提供 `/api/states`/`/api/services/*`/`/api/areas`，
  作为 staging 安全数据源，让 agent 不碰真实设备即可迭代。**已知缺口**：仅实现 REST，无 HA websocket
  （NR 若经 WS 订阅状态变化则收不到）。验证闭环改用 `inject` 节点或 `POST /api/trigger` 注入后断言。
- **`mock_docker_api.py`** — staging 非实体能力（Docker/业务 API）模拟。
- **`sync.py`** — 固定方向 1990(dev)→1880(prod) 的版本号驱动同步（写前快照+子流程预检+版本防重推）。
- **`acp_client.py`** — 调用对端 memory-worker `/acp`（JSON-RPC 2.0 over HTTP+SSE，`Bearer acp_xxx`）。
- **`llm_client.py`** — 自带 LLM 客户端（OpenAI 兼容 `/chat/completions`，多后端 fallback + 日志脱敏）。
- **`self_update.py`** — 受控自更新（见 §7 护栏）；WebUI 自更新只识别 `v*` tag。
- **`debug_bridge.py`** — NR 5.x 原生 debug 事件旁路采集（见 §8）。
- ☆ **`arena.py`** — **竞技场**（v2.0 主线）：分区 → 自由命题 → 三层题目审核 → vhass 验收 → 题目锁定 → 排行榜。数据落在 `data/arena/`。设计见 [04_ideas/ARENA_free_writing_design.md](../04_ideas/ARENA_free_writing_design.md)。
- ☆ **`experience.py`** — 经验数据收集管道（v1.6）：每次 DSL 提交/验收的结果回流成经验，喂给 `error_knowledge.py` 与智能推荐。

---

## 11. 模块速查表（`src/autoflow_gateway/`，50 个 .py）

> **2026-09-08 重新接手时核对**（上一版为 2026-09-02 的 39 个）。
> ☆ 标记 = v1.4–v2.0（dw 主导期）新增，上一版表格完全没有的模块，共 **9 个**。

**入口 / 门面**

| 模块 | 一句话 |
|---|---|
| `gateway.py` (9172 行) | 核心门面，聚合一切；`_GOLDEN_JOBS` + `_TRACE_RING` |
| `mcp_server.py` | MCP 服务 + Bearer 中间件；47 个 `autoflow_*` 工具 |
| `webui.py` (3701 行) | WebUI ASGI 应用（治理/控制面，无业务逻辑） |
| **`webui_auth.py`** | **WebUI 账号密码登录 + 服务端会话（三套令牌隔离）** |
| `cli.py` | 无 MCP 客户端的 JSON 入口 |

**编译链**

| 模块 | 一句话 |
|---|---|
| `dsl_engine.py` (3816 行) | DSL 解析+编译→NR；`parse`/`compile`/`validate`/`DSLError`/`Scene` |
| `subflows.py` | 预建子流程注册表；`SubflowSpec`/`Param`/`SUBFLOWS` |
| `api_specs.py` | API 能力**单一真相源**；`ApiSpec`/`API_SPECS`/`build_nr_tab_flows` |
| `flow_linter.py` | NR flow 结构 lint（R13/R15/R17/R20/R22） |
| `flow_simulator.py` | L2 逻辑可达性仿真 |
| `flow_diff.py` | 两 flow 拓扑+字段级 diff |
| `build_scene.py` | SceneIntent→NR flow（幂等） |
| `template_lib.py` | YAML+`{{var}}` DSL 模板 |

**访问层 / 底座**

| 模块 | 一句话 |
|---|---|
| `ha_layer.py` / `nr_layer.py` | HA / NR 访问层（NR **不暴露** `deploy_all`） |
| `defense.py` | 防御层（受保护流/爆炸半径/所有权隔离） |
| **`device_guard.py`** | **设备保护注册表（Tier-0 强制人审 / Tier-1 记审计）** |
| `confirm.py` | 人工确认闸；`PendingOp` |
| `identity.py` | MCP 身份 + 存档；mode = `normal`/`expert`/`developer` |
| `state.py` / `config.py` | L1 共享态 / 配置（env 优先） |
| `schemas.py` | 意图契约 + 校验 |
| **`errors.py`** | **结构化错误码基座（消灭「静默 count:0」「id 不存在却不报错」）** |
| **`connections.py`** | **HA/NR/Bark 连接凭据在 WebUI 可填（`data/`，绝不入库）** |

**Store**

| 模块 | 一句话 |
|---|---|
| `proposals.py` | 提案/经验沉淀（含 source 徽章） |
| `notes.py` / `plan_store.py` / `decision_store.py` / `command_store.py` | 用户笔记 / 计划 / 决策 / 命令 |
| **`task_store.py`** | **DSL 验证任务池；`tasks` + `task_claims`（多 agent 各自独立做同一任务）** |
| **`api_config_store.py`** | **Link API 运行时配置（独立 SQLite 表，真实密钥不进 git）** |
| **`audit.py`** | **审计日志统一读取入口** |
| ☆ `api_keys.py` (310) | **API Key 管理**：`af_pro_<32hex>`，存 SHA-256 hash；授权 tabs + 权限 + 过期（**fail-closed**） |
| ☆ `deploy_tokens.py` (365) | **部署授权码**：Tab 级授权，信任 Agent 可在指定 tab 自动部署；含节点阈值转人工、配额、限流 |
| ☆ `templates.py` (275) | **模板库**（Pro 版，与 DSL 的 `template_lib.py` 不同层：这是可复用 flow 模板） |

**Pro / 体验 / 竞技场（v1.5–v2.0 新增）**

| 模块 | 一句话 |
|---|---|
| ☆ `arena.py` (848) | **竞技场**：分区/题目/提交/验收；三层判重（实体重叠 >60% / 文本相似 >85% / LLM 考官 0.6–0.85）、创造力评分、题目锁定、排行榜。**三回路闭环（2026-09-09）**：①失败回执附经验库历史同类错误（`knowledge_feedback`，回读）；②种子态健康检查（`seed_health`：断言反态/目标离线，B20 纪律代码化）；③排行榜效率系数（`node_count`≤6 满分、≥12 触底，最高 +10% 不罚臃肿） |
| ☆ `experience.py` (636) | **经验数据收集管道**：实体共现、DSL 模式、错误样本自动采集，供智能推荐 |
| ☆ `tab_organizer.py` (492) | **Tab 组织分级方案**：P2 迁移 + P3 预警 |
| ☆ `error_knowledge.py` (187) | **错误知识库**：失败归因 → 修复建议（B20 降级族 4 个精确类别各有专门文案） |
| ☆ `token_stats.py` (150) | **Token 消耗统计**：按天/Agent/端点/模式聚合 |
| ☆ `snapshot_manager.py` | **快照 / 回滚**：授权码操作前自动快照（full 全量 / incremental 增量），支持一键回滚 |

**孪生 / 集成 / 运维**

| 模块 | 一句话 |
|---|---|
| `vhass.py` / `mock_docker_api.py` | 虚拟孪生 / mock API |
| `sync.py` | 1990→1880 同步 |
| **`acp_client.py`** | **ACP 客户端（对接 memory-worker `/acp`）** |
| **`llm_client.py`** | **自带 LLM 客户端（多后端 fallback + 脱敏）** |
| **`self_update.py`** | **受控自更新（allowlist ref + 备份 + 回滚）** |
| **`debug_bridge.py`** | **NR 5.x debug 事件旁路采集（不插节点）** |
| `telemetry.py` | 失败归因 `tag_action` |

**vendored lib**：`lib/nr_client.py`（+护栏/快照，`NRGuardError`）、`lib/ha_client.py`（只读）、
`lib/affordance.py`（HA 域状态/服务词汇表）。

---

## 12. 目录结构

```
autoflow/
  src/autoflow_gateway/    网关包（39 个模块，见 §11）
    webui/static/          响应式前端（index.html / app.js / style.css）
    lib/                   vendored HA/NR 客户端
    data/                  发布版内置 spec（api_specs.json / subflows/*.json）
  core/                    AutoFlow Core（专家路径）发行物真相源
  tests/                   单元测试
  examples/                示例意图
  docs/                    文档（见 docs/README.md 索引）
  scripts/tag_release.py   发版（VERSION + CHANGELOG + commit + tag v*）
  pyproject.toml / Dockerfile / docker-compose.yml / .env.example
```

---

## 13. 开发机运行（不触真实设备）

**方式 A — 可编辑安装（推荐）**

```bash
PY=<Python 3.12+>
cd autoflow_gateway && $PY -m pip install -e .
autoflow config                                  # 控制台命令
autoflow propose --file examples/scene_home_arrive.json
python -m autoflow_gateway.mcp_server            # 启 MCP（Streamable HTTP :8000/mcp）
```

**方式 B — 零摩擦启动器（无需安装）**

```bash
$PY -m pip install mcp pydantic uvicorn starlette python-dotenv
python run.py cli discover --keyword 客厅
python run.py mcp        # 启 MCP（强制身份，无 WebUI）
python run.py serve      # MCP(/mcp,/mcp-white,/mcp-admin) + WebUI(/) 同端口 :8000
```

> 凭证：复制 `.env.example` 为 `.env`；或设 `HASS_TOKEN`/`NR_PASS`/`HASS_SERVER`/`NR_URL`。
> **WebUI「连接设置」里填过的值优先级高于 `.env`**。

---

## 14. 测试

```bash
$PY -m pytest tests/ -q               # 全量
$PY tests/test_gateway.py             # 网关核心（mock 后端，零设备接触）
$PY tests/test_vhass.py               # 虚拟 HA 端点/服务变更/合成触发
$PY tests/test_staging.py             # staging 集成：gateway 指向 vhass 全链路
```

> WebUI 相关测试需 `AF_WEBUI_TOKEN_MODE=token_only`，否则本地零用户模式下未认证 POST 会 401。

---

## 15. 虚拟孪生 staging

让 agent 在**不碰真实设备**的前提下迭代 flow。

```bash
# ① 从真实 catalog 生成种子 + 镜像进 staging
autoflow seed-vhass --mirror --src data/prod/state/device_catalog.json --seed-out data/vhass_seed.json
# ② 运行虚拟 HA
autoflow vhass --port 8124 --seed data/vhass_seed.json
# ③ 运行 mock 非实体 API
autoflow mock-api --port 9100 --registry data/mock_api_registry.json
# ④ staging 网关指向 vhass：HASS_SERVER=http://127.0.0.1:8124
```

合成触发：`POST /api/trigger {"entity_id":"device_tracker.me","state":"home"}`。

---

## 16. 容器部署

- 镜像基于 `ghcr.io/astral-sh/uv:latest`，容器内用 `uv` 装包，宿主机无需预装 Python。
- `docker-compose.yml` 把 `./src`、`./data` 挂进容器 —— 改源码后 `docker compose restart` 即生效。
- 容器内访问宿主 HA 用 `http://host.docker.internal:8123`，**不要写 `localhost`**。
- 默认 `AUTOFLLOW_ENV=prod` → 所有 NR 写操作需 `allow_prod=True`。
- **生产部署**：NAS 容器 `autoflow_gateway`；走 `autoflow-nas-deploy` skill
  （md5 预检 → CRLF 辨伪 → 备份 + 仅 scp 差异文件 → py_compile + 重启 → 运行时内省验收）。

---

## 17. 已知约束

- **网关是 agent 唯一通路**；网关挂了所有 agent 停。`restart: unless-stopped` 会自拉起。
- 写操作一律进人工确认闸；`staging` 可放宽（`AF_AUTO_APPROVE=true`），`prod` 必须人工。
- `AF_MCP_HOST` 默认 `127.0.0.1`（安全默认）；容器 compose 已设 `0.0.0.0`。
  **暴露前请评估：这是能控制物理设备的网关。**
- **巨石模块**：`gateway.py` 8885 行、`dsl_engine.py` 3816 行，维护成本高；属已知技术债，
  不做大型重构（见项目收尾策略）。

---

## 18. 项目阶段与当前重点（2026-09-08 更新）

> ⚠️ 本节在 2026-09-02 曾写「v1.2.x 收尾版，之后进入维护态」。该判断**已被实际情况推翻**：
> v1.2.1 之后项目又迭代了 57 个 commit 到 **v2.0.11-beta**，新增三条产品线。
> 节标题与内容已重写，旧的「维护态 / 不堆新功能」结论**不再有效**，请勿引用。

### 18.1 版本演进

| 阶段 | 版本 | 内容 |
|---|---|---|
| 我交接出去时 | v1.2.1 | DSL 网关基本盘：编译 → 校验 → vhass 重放 → 人工批准 → 部署 |
| v1.4 | Deploy Tokens | Tab 级授权、可信 agent 自动部署、节点数阈值转人工、配额与限流 |
| v1.5–v1.7 | **AutoFlow Pro** | API Key（`af_pro_<32hex>`，SHA-256 存哈希）、Token 用量统计、模板库、经验库 |
| v2.0 | **Arena 竞技场** | 3 个分区（书房/客厅/卧室，各 8 设备）、三层去重、创意分、vhass 验收、排行榜 |

### 18.2 当前重点：Arena（竞技场）

后端 `arena.py`（848 行）+ 前端（app.js 约 100 处、侧栏 🏟️ 入口）**已实现并在库中**。
设计初衷见 [04_ideas/ARENA_idea.md](../04_ideas/ARENA_idea.md)（该文件状态仍写「想法阶段」，
**实际已实现**，待更新）。

- **目的**：让多个 agent 在授权的真实设备上写 flow，顺带收获经验数据 / DSL 边界 bug / 优质 flow。
- **三层去重**：实体重叠 >60% → 文本相似度 >85% → LLM 判定（0.6–0.85 区间，失败 fail-open）。
- **创意分**：新颖度 40% + 复杂度 20% + 实用性 20% + 描述质量 20%。
- **待办**：① NR 1990 真实部署验证 ② 教程补全 ③ Phase2 挑战模式。

### 18.3 收尾三件事（v1.2.x 遗留，状态更新）

| 事项 | 状态 |
|---|---|
| 文案去技术化 | ✅ 已完成。⚠️ v2 方案**推翻** v1 的「全面去技术化」：术语（flow/tab/节点/实体/DSL）**保留**，只对 AutoFlow 特有概念（安全闸/vhass/自愈闭环/Link API）加注。详见 [01_product/WEBUI_UX_PROPOSAL_v2.md](../01_product/WEBUI_UX_PROPOSAL_v2.md) |
| UX 美化 | ✅ 已完成（WebUI v2 重构） |
| 文档归一 | ✅ 已完成（本文所在结构） |

### 18.4 遗留技术债

- **§17 巨石模块**：`gateway.py` 9172 行、`dsl_engine.py` 3816 行，维护成本高，不做大型重构。
- **B1 无独立 staging NR**：staging 与 prod 同实例，**跨 T007–T010 连续 4 票 BLOCKED 的根因**。
  B1 解决前，不得声称任何写类 e2e 验证「已闭环」。详见 [04_test/findings-ledger.md](../04_test/findings-ledger.md)。
- **历史测试红**：截至 2026-09-08 全量仍有约 95 个历史失败（种子数漂移、lint 规则演进、
  校验器变严等），非新引入，待逐批清理。
