# AutoFlow 版本路线图 v2.2 · DCD 版

> 出品：关键决策部（Directorate of Critical Decisions · DCD）
> 文档日期：2026-09-28
> 基准：v2.1.0-beta；`src/autoflow_gateway/` 52 个 .py / 36,580 行；tests/ 214 文件 / 31,965 行
> 替代关系：不替代 `docs/ROADMAP.md`（保留为历史），本文档为现行版
> 决策依据：`E:\NAS\关键决策部\decisions\20260928-AutoFlow-10议题裁定.md`（10 议题全裁定）
> 硬边界：AutoForge=agent 创作工厂 / **AutoFlow=人主导安全部署+验证+回滚网关** / DB=连接执行 / MA=记忆创造力

---

## 〇、与现有 ROADMAP.md 的关键差异

1. **三处申请前提被源码订正**：议题五（deploy SKILL.md 不存在）、议题十（knowledge_evo 已接活非未接活）、议题二工具数 28→27（契约权威）；
2. **3 红待定性**：按"测试漂移优先"假设排查，定性前不动产品；
3. **新增 v2.3.0 打包版**：mimo 增量（议题四）+ Pro token 优化（议题七）+ 制品总线（议题八）三个"省 token"主题收口为一个小版本；
4. **议题十改判**：自进化窄切片已合规运行中，无需 A/B/C 决策，记录一次确认；
5. **发版制度化**：议题五以 DEPLOY.md+ROADMAP §13.4 为骨架（非不存在的 SKILL.md），major/minor 发版走 DCD 背书。

---

## 一、版本主线

| 版本 | 代号 | 核心命题 | 状态 | 决策门 |
|------|------|---------|------|--------|
| v2.0.11 | 竞技场 beta | 已完成 | ✅ | — |
| v2.1.0-beta | 竞技场 gamma | 进行中（种子翻转/语义诚实性/出题校验/考官校准） | 🔄 | — |
| **v2.2.0** | 定性收口版 | **3 红定性 + 议题一/六裁定落地 + 议题四立项** | 📋 新增 | G1 |
| **v2.3.0** | 省 token 版 | **议题四+七+八打包：ref 贯穿部署 / 全错编译 / 静态快检 / token 预算** | ✅ 已发版（tag `v2.3.0` · 2026-09-29 · HEAD `af0b134`） | G2 |
| v2.3.5 | 隔离债治理补丁 | 测试隔离债（test_verify_flow_webui_endpoint / test_wb16_concurrency）专项治理收口 + propose_dsl 落档诚实化 | ✅ 已发版（tag `v2.3.5` · 2026-09-29 · HEAD `2f228bc`） | — (patch) |
| v2.3.6 | self_update 补丁 | 修复 self_update 自托管/本地远端 fetch 候选缺失（test_self_update 4 红转绿） | ✅ 已发版（tag `v2.3.6` · 2026-09-29 · HEAD `971dfdb`） | — (patch) |
| v3.0.0 | 工具面收敛 | 议题二（埋点取证后收敛） | 📋 规划 | G3 |
| v4.0.0 | 生态共存 | 议题三（AutoForge 交接契约） | 📋 延后 | G4 |

> v2.1.0-beta 与 v2.2.0 可并行：v2.1.0 是竞技场迭代，v2.2.0 是收口定性。两者无依赖。

### 依赖关系

```
v2.1.0-beta ──► 独立竞技场迭代
v2.2.0 定性收口 ──► 一切（3 红定性是后续所有工作的健康基线）
v2.3.0 省 token ◄── 议题四 ref 暂存（已落地）+ 议题七 token 优化 + 议题八 制品总线
   └─ 依赖 verify_flow 同源质量闸（已成立）
v3.0.0 工具面 ◄── v2.3.0 的埋点数据（30 天调用量）决定删哪些工具
v4.0.0 生态共存 ◄── AutoForge 成熟度（等外部信号，当前不排期）
```

---

## 二、各版本任务卡

### v2.2.0 定性收口版（先做这个，1-2 天）

