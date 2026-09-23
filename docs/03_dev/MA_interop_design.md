# AutoFlow ↔ Memory-Agent (MA) 跨项目对接 —— 现状梳理 / 差距 / 规划设计

> **状态**：设计稿（**已留档待用，暂缓实施**）｜**日期**：2026-09-22｜**维护**：AutoFlow 侧（本对话）
> **暂缓决策**：2026-09-22 用户拍板 —— MA（memory-agent）**仍在快速迭代**，此时对接"对接了也是改来改去"，**先继续迭代 AutoFlow 自身**；本设计稿作为时机成熟时的起点。
> **范围**：AutoFlow × MA 的双向互通。**不含** AutoForge（另一条线，见 ROADMAP §8 v4.0.0）。
> **口径**：本文所有结论来自**实测清点**（读源码 / 读 MA 仓库只读副本），非推测；证据附 §7。
> **纪律**：方案完善并经评审后再动手，本文件只做"理顺 + 设计落盘"。

---

## 0. 一页速览（TL;DR）

**已经有（能用、已上线）**：ACP 双向委派（拓扑 X，peer-to-peer）+ 竞技场 4 个消费点；四套令牌互不认领。
**缺的是**：① 契约文档两端不对称（MA 的生态标准里根本没有 AutoFlow）；② AutoFlow **没有消费 MA 的核心能力**（65 个 MCP 工具 / 家庭习惯 / 成员在场 / 设备健康 / 行为漂移）；③ 记忆回流**单向**（只有竞技场战报）；④ MQTT 实时通道**未接**；⑤ MA 的记忆 **source 白名单不含 `autoflow`**；⑥ 长期健康度**无出口**。

**规划**：5 个阶段线性推进 —— **P0 契约固化（零代码）→ P1 读通道 → P2 写通道 → P3 实时通道 → P4 可观测/兼容**。
**需先拍板**：MA 侧 3 件事（补标准 / 开 source / 签令牌）+ AutoFlow 侧 1 件事（是否接 MQTT）。

---

## 1. 现状（实测清点）

### 1.1 拓扑：拓扑 X（peer-to-peer，两端都是 ACP server + client）

```
┌────────────────────────┐   ACP JSON-RPC 2.0 over HTTP+SSE   ┌────────────────────────┐
│   AutoFlow 网关         │ <────────────────────────────────> │   Memory-Agent (MA)    │
│   :8000  /acp  (server) │                                    │   <MA_HOST>:8086       │
│   acp_client.py (client)│                                    │   /acp  (server)       │
└────────────────────────┘                                    └────────────────────────┘
        ↑ 4 把 ACP 工具（只读+委派）                                  ↑ builtin 只读集 + delegate_to_autoflow
```

- 无中心 hub；任一端可用 `delegate` 工具主动调对端。
- 跨容器只走 **HTTP**（stdio 无法跨容器 spawn）。
- 协议同一份规格（MA 侧 `docs/acp-integration.md`），两端互相对称。

### 1.2 出向（AutoFlow → MA）：4 个消费点

| # | 能力 | 传输 | 令牌 | 代码位置 | 备注 |
|---|---|---|---|---|---|
| O1 | 反向委派（取家庭记忆/知识检索） | ACP `prompt`（**LLM 中介**） | `acp_` | `acp_client.delegate_to_memory_worker()`；MCP 工具 `autoflow_delegate_to_memory_worker` | 对端最终 `text` 块即结果 |
| O2 | 竞技场灵感 | ACP `prompt`（**LLM 中介**） | `arena_` | `acp_client.arena_fetch_inspiration()` → 对端工具 `get_arena_inspiration` | 用 `_VERBATIM` 约束模型逐字回传 |
| O3 | 竞技场战报落库 | ACP `prompt`（**LLM 中介**） | `arena_` | `acp_client.arena_record_result()` → 对端工具 `record_arena_result` | 8 字段 + `used_memory_tools` 遥测 |
| O4 | 竞技场快照读 | **HTTP GET（确定性）** | `arena_` | `acp_client.arena_fetch_snapshot()` → `/api/arena/snapshots/{id}` | **不走 LLM**，唯一确定性读通道 |

