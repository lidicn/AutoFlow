# AutoFlow 版本路线图与开发计划（最终版）

> **本文件是 AutoFlow 路线图的唯一有效版本**（2026-09-10 起，2026-09-14 重写定稿，2026-09-16 增补实体解析决策层）。
> 旧路线图（`00_overview/ARCHITECTURE_AND_ROADMAP.md`、`03_dev/ROADMAP.md`）已作废，勿改。
> 维护纪律：每完成一项打 ✅ 并注明 commit；新增项必须带验收门，无验收门不立项。
> **本版最大变化**：明确 AutoFlow 在「AutoForge 已承接 agent 创作」后的定位——**AutoFlow 不再追 agent 主导创作，回归"面向人的 HA/NR 安全部署与验证网关"，分 Core（极客）/ Pro（小白）两档**。

---

## 0. 生态定位与边界（2026-09-14 拍板，最终）

四件套分工，**互不抢地盘**：

| 项目 | 角色 | 面向 | 写自动化的主体 | 代码库 |
|---|---|---|---|---|
| **AutoForge** | 自动化**工厂**（创建/验证/部署） | **Agent** | Agent 主导（Graph-IR → 验证 → 提案） | `E:\NAS\AutoForge`（codebuddy 开发） |
| **AutoFlow（本仓库）** | **安全部署 / 验证 / 回滚**网关 | **人**（极客 + 小白） | 人写 / Agent 辅助但**人批准** | `E:\NAS\autoflow` |
| **doubao-butler (DB)** | 连接与执行、决策对话、TTS、PushGuard | 人/设备 | — | `E:\NAS\doubao-butler` |
| **memory-agent (MA)** | 创造力源泉（记忆/假设/证据） | Agent | — | — |

**AutoFlow 的定位结论（保守、不激进）：**
- AutoFlow 是 **HA + Node-RED 自动化的人脸安全网**：人写的 flow / DSL，经它编译→静态扫描→vhass 孪生验证→人批准→部署+快照，出错能回滚。
- **Core 档（极客）**：直接玩 NR flow，提供手术刀式单节点/单 flow 编辑、diff 预览、多 flow 安全部署、只读 inventory。给"自己写创意流"的高手。
- **Pro 档（小白）**：agent 辅助写 DSL → verify_flow 可视化 → 极简一键批准/回滚；默认隐藏 NR 细节，只给"设备 + 意图 + 验证结果"。
- **Agent 主导的"从零创作自动化"归 AutoForge**。AutoFlow 的 Pro 档只做"agent 辅助 + 人批准"，agent **不自我批准、不自我创作**——这是与 AutoForge 的硬边界。

---

## 1. 当前基线

- **版本**：v2.0.11-beta（竞技场 beta）｜ 竞技场七轮：R7 客厅轮 6/6 全 fv，三机制 + 记忆联动②③已上线
- **架构真相源**：[`02_architecture/ARCHITECTURE.md`](02_architecture/ARCHITECTURE.md)
- **MCP 连接层评估结论**（2026-09-10）：安全授权超额；发现类缺口已清零（v2.0.12-1 OAuth ✅、v2.0.12-2 schema 单一真相源 ✅）；限流降级为条件触发（#8）。
- **安全不变量**（永不变）：批准/升格只在 WebUI；`af_*` 前缀=agent 可写、其余只读；无 replace-all/delete-all；爆炸半径上限；prod NR 只读。
- **实体解析现状（2026-09-16 评估）**：`resolve_entity`（gateway.py:1809）检索/候选/消歧已稳——网关侧机械化、不耗 agent token、fail-closed（绝不静默猜域）、返回 `possible_states` 让 agent 不猜域；房间→area 亦网关侧（`_resolve_area`+`_build_room_aliases`，已修 B1 短词反向吞长词）。但 catalog 实体 schema（refresh 9061-9205）**缺 `integration`/`platform`/`connectivity`/`offline` 字段**——多集成双 entity_id 无优选、弱信号无降权、怪异设备易混淆，列入 v2.3.0 决策层（见 §6）。

---

## 2. 版本路线总览（最终）

