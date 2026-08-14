# AutoFlow 项目接手总结与后续开发计划

> 编制时间：2026-08-14
> 依据：`docs/HANDOFF_for_new_session.md`、`README.md`、`DEPLOY.md`、`WHITEBOX_VERIFY_LOOP.md`、`docs/ARCHITECTURE.md`、`docs/CONVERGE_E_NAS_autoflow.md`，以及对 `src/`、`tests/`、`data/`、`mcp_server.py`、`gateway.py`、`webui.py`、`acp_client.py`、`llm_client.py` 的源码通读。
> 结论：**项目代码已成型、功能闭环完整，可直接接手开发；但「git 唯一定源」尚未真正建立，这是接手前要解决的头号前置条件。**

---

## 0. 接手评估结论（TL;DR）

| 维度 | 评估 |
|---|---|
| 代码成熟度 | ★★★★☆ 功能闭环完整，生产已部署（NAS prod 已跑 LLM UI 修复） |
| 架构清晰度 | ★★★☆☆ 分层理念清晰，但 `gateway.py` 8010 行、`dsl_engine.py` 3020 行属巨石模块，维护成本高 |
| 文档完备度 | ★★★★☆ README/ARCHITECTURE/HANDOFF/WHITEBOX 齐全，交接信息密度高 |
| 测试覆盖 | ★★★☆☆ 115 个测试文件 + 离线硬门槛，但当前有 5 个 red（P1，纯测试文件） |
| **git 健康度** | **★☆☆☆☆ 致命短板**：`.git` 存在但 `master` **零提交**，`origin/main` 从未 fetch，收敛 runbook **未执行** |
| 接手风险 | 中等偏高，集中在 git 重构 + 待裁决语义 + 5 测试修复 |

**一句话**：业务代码可放心继续开发；接手第一步不是写功能，而是**把 git 收敛落地**，再清技术债、补 5 个测试、推进 ACP/LLM 收尾。

---

## 1. 项目是什么

AutoFlow 是插在「AI 助手」与「Home Assistant / Node-RED」之间的**中央网关**：
- 独占 HA / Node-RED 凭证，**AI 永远不直接碰你家设备**；
- AI 用一句自然语言 + 5 行语义 DSL 描述场景，网关负责**编译 → 静态校验 → 虚拟 HA 孪生重放自证 → 人工确认闸 → 部署与快照**；
- 批准/升格**只能在网页端**，agent 在接口层面无法自我批准（零信任）。

价值主张三件事：① 实体 ID 不再靠 AI 猜（网关实时查真实设备）；② 上线前必有虚拟重放 + 断言验证；③ AI 没有 HA 令牌， braking 永远在人手里。

---

## 2. 架构与模块划分

### 2.1 三端点能力分层（核心设计）
| 端点 | 能力 | 面向 |
|---|---|---|
| `/mcp` | 编译器路径 + 读 + 提案 + 提交(进确认闸) | 默认用户面 agent（black/white/dual/admin 都连这里，工具按 mode 分层显隐） |
| `/mcp-white` | 上述 + 原生手写部署刀（deploy_raw / modify_flow / commit_ha_service 等） | 进阶/开发 agent（white/dual/admin） |
| `/mcp-admin` | 上述全集 + 运维/测试杠杆（golden/acceptance 评测、网关重启、任务池、缺陷闭环） | 仅管理员 mode=admin |

历史术语：「黑箱」= 编译器路径，「白箱」= 原生手写路径（新文档统一用新词）。

### 2.2 源码模块（`src/autoflow_gateway/`，共 84 个 .py）
> 行数标注为实测 LOC，巨石模块已标 ⚠️。

