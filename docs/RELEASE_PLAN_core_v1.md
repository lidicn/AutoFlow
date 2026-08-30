# AutoFlow 发行版开发计划（Core v1.0 → 公测 → 迭代）

> 2026-08-30 制定。前提共识：当前用户只有 lidicn 一人，处于闭门造车阶段。
> 策略：先做核心版 → hassbian 论坛推广 → 以真实用户热度决定后续投入。
> 路线：核心版（含方向1）→ 方向3 token 优化 → staging NR → 方向2 子流程商店（门控）→ ACP 前端。

---

## 产品定位（两路径并存，非替代）

| | 安全路径（现有网关） | 专家路径（核心版，本次发行物） |
|---|---|---|
| 面向 | 普通用户 agent | lidicn 本人 / 信任主体 |
| 写入方式 | 语义 DSL → 编译 → verify → WebUI 批准 → 部署 | NR JSON 直写，skill 纪律 + nr_client 护栏 |
| 批准闸 | WebUI（人类唯一闸门） | 无（用户本人即批准人） |
| 自证 | vhass 虚拟孪生重放 | inject → context 回读 → apply 自愈闭环 |
| 凭证 | 只留网关 | core_config.json 本地自管（agent 引导配置） |
| 共同红线 | **用户手工流一律只读**；af_* 命名空间 = agent 所有权 | 同左（skill 黄金法则强制） |

---

## Phase 0 — 发行底座（repo 准备）

1. repo 新建 `core/` 目录作为发行物单一真相源：
   - `core/nr_client.py`（从用户级 skill 提升为 repo 权威版；skill 侧已有 `_resolve_authoritative` 版本仲裁机制，天然支持"repo 为权威、本地副本自动升级"）
   - `core/skill/SKILL.md` + `core/skill/scripts/`（专用 flow-writer skill）
   - `core/INSTALL.md`（一句话安装 prompt 文案 + 手动安装兜底步骤）
   - `core/VERSION` + `core/CHANGELOG.md`
2. **公开前专项清理（关键，易漏）**：
   - 全 `core/` 扫描内网 IP（192.168.2.x / 100.112.138.64）、NAS 路径、账密、`NR_PASS` 硬编码默认值（nr_client.py 现有硬编码默认密码，**必须改为必填环境变量**）；
   - `test_no_secrets.py` 门禁接入 core 目录；
   - 确认 GitHub 仓库公开可匿名 raw 访问。
3. 版本策略：core 独立版本号（v1.0 起），与网关版本解耦。

## Phase 1 — 核心版本体（v1.0）

### 1a. nr_client.py 增强（小改，4 项）
| 命令 | 作用 | 对应 |
|---|---|---|
| `doctor` | 读配置 → login 验证 → list_flows → 输出环境报告；agent 部署后自检验收 | 安装闭环 |
| `inventory` | 全 tab 紧凑概览（id/label/节点数/启用态/af_* 归属标记），只读 | 方向1 落点 |
| `write_flow` | 一键封装：snapshot → lint → diff 预览 → 单 flow PUT → 回读节点数校验 → 日志（现散在多处） | 写入安全 |
| `compact` | 输出去 x/y 坐标等渲染字段，省 token | 方向3 |

### 1b. 自愈闭环：inject → debug 回读 → apply（v1 用 context 桥，零新依赖）
- **方案B（v1 采用）**：agent 在验证点临时接一个 function 节点 `global.set("af_dbg", msg)`，`inject_flow` 触发 → `get_context("global","af_dbg")` 回读（nr_client 已有 get_context，纯 HTTP）→ 与期望比对 → 不符则修 → 重跑。上限 N 轮（默认 3）防死循环。
- **方案A（v2 可选升级）**：移植网关 `debug_bridge.py`（订阅 NR 原生 `ws://<nr>/comms` debug 事件流，#644 已实证）→ 真实 debug 输出，保真度更高；成本是 stdlib 实现 ws 客户端或引入依赖。
- nr_client 新增 `inject_and_read` 原语封装 B 路径。