| 版本 | 主题 | 档位 | 关键交付 | 状态 |
|---|---|---|---|---|
| **v2.1.0** | 收口稳定 | 共用 | 94 测试红清帐 / MA 消费点 / 守卫测试绿 / 安全不变量固化进闸 | 进行中 |
| **v2.2.0** | 双档成型 | Core+Pro | Core：手术刀编辑+diff+多 flow 安全部署；Pro：引导式 DSL+极简批准；共用 verify_flow 核 | 进行中（Core 档先行，2026-09-14 起；#7/#9 引擎已落地） |
| **v2.3.0** | 实体解析决策智能 + 可信闭环 | 共用 | 实体解析决策层（device 归并/集成优选/弱信号降权/遥测）；快照/回滚稳定化；Arena 转正回归台；防假绿四件套+幽灵实体固化 | 进行中（#13/#14/#15 实体解析决策层已落地 2026-09-16；#16/#17/#18 待排期） |
| **v3.0.0** | 平台成熟 | 共用 | 最小稳定工具面(Core/Pro MCP+UI)；部署/验证/回滚审计；安全扩展接口(预留关) | 待排期 |
| **v4.0.0** | 生态共存 | 跨 | AutoForge→AutoFlow 交接协议（导出+验证+部署）；明确非重叠 | **可选/延后** |

> 节奏原则：**v2.x 每个版本都是"低成本增量"，不夹带大重构**；v3.0 才动工具面收敛与审计；v4.0 仅当 AutoForge 成熟且用户想要"agent 创作→AutoFlow 安全落地"时才做。

---

## 3. v2.0.12 —— 连接层成熟化（速赢批次，已完成）

| # | 项 | 内容 | 验收门 | 规模 |
|---|---|---|---|---|
| 1 | ✅ **OAuth 资源发现端点**（commit 40cbed4） | 补 `/.well-known/oauth-protected-resource`（RFC 9728）+ authorization-server metadata（RFC 8414）；401 带 `WWW-Authenticate` 发现头 | ✅ 三端点 200 + 401 带发现头；`/.well-known` 404 消失 | ~半天 |
| 2 | ✅ **Schema 单一真相源**（A20/A27，commit c1662de） | `/acp` 工具面从 MCP 注册表投影；手写 schema 删除 | ✅ 守卫 `test_acp_tool_schema_single_source.py` 8 例全绿 | 1~2 天 |
| 3 | ⏸ **每身份限流/超时（降级为条件触发，见 #8）** | NAS `:8000` 直连无反代，仅 LAN+Tailscale 可达 | 触发条件见 #8；当前不做 | — |

## 4. v2.1.0 —— 记忆联动 + 运维债（体验批次，进行中）