> 观察：**O1/O2/O3 都是 LLM 中介**（把工具调用包成自然语言再让对端模型执行），只有 O4 是确定性 HTTP。LLM 中介有三个已知代价：慢、token 贵、模型可能改写结果（才需要 `_VERBATIM` 兜底）。

### 1.3 入向（MA → AutoFlow）：AutoFlow 的 `/acp` 工具面 = 4 把

| 暴露名（对端稳定契约） | 派生自（MCP 唯一真相源） | 性质 |
|---|---|---|
| `list_entities` | `autoflow_list_entities` | 只读 |
| `get_entity_state` | `autoflow_get_entity_state` | 只读 |
| `list_automations` | `autoflow_list_automations` | 只读 |
| `delegate_to_memory_worker` | `autoflow_delegate_to_memory_worker` | 委派 |

- 来源：`mcp_server.py:_ACP_TOOL_MAP`（**从 MCP 注册表投影**，非手写；失败即抛，不静默降级）。
- 守卫：`tests/test_acp_tool_schema_single_source.py`（锁字段名 `inputSchema` + 4 把名单 + 改名即失败）。
- MA 侧对等工具 `delegate_to_autoflow`（`src/memory_agent/acp_server.py`），配置 `AUTOFLOW_ACP_URL` / `AUTOFLOW_ACP_TOKEN`。

### 1.4 令牌矩阵（现状，四套隔离，绝不互认）

| 侧 | 令牌 | kind/用途 | 可达范围 |
|---|---|---|---|
| AutoFlow | `af_` | MCP 身份码 | `/mcp`、`/mcp-white` |
| AutoFlow | `acp_` | ACP 端点 | `/acp` |
| AutoFlow | WebUI JWT | 人（批准/升格唯一入口） | WebUI |
| MA | `acp_` | kind=`acp` | `/acp` |
| MA | `arena_` | kind=`arena` | 3 个 arena 工具 + `/api/arena/*` |
| MA | `app_token` | 应用查询 | `/api/insights/query` + `/api/agent/memories` |
| MA | `mcp_token` | scope = read/write/admin | MA `/mcp`（65 工具） |
| MA | `butler_token` | 豆包管家专用 | 全部 |
| MA | WebUI JWT | 管理端 | 全部 |

- AutoFlow 侧 ACP 鉴权只放行 `kind in ("acp","arena")`（MA 侧 `acp_auth.py:52` 同款语义）。
- 落盘纪律：连接凭据只走 `connections.py` → `data/<env>/connections.json`，**绝不进 git**（P-2 门禁）；界面只回显掩码。

### 1.5 配置与探活

- AutoFlow 配置项（`config.py`）：`memory_worker_acp_url/token`（兼容别名 `MEMORY_AGENT_ACP_*`）、`memory_agent_arena_url/token`；开关 `acp_enabled`。
- WebUI「连接设置 → Memory-Agent (ACP 委派)」4 个字段（`connections.py`）。
- 探活：`connections.py:418` 对 MA `/acp` 带令牌 POST `initialize`，期望 200。

---

## 2. 差距分析（"差什么"）

> 每条 = 现象 + 影响 + 证据。**G1/G2 是主要矛盾**。

### G1 契约文档两端不对称（最紧要，历史已出过事）

- MA 的《MA 接口标准 v2.0（生态共享版）》适用项目列的是 **TVPilot / DeskPilot / 小甜菜 / 豆包管家 / FFL**——**唯独没有 AutoFlow**。AutoFlow 只在 MA 的 `docs/acp-integration.md` 与一份竞技场交接单里被附带提及。
- AutoFlow 侧**没有一份独立的 AF↔MA 对接契约文档**；事实散落在 `acp_client.py` 注释、`connections.py` 的界面 hint、`ARCHITECTURE.md §10`。
- **代价已兑现**：历史上 `inputSchema` vs `input_schema` 两端字段名不一致（F-ACP-KEY，2026-09-10 才裁决对齐）——正是"两端各自维护、改一端另一端不知道"的产物。
- 影响：任何一端升级协议/工具面，另一端无权威参照，只能靠读对方源码猜。