| 模块 | LOC | 职责 |
|---|---|---|
| `gateway.py` ⚠️ | **8010** | 编排核心：虚拟 HA 重放引擎、golden/acceptance 评测、e2e trace、deploy_raw、verify_flow、提议/审批落盘 |
| `dsl_engine.py` ⚠️ | 3020 | DSL 编译器（意图→NR 节点 + 静态校验） |
| `mcp_server.py` ⚠️ | 2549 | MCP 服务（三端点）+ 身份鉴权中间件 + WebUI 合一 |
| `flow_linter.py` ⚠️ | 2110 | flow 静态 lint（R 系列规则，如 R31 未定义字段引用） |
| `webui.py` ⚠️ | 1823 | WebUI 后端（~60 个 `/api/*` 路由）+ 静态首页托管 |
| `subflows.py` | 998 | 预置子流程 spec 加载器 |
| `task_store.py` | 849 | DSL 验证任务池存储 |
| `proposals.py` | 671 | 提案/经验沉淀（raw→candidate→public，升格落盘公用 skill） |
| `debug_bridge.py` | 618 | NR5.0.1 原生 ws debug 事件旁路订阅（环形缓冲，零炸裂半径） |
| `vhass.py` | 579 | 虚拟 HA（数字孪生 / staging 数据源，**纯标准库**） |
| `llm_client.py` | 476 | 内置 LLM 客户端（OpenAI 兼容 /chat/completions，多后端 fallback，惰性 import httpx） |
| `identity.py` | 468 | MCP 身份层（agent 身份码/存档/拒绝匿名）+ ACP 令牌 |
| `flow_simulator.py` | 461 | flow L2 逻辑可达性仿真 |
| `connections.py` | 410 | WebUI「连接设置」面板，UI 值覆盖 .env 为唯一真相源 |
| `api_specs.py` | 347 | API 能力 spec 加载器 |
| `config.py` | 316 | env 优先配置 + 功能开关（`is_acp_enabled` / `is_task_pool_enabled` / `is_submit_gate_enabled`） |
| `cli.py` | 259 | 脚本/JSON 兜底接口（`autoflow` 命令） |
| `sync.py` / `flow_diff.py` | 201 / 201 | 同步 / flow diff |
| `template_lib.py` | 199 | DSL 模板库（list/render） |
| `device_guard.py` | — | 高危域设备护卫（门锁/水阀升级确认） |
| `defense.py` / `confirm.py` / `ha_layer.py` / `nr_layer.py` / `build_scene.py` / `schemas.py` / `state.py` | — | 防御层 / 确认闸 / HA 受控写 / NR 安全写（无 replace-all）/ 意图→NR / 契约 / 共享态 |
| `acp_client.py` | 172 | ACP 客户端（autoflow → memory-worker 委派；JSON-RPC over HTTP+SSE，纯标准库） |
| `notes.py` / `audit.py` / `telemetry.py` / `command_store.py` / `plan_store.py` / `decision_store.py` / `api_config_store.py` / `errors.py` / `mock_docker_api.py` | — | 笔记 / 审计 / 遥测 / 各 store / 错误处理 / staging 非实体 API 模拟 |
| `lib/ha_client.py` `lib/nr_client.py` `lib/affordance.py` | — | **vendored** HA/NR 客户端（已复制进包，不依赖 skills 目录；加载顺序 vendored→环境变量→skills） |

### 2.3 前端
- `webui/static/index.html`（24KB）+ `app.js`（145KB）+ `style.css`（18KB），响应式九面板（概览/安全闸/提案/已部署/子流程/Agents/诊断/笔记/设置）。
- **注意**：Help 页与部分热修（LLM 气泡/账号池/测试按钮）在 NAS 的 `index.html` 里、未完全进 git（见 §6 风险）。

---

## 3. 技术栈