| # | 项 | 内容 | 验收门 | 前置 |
|---|---|---|---|---|
| 4 | ✅ **memory-agent 联动消费点**（commit b359cd6，2026-09-14 复核达标） | 新增 `acp_client.py`(+129)/`arena.py`(+96)；读侧 `fetch_memory_inspiration`（snapshot 首选 / ACP 兜底），写侧 `_push_memory_report`（含 `used_memory_tools` 回证） | ✅ **22 passed / 0 失败**（`test_arena_memory_channel.py` + `test_arena_memory_linkage.py`）；读侧用例齐备，`test_push_report_success_uses_fully_verified` 断言 `used_memory_tools==["get_arena_inspiration"]` 证明**真实读过**（非只写不读） | ~~ACP WebUI 前端落地~~ → **该前置已不成立**：实现走 `acp_client` 服务端 + arena 读接口，不依赖 WebUI 页面 |
| 5 | ✅ **历史测试红清帐**（2026-09-14 收官） | 期望漂移修复专项（修测试不改产品）；完成后 A/B 基线克隆回归退役 | ✅ 全量 1695 passed / 1 skipped / 0 failed（基线实测 92 红 → 0；V-F1~F4 零信任闸守卫 42 passed 同绿） | 无 |
| 5.1 | ✅ **跨文件环境污染根因**（2026-09-14） | `tests/test_gateway.py:262` 的 `make_gateway("prod")` 设 `AUTOFLLOW_ENV=prod` 后**不还原**，毒化后续全部 NR 写 → 与产品无关的假红。修法：`tests/conftest.py` 加 autouse 夹具 `_isolate_global_env`，对 `AUTOFLLOW_ENV/NR_PROD/AUTOFLLOW_DATA_DIR` 快照+用例后还原 | ✅ 污染源+受害者同会话 **57 passed / 0 失败**；全量 **92 红 → 85 红**（passed 1605→1612），**零产品代码改动** | 无 |
| 5.2 | ✅ **doubao spec 移除后的测试漂移**（2026-09-14） | `cb43830`+`0f4940a` 按 **P0 决策移除 doubao 4 条 spec**（chat/say/image/vision，能力外置 doubao-butler；`tts_speak` 亦于 `77c1b28` 移出网关），但 `test_api_specs.py` 仍断言 `llm_doubao_*` → **7 红** | ✅ 同步 `ALL_SPECS`/`NR_FLOW_SPECS`；删除 3 个锁定已外置能力的用例；tab 节点数按**实测** 24→**15**（2 入口）。**11 passed / 0 失败**。⚠️已在文件内注明"勿凭旧断言把 doubao 加回" | 无 |
| 5.3 | ✅ **WebUI 鉴权后的测试漂移**（2026-09-14） | 账号密码改造后默认 `password_only`，未认证请求一律 401（含本机 TestClient）→ `test_connections_settings.py` **6 红**；另 `memory` 连接组由 `b359cd6` 新增，期望列表过时 | ✅ 沿用代码库既有配方（`test_webui_settings.py`）：建 app 前设 `AF_WEBUI_TOKEN_MODE=token_only`，tearDown 备份还原；期望补 `memory`。**24 passed / 0 失败** | 无 |
| 6 | ✅ **竞技场教程**（`skills/arena.md` v2.0.0） | 12 节 + 提交前自检清单 | ✅ 新 agent 只靠教程完成 T0 并成功提交一题 | 无 |

> **v2.1.0 的 Done 定义**：红清帐完成 + MA 消费点有真实读行为 + 零信任质量闸(V-F1~F4)守卫测试全绿。此版不引入新能力，只把已知债还清、把安全不变量**固化进测试门**。
> ⚠️ **口径订正（2026-09-14 实测）**：历史口径"94 红"已过时，实测基线为 **92 红 / 1605 passed**；修复 5.1 后为 **85 红 / 1612 passed**。后续分批清理按剩余簇推进，每簇先判「改测试 vs 真产品缺陷」再动手。

## 5. v2.2.0 —— 双档成型（Core 极客 / Pro 小白）

**Core 档（极客）**
| # | 项 | 内容 | 验收门 |
|---|---|---|---|
| 7 | 🔧 **手术刀式单节点/单 flow 编辑**（引擎✅ 2026-09-14） | `nr_client.modify_node_field` 支持结构键守卫（禁改 id/type/z/wires/inputs/outputs）+ `dry_run` diff 预览 + 兄弟节点数 0 变化断言（fail-closed）；连线走 `add_wire`/`remove_wire`。**待续**：WebUI/MCP 工具面暴露（人批准路径，agent 不写非 `af_*` 流） | ✅ 引擎单测 `test_nr_client_core_surgical.py` 9 例全绿（守卫/diff/兄弟数/节点缺失）；验收门「兄弟节点数 0 变化 + diff 可读」已满足 |
| 8 | ✅ **多 flow 安全部署**（commit d8c583f 后端 + 8a00acd 前端，2026-09-16/17） | 一次部署多个 flow，逐个 node-count/structural guard；任一个不达标整体回退，不半部署（WebUI 提案多选 + 批量部署到 NR，任一失败整体回滚） | ✅ 部分失败时已部署部分可一键回滚到部署前快照；前端批量部署 UI + 单测 4 passed |
| 9 | 🔧 **只读 inventory 增强**（引擎✅ 2026-09-14） | `nr_client.get_inventory()` 纯 GET 清点 tabs→nodes，含 `owned_by_af`（label `af_` 前缀）+ 风险标注（`unknown_node_type` / `protected_flow`）；零写路径。**待续**：WebUI 只读面板暴露 | ✅ 引擎单测 `test_nr_client_core_surgical.py` 3 例全绿（结构/归属/受保护/纯只读）；验收门「列得全、标注准、只读」已满足 |