### G2 AutoFlow 未消费 MA 的核心能力（最大价值缺口）

- MA 侧已具备：**65 个 MCP 工具**（设备使用时长、记忆增删查升、感知事件、行为分析、视觉、成员画像）+ app 洞察接口 + 身份层（entity_id 漂移免疫）+ MQTT 在场/健康推送。
- AutoFlow 只用掉：1 条 LLM 中介委派（O1）+ 竞技场 3 个工具（O2/O3/O4）。
- **本可强相关的 4 类能力，全无 wired path**：
  | MA 能力 | 对 AutoFlow 的价值 | 现状 |
  |---|---|---|
  | 习惯记忆（habit，v2.0 记忆晋升） | 生成更贴合家庭实际的自动化（"工作日 23:00 后 Kevin 还在看电视"） | ❌ 未接 |
  | 成员在场（`ma/presence`） | 离家/回家类自动化的触发与前置条件 | ❌ 未接 |
  | 设备健康（`ma/device-health`） | AutoFlow「防假绿」需要知道依赖设备是否真在线 | ❌ 未接 |
  | 行为漂移（`get_behavior_drift`） | 自动化失效检测 / 建议重调 | ❌ 未接 |
- **两个结构性障碍**：
  1. AutoFlow **只有 ACP 客户端，没有 MCP 客户端**（`acp_client.py` 是唯一对外通道）。
  2. MA 的 `/acp` 默认**只暴露 builtin 只读集 + `delegate_to_autoflow`**，**不含那 65 个工具**（`acp-integration.md §4.1` 明说"默认保守只给只读集，需要写/变更类可在 MA 侧调整 `build_acp_tools()`"）。
- 结论：要消费 MA 能力，必须**新开一条确定性通道**（HTTP API 或 MCP），不能指望现有 ACP 委派。

### G3 记忆回流单向（写侧只有竞技场）

- AutoFlow → MA 的写入**只有** `arena_record_result`（竞技场结果）。
- AutoFlow 日常最有价值的经验——**flow 部署成功/失败、verify 结果、用户否决、失败归因、设备依赖**——**完全不回流 MA**。
- 而这正撞在既有共识上：**「学习闭环只沉淀 high + 单候选」「长期健康度归 MA」**——共识有了，**接口没有**。
- 反向（MA → AF）也只有 LLM 中介委派，没有"确定性拉取某类记忆"的入口。

### G4 MQTT 实时通道未接（事件驱动缺口）

- MA 已发布：`ma/presence`（retain，周期 60s 内容变化才发）、`ma/device-health`（对账发现状态变化才发）。
- AutoFlow 侧**没有任何 MQTT 客户端**：仓库里 `mqtt` 只出现在 DSL 节点白名单（`dsl_engine.py:118`）与 linter 高危规则（`flow_linter.py`）中，**没有收接者**（`grep mqtt|paho` 无客户端代码）。
- 影响：**事件驱动的自动化**（人离家 → 关灯/布防；设备失联 → 自动化降级 + 告警）无法实现；AutoFlow 只能靠 HA 轮询/NR。

### G5 身份 / source 未纳入生态统一模型

- MA 的记忆写入有 **source 白名单**，默认为 `["ma","butler","vision","manual"]`——**不含 `autoflow`**。
- 且 MA 有"**source 强制派生**"规则（v0.6）：用 app_token 写入时 body 里的 `source` 字段会被**忽略并覆盖**为令牌绑定的 source。
- 后果：AutoFlow 即便现在就开始写记忆，要么被拒，要么落进错误的 source 桶（污化 MA 的记忆分区）。
- 另：**令牌轮换策略缺失**（对应已登记的 C-A3 ACP 令牌轮换未闭环）；两端"谁为谁签发几个令牌"没有文档化。

### G6 版本 / 快照契约只覆盖竞技场