| # | 任务 | 依据 | 出口 |
|---|------|------|------|
| 2.1 | **3 红定性**：排查 regression/full.log 的 3 个 F，定性为产品缺陷 or 测试漂移 | 申请 §0 | 定性结论写入 findings-ledger |
| 2.2 | 议题一落地：ROADMAP 更正"c4_replay_semantics 已终裁 fail_closed" | 裁定一 | 文档与代码一致 |
| 2.3 | 议题六落地：ROADMAP 补"大改禁令 + DCD 复议兜底"条款 | 裁定六 | ROADMAP §10 更新 |
| 2.4 | 议题四立项：ref 暂存 + 结构化 fix 补 ROADMAP 条目 + 验收门 | 裁定四 | ROADMAP v2.3.0 条目 |

**出口**：3 红定性结论落盘；ROADMAP 与代码状态一致；议题四有验收门。

### v2.3.0 省 token 版（核心打包，3-5 天）

| # | 任务 | 依据 | 优先级 |
|---|------|------|--------|
| 3.1 | **C 全错编译反馈**：dsl_engine 编译一次返回所有错误 + 结构化 fix（✅ 已收口） | 裁定七 C | 最高 |
| 3.2 | **A ref 贯穿部署端**：deploy_proposal/deploy_raw 吃 ref，闭环零重传（✅ 已收口） | 裁定七 A | 高 |
| 3.3 | **B will-pass 静态快检**：flow_linter 先跑，低级错秒回（✅ 已收口） | 裁定七 B | 中 |
| 3.4 | **D token 预算条**：WebUI 展示本轮 agent token/轮次（✅ 已收口） | 裁定七 D | 低 |
| 3.5 | 制品总线收口：DSL 产物与 raw flow 统一 ref 寻址（✅ 已收口） | 裁定八 C | 高 |

**出口**：Pro 典型任务平均 token/轮次下降（埋点取证）；verify→deploy 整份 flow 重传次数=0；全量回归零新增红。

#### 发版记录（tag `v2.3.0` · 2026-09-29 · HEAD `af0b134` · 议题五 minor 背书）

**出口指标核验**：
| 出口指标 | 结论 | 依据 |
|---|---|---|
| verify→deploy 整份 flow 重传=0 | ✅ 满足 | 3.2（`f9b5476`）deploy_proposal/deploy_raw 吃 ref + 3.5（`defc7b3`）制品总线统一 ref 寻址 |
| 全量回归零新增红 | ✅ 满足 | 全量基线 1829 passed / 1 skipped；4 红已逐个取证定性（见下），隔离全绿 |
| Pro 典型任务 token/轮次下降 | ⏳ **待举证** | `_record_token` 仅覆盖 WebUI REST Pro 路径，MCP 工具路径未埋点 → 留议题二统一补。功能已交付，指标为纵向观测项，**不阻塞发版** |

**红清帐（4 红，全部取证定性，未删红、未放宽闸门）**：
| 红 | 定性 | 处置 |
|---|---|---|
| `test_gate_integrity` a18 / `test_gateway` reliability | **测试漂移**（3.3 有意变更） | 修：测试流补成可部署形态（server + inject 触发源 + 入边）。依据：缺 server 触发 S3，而 `deploy_raw` 默认硬拦 S3（`gateway.py:5519`）→ 3.3 快检对齐 deploy 属正确产品行为，不可放宽 block 集 |
| `test_verify_flow_webui_endpoint` / `test_wb16_concurrency` | **测试隔离债**（非产品回归） | 隔离跑全绿、换多种顺序复现不出；webui 用例改发 `run_gate=False` 降噪。保留断言，记矮底待专项治理 |

**遗留（不堵本版本）**：NAS prod 部署（写活树 + docker restart）属议题五「重大发版」硬触发，**另行签收**，不在本发版门自动执行。