**Pro 档（小白）**
| # | 项 | 内容 | 验收门 |
|---|---|---|---|
| 10 | **引导式 DSL 编写** | 对话/表单引导产出 DSL；编译后即 verify_flow 可视化（不暴露 NR 细节） | ✅ 小白按引导产出 1 条可验证 DSL |
| 11 | ✅ **极简批准 UX**（commit 8a00acd，2026-09-17） | 一键批准 / 一键回滚；展示"意图 + 验证结果 + 影响设备"三句话（💡 人话卡，提案卡内插），无技术噪音 | ✅ 非技术用户能独立完成批准/回滚；三句话卡前端+端点已落地（test_webui_proposals_v22 4 passed） |

**共用**
| # | 项 | 内容 | 验收门 |
|---|---|---|---|
| 12 | **verify_flow 统一核** | 编译→静态扫描→vhass 孪生 三件套作为 Core/Pro 共用验证核，一套逻辑两档复用 | ✅ Core/Pro 走同一验证入口，行为一致 |

> 细节参考：`01_product/PRODUCT_AutoFlow_Pro.md`、`01_product/RELEASE_PLAN_core_v1.md`。

## 6. v2.3.0 —— 实体解析决策智能 + 可信闭环

**实体解析决策层（来自 2026-09-16 架构评估）**：当前 `resolve_entity`（gateway.py:1809）已是网关侧机械化、不耗 agent token、fail-closed（绝不静默猜域），且返回 `possible_states` 让 agent 不猜域——方向正确。但 catalog 实体 schema（refresh 9061-9205）缺 `integration`/`platform`/`connectivity`/`offline` 字段，导致三大痛点未解：① 同设备多集成产生两套 entity_id 平铺返回、无优选；② 弱信号/常离线设备无健康度、仍被返回；③ 怪异设备（如书房电脑=switch 开机卡）靠返回 domain 自查、风险在 fuzzy 排序埋没。以下 P0/P1/P2 是**低成本增量，补"决策智能"而非重做检索**——符合 v2.x 小步原则。

| # | 项 | 内容 | 验收门 | 优先级 |
|---|---|---|---|---|
| 13 | ✅ **实体解析·设备归并与集成优选（P0）**（commit 4b703f5，2026-09-16） | `refresh_catalog` 抓取 `integration`/`platform`（HA websocket `entity_registry` 直给 `platform`/`config_entry_id`，零成本）；`resolve_entity` 按 `device_id` 归并同物理设备多 entity_id、去重展示并标注各接入路径；默认开启 `state.add_mapping` 沉淀人工选择为精确映射（high 置信），下次直中 | ✅ 同 device_id 双集成实体在候选中归并为一条设备卡、首选本地集成；"书房电脑"二次解析直中（不再每轮重排） | 已落地（速赢插队 v2.2 完成） |
| 14 | ✅ **实体解析·连通性与健康度（P1）**（commit b4888ef，2026-09-16） | catalog 增 `connectivity_tier`（local>cloud>polling，由 integration/config_entry 推导）+ `entity_health`（`offline_rate`，由 state 历史 `unavailable`/`unknown` 频次 + `last_seen` 间隔推导）；`resolve_entity` 打分纳入降权/优选并显式回传 agent（"设备 Y 常离线，建议优先 X"） | ✅ 弱信号设备候选降权且带 health 标注；本地集成排序优先于云集成；离线设备不被静默置顶 | 已落地 |
| 15 | ✅ **实体解析·遥测与消歧闭环（P2）**（commit 54fbe6e，2026-09-16） | `resolve` 出口记 `exact/medium/low/ambiguous/none` 计数（落指标/`intent_log`），给出真实成功率；歧义时回自然语言消歧提示（"书房电脑 是 switch 开机卡，不是 light"）；与 `verify_flow` 联动——离线/低健康度设备不进自动化候选或明确标注，防假绿 | ✅ 遥测可读出成功率漏斗；歧义提示驱动 agent 澄清；离线设备被 verify 标记不可靠 | 已落地 |
| 16 | **快照/回滚稳定化** | 部署前自动整实例快照；失败/异常一键回滚（修掉 T011 逐条扁平 PUT 全 tab 归零坑，坚持 `POST /flows` 整包） | ✅ 回滚后状态 == 部署前；无归零 | — |
| 17 | **Arena 转正回归台** | 每次发版用 Arena 跑回归断言（不进生产，纯验收）；从"实验场"变"回归台" | ✅ 发版阻塞于 Arena 回归红 | — |
| 18 | **防假绿四件套 + 幽灵实体固化** | B20 空转 / B22 未激活分支 / F-R5-01 死锁 / F-R6.5 数值当字符串 + 幽灵实体检测，固化进 verify 闸门，不可旁路 | ✅ 针对性用例全绿，旁路即失败 | — |