- `arena_fetch_snapshot` 是**唯一**"MA 拥有、AutoFlow 只读"的版本化快照路径。
- 更广的"**MA 记忆版本 ↔ AutoFlow flow 版本**"兼容矩阵缺失：M{A} 的 snapshot `version` 未在 AutoFlow 侧登记/校验，跨版本升级后无法判断"这条记忆/这份灵感是否还适用于当前 flow"。

### G7 长期健康度无出口

- 共识是「**长期健康度归 MA**」，但 AutoFlow 侧**没有任何健康上报出口**。
- MA 的 `ma/device-health` 只做 **HA 实体对账**，**不含 AutoFlow 的自动化层健康**（flow 是否还在跑、是否假绿、依赖设备是否离线）。
- 后果：AutoFlow 的"防假绿四件套 + 幽灵实体"（ROADMAP #18）是**单机自证**，无法与 MA 的实体健康形成交叉验证。

### G8 跨端失败降级语义未对齐

- AutoFlow 铁律：**降级空串绝不清零** / 注册表抓失败静默返空须判空 + REST 兜底。
- MA 铁律：**`stale: true` 不静默** / 记忆默认 `staging`。
- 现状：跨端调用失败时 AutoFlow 只返回 `{ok:false, error, hint}` 友好提示（不崩、但也不降级）——**没有**"MA 不可达时用本地缓存 / 降级到快照 / 告警"的约定，AutoFlow 也**没有本地缓存 MA 记忆**的机制。两端各自的降级哲学没有一份对齐文档。

---

## 3. 目标设计

### 3.1 设计原则（沿用既有铁律，不新造）

1. **不新增中心 hub**：沿用拓扑 X，谁需要谁主动调。
2. **确定性优先，LLM 中介降级为兜底**：能走 HTTP 就不走 LLM（对齐 `arena_fetch_snapshot` 与 AutoFlow「网关侧机械化、不耗 agent token」哲学）。
3. **令牌四套隔离，绝不互认**：新增令牌同样单用途、单范围、可单独吊销。
4. **记忆与长期健康归 MA**：AutoFlow 只做"读消费 + 经闸写入"，**不私存长期记忆**。
5. **写操作必经 AutoFlow 确认闸**：委派/回流**不绕过**"共享态→防御层→确认闸"。
6. **工具面收敛**：新增能力走"提案→评审"（ROADMAP #19 铁律），**不把 MA 的 65 工具灌给 AutoFlow agent**。

### 3.2 目标拓扑（在现状上叠加 3 条新通道）

```
                    ┌──────────── AutoFlow ────────────┐
                    │                                   │
   [现有] ACP 双向委派 │  acp_client (ACP)                 │
   ───────────────────│                                   │
   [新增 P1] 读通道    │  ma_http_client (确定性 HTTP)   ──┼──► MA /api/agent/memories/retrieve
   [新增 P2] 写通道    │  ma_http_client (确定性 HTTP)   ──┼──► MA /api/agent/memories (source=autoflow)
   [新增 P3] 实时通道  │  MQTT 订阅（或经 NR 桥）        ──┼◄── MA ma/presence, ma/device-health
                    └───────────────────────────────────┘
```

### 3.3 接口契约（拟定，待评审）

**P1 读通道（确定性）**

| 用途 | 拟用接口 | 令牌 | 备注 |
|---|---|---|---|
| 记忆检索 | MA `POST /api/agent/memories/retrieve` | 新 `ma_read` token（或 app_token） | 返回结构化，不经 LLM |
| 设备使用时长 | MA `POST /api/insights/query` | 同上 | 模板查询优先（MA 建议） |
| 实体/身份清单 | MA `GET /api/identity/devices` | 同上 | 仅 JWT/授权令牌，需 MA 放行 |

**P2 写通道（经验回流）**

| 用途 | 拟用接口 | 令牌 | 约束 |
|---|---|---|---|
| flow 部署/验收结果 | MA `POST /api/agent/memories` | 新 `ma_write` token（source=`autoflow`） | `state=staging`（永不自动 live）；**只推 high 置信**（对齐"学习闭环只沉淀 high + 单候选"） |
| 长期健康度 | 同上（topic_key=`health:/autoflow/`） | 同上 | 与 MA 的 `ma/device-health` 形成交叉验证 |

