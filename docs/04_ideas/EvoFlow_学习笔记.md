# EvoFlow 学习笔记（外部项目调研）

> 调研对象：https://github.com/EvovexAI/EvoFlow
> 调研日期：2026-09-12
> 目的：评估对 AutoFlow（HA DSL 网关）可借鉴的设计点
> 信息源：README、docs/user/explanation/{agent-system,ecosystem-comparison,why-evoflow,asset-center}.md
> 许可：PolyForm Noncommercial 1.0.0（源码可见，**非 OSI 开源**；商用需书面授权）→ **可学设计，不可直接搬运代码**

## 1. EvoFlow 是什么

- 定位：「面向长任务的原生 Agent Runtime + 控制平面」。桌面 EvoPanel + Gateway(FastAPI) + LangGraph Harness。
- **不是 HA / 智能家居专用**，是通用长任务 Agent 编排产品（早期参考 DeerFlow 并系统性重构为独立产品，对标 Hermes / Codex / OpenClaw）。
- 主打两场景：① 个人固定业务工作流产品化（应用中心多节点画布→发布→填参再跑）；② 多 AI 员工组织协作 + 关键审批。

## 2. 核心架构要点

- **四层架构**（依赖单向，下层不感知上层）：
  `EvoPanel(桌面/Web 控制平面)` → `Gateway(FastAPI 业务聚合)` → `LangGraph 运行时 / Channels(IM)` → `Harness(evoflow.*：工具/技能/记忆/子代理/沙箱/Supervisor)`。
- **Agent = 模型 + 工具 + 技能(SKILL.md) + 记忆(`<memory>` 注入前 15 条事实) + 子代理(`task` 委派)**；可组合 / 可配置 / 可隔离（每 thread 独立环境）。
- 13 个中间件按固定顺序编排；运行时参数 `config.configurable` 控制 `thinking_enabled / model_name / is_plan_mode / subagent_enabled`。
- **ThreadState**：`sandbox / thread_data / artifacts`(去重合并) `/ todos / viewed_images`。
- **资产中心（自我进化）**：画像 / 记忆 / 经验(craft) / 反思 存本地文件；对话里说「记住 / 沉淀 / 反思」即落盘；可「导出 Pack 应用到某员工/Agent」。对话告一段落会自动把偏好+可复用经验整理进资产；长对话压缩后用资产检索补细节。

## 3. 与 AutoFlow 对照矩阵

| 维度 | AutoFlow（现状） | EvoFlow | 差距 / 可借鉴 |
|---|---|---|---|
| 流程表示与自证 | HA DSL：编译→静态校验→**vhass 孪生重放自证** | 通用 Agent 工作流（多节点画布） | 我们用 DSL+孪生做"物理安全自证"，比其 OS 沙箱更针对 HA |
| 安全闸门 | WebUI 人工批准（核心不变量：MCP 不暴露批准） | 智能体员工关键审批 + 安全中心 | 已对齐；其"看板+打回"更产品化 |
| **记忆 / 自我进化** | memory-agent 灵感（arena 读侧快照 / ACP 兜底） | 资产中心：经验沉淀+反思+画像+记忆，可导出 | ★ 我们只有"只读灵感提示"，缺"反思/经验沉淀+可导出资管" |
| 配置热更新 | ArenaManager 每次读盘(hot reload)、skills bind-mount | mtime 热更新 | 已对齐 |
| 原子写 | tasks.json `os.replace` 临时写再 rename | 线程隔离 + 临时写再 rename | 已对齐 |
| 工具面精简 | MCP 最小集，隐藏运维刀 | 工具按场景渐进暴露 | 已对齐（用户强偏好） |
| 懒初始化 | vhass 实例按 arena 懒创建 | MCP/沙箱/模型懒加载 | 已对齐 |
| **任务可观测** | 日志 + inject_debug 回读 apply | 任务中心看板/DAG/总结验收/observability 面板 | ★ 缺"验收看板+任务总结+打回"产品化 |
| **工作流复用** | 每次重新生成 DSL | 应用中心：多节点工作流发布→填参再跑 | ★ 缺"已验证 DSL 模板库" |
| **架构分层** | gateway.py 9172 行单体（引擎+产品壳耦合） | Harness/App 严格分离 | ★ 单体是债，应逐步解耦 |
| 费用 / 多账号 / SSO | 无 | 费用账本、SSO、多账号 | 非核心，暂不需 |

## 4. 值得借鉴的点（按性价比排序）