- **语言**：Python 3.12+（本地实测 3.13.2 已装齐依赖；托管 3.13.12 未装依赖不可用）
- **核心依赖**：`mcp>=1.2`、`pydantic>=2`、`uvicorn>=0.30`、`starlette>=0.37`、`python-dotenv`、`httpx>=0.27`
- **容器**：基于 `ghcr.io/astral-sh/uv:latest`，容器内 `uv` 装包；`./src`、`./skills`、`./data` 绑定挂载 → NAS 改源码 + `docker compose restart` 即生效（配合 Dockerfile `-e` 可编辑安装 + site-packages 符号链接补丁）
- **后端依赖（运行时外部）**：Home Assistant（实体/状态/服务调用）、Node-RED（flow 部署/触发/debug 回读）
- **协议**：MCP（Streamable HTTP 主，SSE 备）；ACP（JSON-RPC 2.0 over HTTP+SSE，对接 memory-worker）；HA/NR REST + websocket
- **前端**：原生 HTML/JS/CSS（无构建步骤）
- **测试**：pytest，`run_tests.py` 统一调度，**离线硬门槛**（mock/FakeNR/FakeHA/本地 vhass/进程内 TestClient），`--live`/`--smoke` 可选

---

## 4. 现有功能清单

### 4.1 编译器路径（默认，产出干净 flow）
- 自然语言设备名 → 跨域实体候选（`autoflow_resolve_entity`，不过滤域，返回 domain/state/possible_states/confidence）
- 实体目录过滤浏览（`list_entities`，分页防上下文炸弹）、强制刷新目录（`refresh_catalog`）
- DSL 模板库（list/render）+ 自助语法帮助（`dsl_help`）
- DSL 提案→编译→静态校验→staging 闸门(vhass 重放断言)→落 raw 提案

### 4.2 原生手写路径（white/admin）
- `deploy_raw` / `validate_flow` / `simulate_flow` / `run_e2e_trace` / `modify_flow` / `commit_ha_service` / `create_subflow` / `set_tab_state` / `verify_flow` / `apply`+`apply_rollback`+`apply_state_from_debug`+`get_trace`

### 4.3 诊断与闭环
- `get_nr_flow`（子流程空壳扫描）、`debug_read`（debug 事件旁路回读）、`trigger_inject`（点火 inject 产生帧）
- `list_pending`（按身份隔离的待确认）、`set_plan`/`request_decision`/`get_decision`（向人类请示）

### 4.4 治理与跨系统
- **提案治理**：经验沉淀 raw→candidate→public，WebUI 升格公用 skill
- **任务池**（admin）：publish/reset/stats/submissions（早期 `auto_*`/`wb_*` 已停用，勿认领）
- **缺陷闭环**：`report_issue` / `list_issues` / `resolve_issue`
- **ACP 反向委派** memory-worker：`delegate_to_memory_worker` + `acp_client`
- **内置 LLM 钩子**：`ask_llm`（OpenAI 兼容多后端 fallback，裸 500 已转 LLMError）

### 4.5 WebUI 控制面（~60 路由，9 面板）
健康检查、配置、连接设置（UI 覆盖 .env）、设备护卫、目录/实体、审计、首跑引导、诊断、Agents CRUD/吊销/重置、ACP 令牌管理、ACP 开关、LLM 配置/对话/测试、安全闸（approve/reject）、提案治理、已部署（undeploy）、flow 触发、笔记、子流程（导入/启停/ensure/bark 安装）、link-apis、首页。

### 4.6 虚拟孪生 staging
- `vhass`（虚拟 HA，REST）/ `mock_docker_api`（非实体 API 模拟）；`seed-vhass` 镜像真实 catalog 进 staging。

---

## 5. 开发环境搭建步骤

### 5.1 本地开发（不触真实设备，推荐）
```bash
cd E:/NAS/autoflow
python -m pip install -e .          # 可编辑安装，autoflow 命令随处可用
python run.py serve                  # 启动 MCP(/mcp,/mcp-white,/mcp-admin) + WebUI(/) :8000
python run.py mcp --no-webui         # 仅 MCP（仍强制身份）
# 或零摩擦：
python -m pip install mcp pydantic uvicorn starlette python-dotenv httpx
python run.py cli discover --keyword 客厅
```
- 配置：复制 `.env.example` 为 `.env` 填 `HASS_TOKEN`/`NR_PASS`/`HASS_SERVER`/`NR_URL`，或设环境变量；WebUI「连接设置」优先级最高。
- `AUTOFLLOW_ENV=staging`（默认，数据落 `data/staging/`，连测试 HA）或 `prod`（连真实设备，写操作需人工确认）。