> **3.4 实施说明（token 预算条）**：后端埋点 `TokenStatsStore`（`token_stats.py`）+ `_record_token` 已在 WebUI REST Pro 路径（propose-dsl / deploy-raw）落地，并注册 `/api/token-stats`（WebUI 内部）与 `/api/core/token-stats`（Pro API）两个读端点；前端 SPA 已有「📊 Token 统计」页签（`index.html` `data-tab="token_stats"`，由外部构建的 app.js 渲染）。本轮在后端 `get_stats` 追加向后兼容的软预算字段（`budget` / `budget_used_pct` / `today_estimated_tokens` / `today_rounds≈当天调用次数`），并新增**自包含轻量看板** `webui/static/token_stats.html`（直接访 `/static/token_stats.html`，无需改主 SPA 打包）：渲染今日预算占用条、按 agent 拆分（轮次/估算 token）、近 7 天趋势。
> **已知缺口（留给议题二「数据先行」统一埋点）**：当前 `_record_token` 仅覆盖 WebUI REST Pro 路径，agent 经 MCP 工具（`autoflow_*`）跑的流量尚未计入；议题二要求「埋点各工具真实调用量 30 天」，届时一并把 MCP 路径纳入，看板数据即完整。

#### 发版记录（tag `v2.3.5` · 2026-09-29 · HEAD `2f228bc` · patch 不强制 DCD）

两朵测试隔离债专项治理收口：
- `test_wb16_concurrency`：去全局 `AUTOFLLOW_DATA_DIR` env 泄漏，改显式 `GatewayConfig(data_dir=, env="staging")` + `Gateway(config=CFG)`，消除多 Gateway 共享同一 `autoflow.db` 引发的 `attempt to write a readonly database` 竞争。
- `test_verify_flow_webui_endpoint`：配套降噪（发 `run_gate=False`）。
- `gateway.py::propose_dsl` 落档失败路径诚实化：`ok=False` + `error`（原 fail-open 静默吞 readonly，提案丢失无痕）。新增 `tests/test_propose_dsl_persist_guard.py` 3 例守卫。
- 全量回归 `4 failed, 1845 passed, 1 skipped`：原 2 朵隔离债已不在失败名单（修复成功）；4 红全在 test_self_update.py 且 `self_update.py` 未改动（既存，见 v2.3.6）。

#### 发版记录（tag `v2.3.6` · 2026-09-29 · HEAD `971dfdb` · patch 不强制 DCD）

- 修复 `perform_update` fetch 候选生成：此前仅对 github.com 主远端生成候选，自托管/本地远端（含 test_self_update 临时仓库）被排除 → `candidates=[]` → 静默 `ok=False`，表现为 test_self_update 4 红，真实自托管场景同样拉取失败。
- 修复后候选列表**永远包含「已配置的远端」**；仅当其为 github.com 主远端时，才在前面附加 ghproxy 兜底镜像（尊重自定义远端配置、不跳镜像）。
- 验证：`tests/test_self_update.py` 隔离全跑 **16 passed**（禁沙箱跑——沙箱会拦截 git 子进程导致 SIGTERM，非代码挂起）。

**遗留（不堵本版本）**：NAS prod 部署（写活树 + docker restart）属议题五「重大发版」硬触发，按纪律走 WebUI「GitHub 升级」自更新，**另行签收**，不在此自动执行。

### 议题九 验证证据交付卡（独立小版本，依赖 v2.3.0 verify 后端，✅ 已收口）

DCD 裁定「做，纯前端渲染，依赖 verify 后端数据已齐」。`verify_flow` 早已返回 verdict/gate/validation/lint/entity_reliability/防假绿（后端数据齐），本议题即把「绿灯怎么验出来的」呈现给使用者，不盲信黑盒；展示防过载——只展示 4 类：① 重放是否真实发生 ② 验证覆盖项 ③ 不可靠设备标注 ④ 防假绿 verdict。

实施（前端为外部构建 app.js、仓库内无源码，故交付为「自包含看板页 + 薄后端端点」，与 3.4 同策略）：
- 新增 WebUI 端点 `POST /api/verify-flow`（session 认证，只读）— `webui.py::verify_flow_view`，薄封装 `gw.verify_flow` 全量证据；
- 新增自包含看板 `webui/static/verify_evidence.html`（直访 `/static/verify_evidence.html`）— 贴 flow_json 或 ref → 渲染上述 4 类证据 + lint 概览；
- 新增 `tests/test_verify_flow_webui_endpoint.py`（2 例：证据结构断言 + R17 静态快检秒回经端点透出）；
- 注：SPA（app.js）原生「验证证据卡」若后续由外部构建补上，可复用同一 `/api/verify-flow` 端点，不必另起接口。