### A. 反思 / 经验沉淀闭环（强化 R10/R11 记忆联动）— 高价值
- EvoFlow：每次对话告一段落自动整理"偏好+可复用经验"进资产；长对话压缩后用资产检索补细节。
- AutoFlow 现状：memory-agent 只提供"灵感"（过往成功 DSL 片段），是**只读提示**，无"反思 / 失败复盘 / 经验沉淀"，且写入侧 `_push_memory_report` 永不影响主流程。
- 借鉴：在 WebUI 批准部署（或 arena 验收）后，自动跑一次"反思"——成功/失败原因、哪些 DSL 写法稳、哪些设备组合易踩坑——沉淀为**可检索、可导出**的经验资产。直接补 R10/R11 记忆机制短板。
- 落地：扩 memory-agent 写入侧，增加 `reflection` 类型；arena 验收后触发；与现有灵感读取通道共存。

### B. 已验证 DSL 模板库（工作流产品化）— 中高价值
- EvoFlow 应用中心：多节点工作流发布为模板，填参再跑。
- AutoFlow 现状：每个自动化都从零生成 DSL，重复模式（"传感器超阈值开开关"）反复重写。
- 借鉴：把验收通过的 DSL 抽象成参数化模板（设备角色+阈值+动作），用户/agent 填参即生成，减少重生成与闸门失败。
- 落地：arena 或 WebUI 增加"模板"概念；与 A 的经验资产联动（模板由经验提炼）。

### C. 任务验收看板（arena 生命周期产品化）— 中价值
- EvoFlow 任务中心：看板 / DAG / 任务总结 / 验收 / 打回，执行层可见。
- AutoFlow 现状：arena tasks 有 `locked/available/feedback`，但无"总结+打回"产品化看板，FFL 靠 FINDINGS.md 人工记录。
- 借鉴：arena 任务增加"验收总结 + 打回(reject)"状态与可见看板，降低 FFL 协作摩擦。
- 落地：WebUI arena 面板增强（已有侧栏，加验收列与驳回原因）。

### D. Harness / App 解耦（还 gateway.py 单体债）— 中价值 / 长期
- EvoFlow 五原则之一：`evoflow.*` 不引用 `app.*`；上层长业务，内核稳定。
- AutoFlow 现状：gateway.py 9172 行，DSL 引擎 + 产品 API + WebUI 后端耦合。
- 借鉴：把 DSL 编译 / 校验 / 孪生(vhass) 抽为独立 engine 包，gateway.py 只做产品聚合与路由。
- 落地：记为技术债清理候选；与用户"不做大型重构"原则一致，**本轮不做**。

## 5. 不值得 / 暂不适用的点（诚实标注）

- **智能体员工排班 / 组织协作、IM 多渠道（飞书/微信/Slack）、资源市场、费用账本、SSO**：与 AutoFlow"聚焦 HA DSL 网关、凭证只留网关、agent 永不接触"的使命无关，不照搬。
- **通用 Agent 长任务编排（Goal 后台自驱）**：AutoFlow 是"写 DSL→部署物理设备"，不是通用任务引擎；过度泛化会稀释定位（用户明确"产品已成型，不做大型架构重构"）。
- **桌面 EvoPanel 控制平面**：AutoFlow 已有 WebUI + MCP，形态够用。

## 6. 许可与合规提醒

- EvoFlow 是 **PolyForm Noncommercial**（源码可见但非 OSI 开源），商用需书面授权。
- 结论：本次仅做设计调研与理念借鉴；**不复制其代码**，不引入其依赖（LangGraph 等）。AutoFlow 保持自有 DSL-first 路线。
- 若未来借鉴某具体机制（如反思闭环），用我们自己的实现（memory-agent / arena 已有基座）。

## 7. 结论

- 有值得学的地方，但集中在 **4 个具体点**（A 反思沉淀 / B DSL 模板库 / C 验收看板 / D 引擎解耦），且 A、B、C 都直接补强现有 R10/R11 记忆与 arena 工作；D 是长期债。
- AutoFlow 已自发践行 EvoFlow 的多条工程原则（原子写、配置热更新、工具面精简、懒初始化、人类批准闸门、孪生即沙箱），说明方向与其"控制平面产品化"理念一致，只是产品化程度更低、且**缺"自我进化 / 经验资产"这一环**。
- 建议：把 **A（反思 / 经验沉淀闭环）** 作为下一步记忆机制增强的高优先候选，纳入 ROADMAP；B/C 作为 arena/WebUI 体验增强候选；D 记为技术债。