### 5.2 容器部署（NAS / 生产）
```bash
cd <DEPLOY_DIR>/autoflow-gateway
cp .env.example .env   # 填凭证；或 WebUI 连接设置
docker compose up -d --build
curl -i http://<HOST>:8000/mcp -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```
- 容器内访问宿主 HA 用 `http://host.docker.internal:8123`，**不要写 localhost**。
- NAS 同步走 `autoflow-nas-deploy` 技能（scp 4 文件 + restart + 运行时内省验收），**不在沙箱里跑 git 写**。

### 5.3 测试
```bash
python run_tests.py            # 离线硬门槛（mock/FakeNR/本地 vhass/TestClient）
python run_tests.py --live     # 含 live（需 NR/HA 在线）
python run_tests.py --smoke    # 离线全绿后可选 deepseek++ 黑箱冒烟
python -m pytest tests/test_gateway.py -q   # 单文件
```
⚠️ 必须用「实际运行网关的 python」（系统 3.13.2）；托管 3.13.12 未装依赖不可用。

---

## 6. 遗留任务（按优先级）

| 优先级 | 事项 | 交付物/位置 | 状态 |
|---|---|---|---|
| **P0** | **git 收敛未落地**（见 §7 风险①）：`.git` 零提交、`origin/main` 未 fetch | `docs/CONVERGE_E_NAS_autoflow.md` | 阻塞中 |
| P0 | LLM 助手 agent 身份注入（修「未识别 agent」使 DSL 提案可提交） | `handoff/WORKORDER_DEV_llm_agent_identity_and_chat_input.md` §1 | 待 DEV |
| P1 | 聊天输入框改底部居中固定条、无滚动条（仿 MiMo 截图） | 同上 §2 | 待 DEV |
| P1 | **pytest 5 红修复**（纯测试文件，非生产回归） | `handoff/HANDOFF_DEV_llm_ui_pool_test_apply.md` §2 | 待 DEV |
| P1 | deepseek2api ↔ AutoFlow 内置 LLM 对接（tools 格式） | `handoff/INTEGRATION_deepseek2api_autoflow_llm.md`（转交对方开发者） | 转交中 |
| P1 | ACP 对应 WebUI 前端 + LLM 对接落地 | 分支 `dev/acp-integration` (@ e8b3e63) | 待落地 |
| P2 | `_mask_secret` 把后端名也遮成乱码（cosmetic） | 建议名字走 esc 不 mask | 可选 |
| P2 | A23/A24/A26 修复 | 分支 `dev/round5-cheap-fixes` (@ 1a92435) | 推进中 |

> 注：HANDOFF §5 索引的 `handoff/*.md` 文档在 `D:/Documents/HAOS/AutoFlow/handoff/` 或 `docs/handoff/`，本仓库 `docs/` 仅含 `ARCHITECTURE.md` / `CONVERGE_*.md` / `HANDOFF_for_new_session.md`，需到源路径取详细工单。

---

## 7. 已知问题、技术债与风险点

### 🔴 风险①（头号，致命）：git 唯一定源未建立
- 现状：`E:/NAS/autoflow/.git` 存在，但 `git log` 显示 `master` **没有任何提交**，`git branch -a` 为空 → `origin/main` **从未 fetch**，CONVERGE runbook 的 `reset --mixed origin/main` + commit + push **未执行**。
- 含义：文档声称的「E:/NAS/autoflow 是带 git 的唯一定源」**目前不成立**；本机只是一份未版本化的代码快照。
- 影响：分支 `dev/round5-cheap-fixes`、`dev/acp-integration` 在本克隆里不可见（仅存在于 recovery 仓 `E:/af_recov*` 或 origin），无法在本机直接续开发。
- **接手第一步必做**：在真实终端（非沙箱，`dangerouslyDisableSandbox`）按 `docs/CONVERGE_E_NAS_autoflow.md` 执行 fetch → reset --mixed → 防泄漏核验 → commit → push。提交前务必 `git status` 确认无 `.webui_token`/`.env`/`data/llm_config.json`/Bark key/NAS IP 泄漏。