### 1c. 专用 skill（主要工作量：SKILL.md 重写）
1. 黄金法则：af_* 命名空间 = agent 所有权；用户手工流**只读**（inventory 只列不改）；prod 闸默认拦；写前必快照。
2. 标准写入流程：build（v6 节点构建器）→ lint → diff 预览 → `write_flow` → `inject_and_read` 验证 → 不符则修 → 重验（自愈闭环）。
3. 已知坑清单固化：1880/1990 双实例不共享、`POST /flow` 忽略 body id（必须 PUT 单 flow + 台账回写）、v6 schema、stub 404 语义。
4. 配置自管指引：agent 引导用户提供 NR 地址/账密 → 写本地 `core_config.json`（gitignored）→ `doctor` 验收。

### 1d. 一句话安装指令（INSTALL.md，论坛可复制）
形态 = 一段用户粘给任意 agent 的 prompt，agent 自主完成：
1. 从 repo raw URL 下载 `nr_client.py` + `skill/`；
2. 落位 `~/.workbuddy/skills/autoflow-core/`（WorkBuddy；文档同时给通用 agent 的手动路径）；
3. 向用户要 NR 地址/账密 → 写 `core_config.json`；
4. 跑 `doctor` 自检 → 报告「已就绪 / 缺什么」。
验收标准：用户粘贴一句话 → agent 全程自主 → doctor 全绿。

### 1e. 守卫测试
`core/` 附最小 pytest（fake NR server）：doctor / inventory 只读性 / write_flow 快照与回读校验 / inject_and_read context 桥 / no-secrets 扫描。

## Phase 2 — hassbian 论坛推广（验证期）

- 帖子三件套：场景 demo（一句话建 flow + 自愈验证录屏或 JSON 证据）、安装 prompt、安全红线声明（只读手工流）。
- 收集指标：安装成功率、配置卡点分布、复访/追问热度。
- **门控产出**：热度达标 → 立项 Phase 5 商店；不达标 → 核心版迭代为止。

## Phase 3 — 方向3 token 优化（可与 Phase 2 并行）

- mcp-white 工具面白名单子集（现 47 个 → 使用者最小可用集，运维刀隔离到 admin 面）；
- 网关工具输出 compact（大 JSON 截断/摘要化）；
- nr_client compact 模式（1a 已含，此处收尾网关侧）。

## Phase 4 — staging NR 实例（基础设施，一石三鸟）

- NAS docker 起 NR staging（建议 :1991），种子设备数据；
- F12 真机 e2e 从借 prod 1990 切到 staging；
- 核心版可选 verify 桥：`run_staging_gate(flow=<NR JSON>)` 网关加小入口，agent 写入前可选自证；
- staging 保真度补齐（事件时序类用例）。

## Phase 5 — 子流程商店（**门控：Phase 2 热度达标才立项**）

- spec 库（3~5 个高频子流程起步）+ `generate_subflow_from_spec` 安装命令 + 版本/更新机制。

## Phase 6 — ACP WebUI 前端 + LLM 对接

- 产品闭环「AI 写 DSL → 批准 → 部署」前端落地，安全路径面向公众的最后一块。

---

## 待拍板决策点

| # | 决策 | 建议 |
|---|---|---|
| D1 | 一句话安装的目标 agent 范围：WorkBuddy 专用 or 通用 agent | v1 按 WorkBuddy skill 约定为主，INSTALL.md 附通用手动步骤 |
| D2 | 自愈闭环 v1 用 context 桥（零依赖）还是移植 debug_bridge（ws，保真度高） | v1 context 桥先跑通，v2 再评估移植 |
| D3 | repo 公开与脱敏：core 公布前必须确认仓库 public 且无内网信息/硬编码凭据 | 公开前专项清理 + test_no_secrets 扩面 |
| D4 | 核心版是否要 verify 桥（依赖 Phase 4 staging） | v1 不做，Phase 4 落地后作为可选增强 |

## 里程碑顺序

P0 底座 → P1 核心版 v1.0 → P2 论坛推广（验证）→ P3 token 优化（并行）→ P4 staging NR → P5 商店（门控）→ P6 ACP 前端。

P1 验收 = 一句话安装指令粘给全新 agent，全程无人干预完成部署 + doctor 全绿 + 用真实设备建一条 PC/显示器灯联动 flow 并自愈闭环验证通过。