**P3 实时通道**

| 主题 | 用途 | 方式 |
|---|---|---|
| `ma/presence` | 离家/回家自动化触发 | 建议**经 NR 桥**（符合"非核心逻辑迁 NR"偏好），AutoFlow 侧只读 NR 的 flow 结果 |
| `ma/device-health` | 依赖设备失联 → 自动化降级/告警 | 同上 |

> P3 两条路线二选一：**(a) AutoFlow 内建 MQTT 客户端**（新增依赖，违反"零新依赖"惯例）；**(b) 经 Node-RED 桥接**（无新依赖，但引入 NR 中间层）。**建议 (b)**，与既有架构哲学一致。

### 3.4 数据 / 记忆归属（明确边界，防两头私存）

| 数据 | 归属 | AutoFlow 的角色 |
|---|---|---|
| 家庭记忆、习惯、画像 | **MA** | 只读消费；经闸写 staging |
| 长期设备/自动化健康 | **MA** | 上报出口，不自留长期健康 |
| flow / 部署 / 快照 / 回滚 | AutoFlow | 自有（本条不变） |
| 竞技场题目库 / 结果 | MA（`arena_titles`/`arena_results`） | 经 `arena_` 令牌读写 |

### 3.5 降级语义（跨端）

- MA 不可达：**不崩、不静默清零**；返回 `{ok:false, error, hint}` 并**显式标注"记忆不可用，自动化按无记忆生成"**（对齐 AutoFlow "静默返空须判空" 铁律）。
- MA 返回 `stale: true`：**原样上抛，不假装有数据**（对齐 MA "stale 不静默"）。
- 可选（P1 之后评估）：本地**短期**缓存 MA 读结果（带 TTL），仅作降级，**不作长期记忆**。

---

## 4. 分阶段规划

> 铁律：**前一阶段验收门不达标不进下一阶段**；每阶段都可独立叫停。

| 阶段 | 交付 | 验收门 | 依赖 | 规模 |
|---|---|---|---|---|
| **P0 契约固化** | ① AutoFlow 侧落本文档（已做）；② 提 MA 侧变更请求（把 AutoFlow 补进《接口标准 v2.0》）；③ 补 AF 侧 ACP 工具面漂移守卫（已有 `test_acp_tool_schema_single_source.py`，评估是否需扩） | 双方各有一份**互相引用**的权威契约文档；守卫测试覆盖 4 把 ACP 工具 | 无 | ≤半天 |
| **P1 读通道**（确定性） | AutoFlow 增 `ma_http_client`（或扩 `acp_client`）：记忆检索 / 设备使用时长；落 **1 个 MCP 工具（**专家档**）**；WebUI 连接设置加字段 | 真实 MA 实例上取回结构化记忆（非 LLM 中介）；MA 不可达时 fail-closed 且标注；工具面评审通过 | P0；**MA 侧签发 read 令牌** | 1~2 天 |
| **P2 写通道**（经验回流） | AutoFlow → MA 写 flow 结果/健康；**只推 high 置信**；source=`autoflow`（staging） | 写后 MA 可查（`state=staging`）；越权/无令牌被拒；**不污染**既有 source 分区 | P1；**MA 侧开 `source=autoflow` 白名单 + write 令牌** | 1~2 天 |
| **P3 实时通道**（事件驱动） | 订阅 `ma/presence` / `ma/device-health`（建议经 NR 桥）；接入自动化触发/健康告警 | 人离家事件能在 AutoFlow 侧产生一条可验证触发；设备失联能触发降级 | P1（或独立）；**架构选型拍板** | 2~3 天 |
| **P4 可观测 / 兼容** | 联合版本矩阵（MA 快照 version ↔ AF flow 版本）；令牌轮换（闭环 C-A3）；健康度交叉验证 | 版本不匹配时显式告警；令牌可安全轮换 | P2 | 1~2 天 |

---