### 🟠 风险②：巨石模块，维护风险高
- `gateway.py` 8010 行、`dsl_engine.py` 3020、`mcp_server.py` 2549、`flow_linter.py` 2110、`webui.py` 1823。虚拟重放引擎、golden/acceptance 评测、e2e trace、deploy 全挤在 `gateway.py` 一个类里。
- 策略：**产品已成型，不做大型架构重构，仅采纳低成本/高价值增量改进**（用户既定原则）。后续若有改动，优先在边界新增小函数/模块，避免横向扩 `gateway.py`。

### 🟠 风险③：源码树内残留备份目录（易误读）
- `src/autoflow_gateway.bak-20260806-203201/`（整包副本）、`src/autoflow_gateway/webui/static/app.js.bak-20260806-005756`、`src_backup_before_gate_20260809/`、`src_backup_before_p0_20260805/`。
- 这些已被 `.gitignore`（`*.bak-*/`、`src_backup_*/`）排除，不会进版本库，但留在工作树易混淆。接手后建议清理（移到仓库外归档）。

### 🟡 风险④：CRLF / LF 辨伪
- NAS 与本地均为 CRLF，`git show` 是 LF → 跨格式 md5 必不等。验收用 `diff --strip-trailing-cr` 或同格式比，别被 md5 假阳性骗。

### 🟡 风险⑤：vhass 仅实现 REST，无 websocket
- NR 若经 HA websocket 订阅状态变化，需 vhass 支持 HA websocket 协议（当前没有）。推荐 staging 触发器用 NR `inject` 节点（手动/网关注入）或用 `POST /api/trigger` 注入后断言 vhass 状态，**不依赖实时事件流**（ARCHITECTURE.md 已注明）。

### 🟡 风险⑥：`*.json` 全局忽略 + 白名单的脆弱性
- `.gitignore` 用 `*.json` 全局忽略 + 白名单放行 `src/autoflow_gateway/data/*.json`/`examples/*.json`。实测需提交的 json 仅 `nr_subflows/history/subflows_built.json`（已被 `src/autoflow_gateway/nr_subflows/` 规则排除，系有意）与 golden 场景（在 `tests/golden/scenarios.md`，非 json）。收敛提交前**仍需复核**是否漏掉任何运行必需的 json（如新增测试 fixture）。

### 🟡 风险⑦：Help 页 / 部分热修未进 git
- NAS `index.html` 的 Help 页（NAS 独有热修）与 LLM UI 修复在活树，但 HANDOFF 明确「Help 页未进 git」。收敛时以 NAS 活树为最新真相，勿用旧 git 版本覆盖。

### 🟢 技术债（已知存量，非阻塞）
- live 实例上一批白箱 tab 缺 entityId（刷 `ConfigError`），已隔离到 `quarantine/`，不影响新 flow 验证（WHITEBOX_VERIFY_LOOP.md）。
- `test_e2e_trace.py:80` 的 `delete_flow` 缺 `allow_prod` 形参（stub 债，A26 范围）。

---

## 8. 待裁决项（需你拍板）

### ⚠️ c4_replay_semantics（`_replay_zero_policy()`，gateway.py:720）
- **现状**：默认 `fail_closed`。即闸门因「unevaluable JSONata」或「分支被判恒假」导致本步 **0 个 HA 意图 + 0 个外部调用**被重放时，**不得报验证通过**——因为 0 重放=什么都没验证，静默 pass 是假过。
- **可选**：`AUTOFLOW_REPLAY_ZERO_POLICY=warn_only` → 只告警、保留放行（可用性优先）。
- **这是唯一明确待你终裁的语义变更**。终裁后**只改 `_replay_zero_policy()` 默认值或环境变量取值，不动闸门主体**（代码已留 hook）。
- **我的建议**：保持 `fail_closed` 默认（与零信任定位一致），仅在 staging 调试期按需设 `warn_only`。