## 7. v3.0.0 —— 平台成熟（稳定面 + 可观测）

| # | 项 | 内容 | 验收门 |
|---|---|---|---|
| 19 | **最小稳定工具面** | Core/Pro 各一套**最小 MCP + UI 工具集**（使用者视角，无运维刀：重启/清库等不暴露）；工具面变更走"提案→评审" | ✅ 工具数 = 最小可用集；运维动作不可达 |
| 20 | **部署/验证/回滚审计** | 基于现有 `_slog`，补部署/验证/回滚事件的结构化审计（**不引入 OpenTelemetry**） | ✅ 任一次部署可回溯"谁/何时/验证了什么/回滚了没" |
| 21 | **安全扩展接口（预留关）** | 留 pluggable executor 接口占位（默认关），供极客接外部执行器；**不实现 NR 替换** | ✅ 接口存在且默认关；接错不崩主流程 |

## 8. v4.0.0 —— 生态共存（可选 / 延后）

| # | 项 | 内容 | 触发条件 |
|---|---|---|---|
| 22 | **AutoForge→AutoFlow 交接协议** | AutoForge 生成的自动化可导出为 AutoFlow 能验证+部署的格式（Graph IR / HA automation → AutoFlow DSL/flow）；让想保留"人批准+NR 兼容+回滚"的用户用 AutoFlow 作安全落地层 | AutoForge 成熟且用户想要该路径 |
| 23 | **非重叠声明固化** | 文档写明：AutoForge=creator(agent)，AutoFlow=safe-deploy/verify/rollback(人)；不抢 agent-authorship | 同 #22 |

> v4.0.0 在做之前必须重新评审；当前**不排期**。

---

## 9. 条件触发（不主动排期）

| # | 项 | 触发条件 | 备注 |
|---|---|---|---|
| 7st | **B1 独立 staging NR** | 需要做写类真机 e2e 闭环时立项 | **B1 解除前，禁止声称任何写类验证"已闭环"**；竞技场验收走 vhass 孪生，不受 B1 阻塞 |
| 8 | **每身份限流/超时** | 某 `af_` 令牌泄漏滥用 / agent 循环猛打网关 | 网关只在 LAN+Tailscale 可达（非公网）；应急=WebUI 吊销令牌。真立项目标=反代+令牌桶，属 infra 变更 |

---

## 10. 明确不做（负面清单，防止范围膨胀）

- ❌ **Agent 主导的自动化创作**（归 AutoForge；AutoFlow Pro 只做"agent 辅助 + 人批准"）
- ❌ **自研仿真器 / 把 vhass 迁到 pytest-homeassistant**（v1 的 vhass 保留为 NR-flow 孪生；pytest-homeassistant 收益仅归 AutoForge 的 `forge sim`）
- ❌ **替换 / 跨接 Node-RED 之外的执行后端**（保持 NR-bound）
- ❌ **replace-all / delete-all 类工具**（安全不变量：爆炸半径上限，永不做）
- ❌ **大型架构重构**（增量改进原则；v2.x 每个版本都是小步）
- ❌ **多租户 / 企业网关**（单家庭单网关形态）
- ❌ **OpenTelemetry 接入**（`_slog`/`_telemetry` 已够用）
- ❌ **公网暴露**（只在 LAN + Tailscale 尾网）

---

## 11. 开发计划（分阶段，顺序即优先级）