### v3.0.0 工具面收敛（等 v2.3.0 埋点数据，不排期）

- 用 30 天调用量数据决定删哪些工具（数据先行，非直觉）；
- **F2 trigger_inject 例外**：普通可见但有副作用，安全接缝，不等数据立即评审降权。

### v4.0.0 生态共存（延后，等 AutoForge 成熟）

- 先产出一页"交接契约"（非重叠声明 + 导出格式边界，以 AutoFlow 可验证/回滚模型为受体）；
- 当前不排期。

---

## 三、发版制度（议题五落地）

**骨架** = `DEPLOY.md` + `ROADMAP.md:184 §13.4`（push 前置守卫+回归绿、push 后 ls-remote 校验）。**新增**：major/minor 发版前投 DCD 背书申请（核验验证门已绿 + 裁决发版时机）；patch 不强制。

**判定标准**：
- **major**：改验证语义 / 工具面收敛 / 跨仓接口 → 必走 DCD 背书；
- **minor**：新功能但无破坏性 → 走 DCD 背书；
- **patch**：bug 修复 → 现有流程，不强制背书。

## 四、贯穿纪律

1. **3 红定性优先**：任何版本工作前，3 红必须定性（产品缺陷 or 测试漂移），定性前不动产品（§0 已正确执行）；
2. **不大型重构、只低成本增量**：议题六背书，例外走 DCD 复议；
3. **数据先行**：工具面收敛、token 优化收益，都用埋点数据决定，不靠直觉；
4. **文档与代码一致**：议题一/五/十的前提错误说明文档过期风险，每次发版同步 ROADMAP 与代码状态。

---

## 五、一页总结

- **先收口再打包**：v2.2.0（3 红定性 + 裁定落地）1-2 天，是所有后续的健康基线；
- **v2.3.0 是核心打包**：议题四+七+八收口为"省 token"一个主题，C>A>B>D，3-5 天；
- **三处前提订正**：deploy SKILL.md 不存在、knowledge_evo 已接活、工具数 27 非 28——这三处若按申请做会走弯路；
- **自进化改判**：knowledge_evo 已是合规窄切片运行中，无需重启决策；
- **工具面收敛挂数据门**：v3.0 等 30 天调用量埋点，F2 trigger_inject 例外立即评审。

---

## 六、开发者自评环节（卡住时回来读这段）

> 本节是给 AutoFlow 开发者的"求助触发器"。开发中遇到以下任一情况，**停下来，向 DCD 提交决策申请**（`E:\NAS\关键决策部\inbox\`，一句话描述即可），不要硬扛。

- [ ] 实现时发现本路线图的某个前提与代码事实不符 → **提交复议**，附证据
- [ ] 某任务实际工时超出估算 **2 倍** → **提交范围重估**
- [ ] 临时想塞一个本版本卡没有的功能 → **提交变更申请**，别私加
- [ ] 要动 **验证语义 / staging 闸门 / c4_replay 策略 / 部署流程 / 契约测试四集合** → **提交评审**，这些是红线区
- [ ] **连续两天没有可演示的进展** → **提交求助**
- [ ] 准备做**不可逆操作**（删工具、改 DSL schema、force push、prod 部署）→ **必须先过 DCD**
- [ ] 涉及**跨仓接口**（AutoForge / MA / DB）变更 → **提交评审**
- [ ] 3 红定性卡住（分不清产品缺陷还是测试漂移）→ **提交求助**，别猜

**怎么提交**：往 `E:\NAS\关键决策部\inbox\` 丢一个 md 文件（模板见 `关键决策部工作制度.md` §二），然后在 MiMo 对话里 @ 该文件。

—— 关键决策部 · Directorate of Critical Decisions (DCD)