---

## 9. 后续开发计划（路线图）

### 阶段 0 — 接手前置（必做，阻塞一切）
1. **落 git 收敛**：真实终端按 CONVERGE runbook 执行 fetch→reset→防泄漏核验→commit→push；验证 `git ls-remote origin main` 权威。
2. **取回特性分支**：`git fetch origin dev/round5-cheap-fixes dev/acp-integration`（或自 recovery 仓导入），确认 `@ 1a92435` / `@ e8b3e63` 可续。
3. **清理工作树**：移出 `*.bak-*` / `src_backup_*` 目录到仓库外归档。
4. **建立本地 staging 验证环境**：起 vhass + mock_api，跑 `run_tests.py` 确认真实红数（5 红基线）。

### 阶段 1 — 收尾已知缺口（低成本/高价值）
5. **修复 pytest 5 红**（P1）：纯测试文件，按 `HANDOFF_DEV_llm_ui_pool_test_apply.md` §2 改法，不引入生产回归。
6. **裁决 c4_replay_semantics**：默认维持 `fail_closed`（建议），写入决策记录。
7. **LLM 助手 agent 身份注入**（P0）：修「未识别 agent」使 DSL 提案可提交，闭环 `WORKORDER_DEV_llm_agent_identity_and_chat_input.md` §1。
8. **聊天输入框 UI**（P1）：底部居中固定条、无滚动条。

### 阶段 2 — ACP / LLM 落地
9. **ACP WebUI 前端 + LLM 对接**：推进 `dev/acp-integration`，把 `/acp` 开关、令牌管理、委派可视化做完整。
10. **deepseek2api 对接**：tools 格式对齐，转交对方开发者后做联调验收。
11. **A23/A24/A26**（dev/round5-cheap-fixes）：按 `--no-ff` 合并，`push` 后刷新 tracking refs。

### 阶段 3 — 稳健性（按需）
12. **提交门控加固常态化**：WIP 备份 + `gc.auto=0` + `fsck` 校验 + pre-commit hook（沙箱 git 写已证实不可靠，所有写操作走真实终端）。
13. **本地工作流纪律**：改码在 `E:/NAS/autoflow` → 真实终端 commit/push → `autoflow-nas-deploy` 同步验收。
14. （可选）逐步把 `gateway.py` 的评测/e2e/deploy 拆为独立模块，降低单文件风险（仅在边界改动，不做大重构）。

---

## 10. 接手第一步 CheckList

- [ ] 真实终端执行 git 收敛（fetch → reset --mixed origin/main → 防泄漏核验 → commit → push）
- [ ] `git ls-remote origin main` 确认远程权威
- [ ] 取回 dev/round5-cheap-fixes、dev/acp-integration 分支
- [ ] 清理 `*.bak-*` / `src_backup_*` 残留目录
- [ ] 起 vhass + mock_api，跑 `run_tests.py` 记录红数基线
- [ ] 拍板 c4_replay_semantics（建议维持 fail_closed）
- [ ] 阅读 `handoff/` 下 P0/P1 工单原文（D:/Documents/HAOS/AutoFlow/handoff/）
- [ ] 确认 NAS prod 活树为最新真相，避免被旧 git 覆盖

---

## 附：关键路径速查
- 接口面：`src/autoflow_gateway/mcp_server.py`（MCP 工具）、`webui.py`（~60 `/api/*` 路由）
- 编排核心：`gateway.py`（8010 行，含重放/评测/e2e/deploy）
- 编译器：`dsl_engine.py`（3020 行）
- 虚拟孪生：`vhass.py` + `mock_docker_api.py`
- 协议对接：ACP `acp_client.py`、内置 LLM `llm_client.py`
- 部署：`docker-compose.yml` / `Dockerfile` / `install.sh`；NAS 同步 `autoflow-nas-deploy` 技能
- 接手纪律：`docs/CONVERGE_E_NAS_autoflow.md`（git 收敛）、`WHITEBOX_VERIFY_LOOP.md`（白箱闭环）