| 阶段 | 范围 | 负责人 | 里程碑/验收闸门 | 依赖 |
|---|---|---|---|---|
| **A** | ✅ v2.1.0 收口 | 本对话（维护） | 92 红清帐(1695 passed/1 skipped/0 failed) + MA 消费点真实读(22 passed) + 零信任闸守卫全绿(42 passed) | —（已完成） |
| **B** | v2.2.0 双档 | 本对话 | Core 手术刀编辑+多 flow 安全部署；Pro 引导 UX；共用 verify_flow 核 | A 完成 |
| **C** | v2.3.0 实体解析决策智能 + 可信闭环 | 本对话 | 实体解析决策层（device 归并/集成优选/弱信号降权/遥测）；快照/回滚稳定；Arena 回归台；防假绿固化 | B 完成 |
| **D** | v3.0.0 成熟 | 本对话 | 最小工具面；审计；扩展接口预留 | C 完成 |
| **E** | v4.0.0 共存 | 待定 | AutoForge→AutoFlow 交接协议 | AutoForge 成熟 + 用户拍板 |

> 顺序铁律：**A→B→C→D 线性推进，每阶段 Done 定义不达标不进下一阶段**；E 独立、可永不排期。
> 节奏：**速赢（≤半天）随时插队；≥1 天进版本批次**。每次结构变更同步 `ARCHITECTURE.md`；测试结论进 `04_test/findings-ledger.md`，不新增报告文件。

---

## 12. 决策记录（为何这样定，防反复）

- **为何 AutoFlow 转保守**：AutoForge 已承接"agent 从零创作自动化"，AutoFlow 若再追 agent-authorship 会与 AutoForge 重叠、且违背"产品已成型、只做低成本增量"。AutoFlow 守住"人主导的安全部署/验证/回滚"更值钱、更稳。
- **为何分 Core/Pro**：1880 勘察证明 98% 的 flow 活在 DSL 疆域外（人写创意流），Core 给这些人手术刀；Pro 给不想碰 NR 的小白极简面。两档共用一套 verify_flow 核，不重复造。
- **为何 vhass 不迁 pytest-homeassistant**：v1 重放 NR flow，pytest-homeassistant 只模拟 HA core 无 NR 运行时，域错配 + 维护期禁大重构；收益仅归 AutoForge。
- **为何 v4 延后**：生态共存取决于 AutoForge 成熟度，现在定死会绑架两边；留接口、待拍板。
- **为何补"实体解析决策层"（2026-09-16）**：架构评估确认 `resolve_entity` 检索层已稳（网关侧、不耗 token、fail-closed、`possible_states` 防猜域），但 catalog 缺 `integration`/`connectivity`/`health` 字段，致用户三大痛点（多集成双 entity_id 无优选、弱信号无降权、怪异设备易混淆）未解。属"补决策智能"而非重做检索，符合低成本增量；P0 机械零风险、可随 v2.2 速赢插队。遥测（#15）优先级最高——无它则"成功率"永远拍脑袋、优选/降权效果无法验证。

---

## 13. 开发节奏约定

1. **速赢优先**：≤半天的小改随时插队；1 天以上的进版本批次。
2. **验收门即 Done 定义**：没过验收门不算完成，不合并。
3. **每次代码结构变更同步 ARCHITECTURE.md**；测试结论进 findings-ledger，不新增报告文件。
4. **push 前置**：守卫测试 + 相关回归绿；push 后 `git ls-remote` 权威校验。
5. **文档不写真实内网 IP**（用 `<NAS_IP>` 等占位），否则 `test_no_secrets.py` 红。

## 14. 已完成里程碑（近三次迭代，供对照）

- ✅ **v2.0.11-beta**：竞技场七轮——B20/B22/B23/B24 诚实性修复、F-R5-01 死锁、F-R6.5 数值链路三层修复、JSONata 求值器补全。
- ✅ **2026-09-09**：三回路闭环（knowledge_feedback 回读 / seed_health 守卫 / 效率系数）。
- ✅ **2026-09-10**：记忆联动②③；F-R7-03 media_player 语义对齐；F-R7-02 seed_health 正例实测；竞技场教程 v2.0.0 重写。
- ✅ **2026-09-12**：V-F1~F4 零信任质量闸漏洞修复并部署 NAS prod；R10/R11 竞技场预置。
- ✅ **2026-09-16**：AutoFlow v1 实体解析架构评估（NL→entity_id / 房间→area）——确认检索层稳、哲学与"网关侧机械化不耗 token"一致；定位 catalog 缺集成/健康度字段为真实漏点，产出 P0/P1/P2 决策层方案，落地 v2.3.0 §6。