## 5. 待确认（跨团队 / 用户拍板）

**MA 侧（需跨项目协作，本仓只提请求、不改码）**
1. 是否同意把 **AutoFlow** 补进《MA 接口标准 v2.0（生态共享版）》并注明对接方向（ACP 双向 + 竞技场）？
2. 是否开放记忆写入 `source` 白名单新增 **`autoflow`**（否则 AF 写不进或写错桶）？
3. 是否为 AutoFlow 签发**专用 read / write 令牌**（单用途、可吊销）？轮换策略？

**AutoFlow 侧（用户拍板）**
4. P3 实时通道：接受 **MQTT 直连**，还是走 **NR 桥**？（我推荐 NR 桥，零新依赖）

**顺序取舍（用户拍板）**
5. 先做 **P1 读通道**（立刻能用上 MA 记忆/健康，风险低）还是先做 **P2 写通道**（让 MA 学到 AutoFlow 的经验）？我推荐 **先 P1**——读通道收益直接、风险最低，且是写通道"验证写得对不对"的前置。

---

## 6. 风险与"不做"

**风险**
- 跨项目强耦合：MA 侧改动不受 AutoFlow 控制 → 用"契约文档 + 守卫测试 + fail-closed 降级"三件套隔离。
- LLM 中介的不可靠性（现状 O1~O3）→ 新增通道一律**确定性 HTTP 优先**。
- 令牌扩散 → 每令牌单用途单范围 + 可吊销 + 掩码落盘。

**不做（负面清单，防范围膨胀）**
- ❌ 新增中心 hub / 服务注册中心（保持拓扑 X）。
- ❌ 把 MA 的 65 个工具灌进 AutoFlow 的 agent 工具面（违反 #19 工具面收敛）。
- ❌ AutoFlow 私存长期家庭记忆（记忆归 MA）。
- ❌ 共享/复用对方 LLM 密钥（MA 用 MA 自己的后端）。
- ❌ 用 LLM 中介替代确定性读取（能走 HTTP 就不走 LLM）。

---

## 7. 证据索引（实测来源）

| 结论 | 证据 |
|---|---|
| 出向 4 消费点 | `src/autoflow_gateway/acp_client.py`（`delegate_to_memory_worker` / `arena_fetch_inspiration` / `arena_record_result` / `arena_fetch_snapshot`） |
| 入向 4 把 ACP 工具 | `src/autoflow_gateway/mcp_server.py:2245` `_ACP_TOOL_MAP`；`tests/test_acp_tool_schema_single_source.py` |
| ACP 开关 / 端点 | `config.py:129` `acp_path`；`config.py:244` `is_acp_enabled` |
| 配置项与界面字段 | `config.py:134-145`；`connections.py:53-92`（memory 组 4 字段）；`connections.py:418`（探活） |
| 令牌隔离（AutoFlow） | `identity.py:346`（`acp_` 仅用于 `/acp`） |
| 令牌隔离（MA） | `memory-agent/src/memory_agent/acp_auth.py:52`（`kind in ("acp","arena")`） |
| MA 接口标准（不含 AutoFlow） | `memory-agent/docs/MA接口标准_v2.0_生态共享.md` §0/§8 适用项目清单 |
| MA 的 65 MCP 工具 | 同上 §4.2；MA `/mcp` Streamable HTTP |
| MA source 白名单 / 强制派生 | 同上 §2.2、§9.3 |
| MA 实时主题 | 同上 §5.1（`ma/presence` / `ma/device-health`） |
| ACP 协议规格（对称要求 §8） | `memory-agent/docs/acp-integration.md` |
| 竞技场对接设计（MA 侧） | `memory-agent/.codebuddy/plans/AutoFlow竞技场对接完善计划_c5e0bb2a.md` |
| AutoFlow 无 MQTT 客户端 | `grep mqtt\|paho` 命中仅 DSL 白名单/linter，无收接者 |
| ROADMAP 生态定位 | `docs/ROADMAP.md` §0（MA = 创造力源泉）、§4 #4（记忆联动消费点）、§8 v4.0.0 |
