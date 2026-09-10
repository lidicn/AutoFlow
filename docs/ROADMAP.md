# AutoFlow 版本路线图与开发计划

> **本文件是路线图的唯一有效版本**（2026-09-10 起）。
> 旧路线图（`00_overview/ARCHITECTURE_AND_ROADMAP.md`、`03_dev/ROADMAP.md`）已作废删除指针。
> 维护纪律：每完成一项打 ✅ 并注明 commit；新增项必须带验收门，无验收门不立项。

## 当前基线

- **版本**：v2.0.11-beta（竞技场 beta）｜ 竞技场七轮：R7 客厅轮 6/6 全 fv，三机制 + 记忆联动②③已上线
- **架构真相源**：[`02_architecture/ARCHITECTURE.md`](02_architecture/ARCHITECTURE.md)
- **MCP 连接层评估结论**（2026-09-10，对照通用 MCP 成熟度框架）：
  安全授权超额（三面板过滤 + 身份守卫 + WebUI-only 审批）；缺口集中在**发现类**（OAuth metadata、schema 双真相源）与**运营类**（限流）——均为部署成熟化问题，非架构问题。

---

## v2.0.12 —— 连接层成熟化（速赢批次）

| # | 项 | 内容 | 验收门 | 规模 |
|---|---|---|---|---|
| 1 | ✅ **OAuth 资源发现端点**（commit 40cbed4） | 补 `/.well-known/oauth-protected-resource`（RFC 9728，含 `/mcp` 后缀变体）与 authorization-server metadata（RFC 8414）两个只读匿名端点；resource=autoflow，scopes 映射三面板（normal/expert/admin）；401 补 `WWW-Authenticate: Bearer resource_metadata=...` | ✅ MCP 客户端**零手工配置**发现鉴权方式（三端点 200 + 401 带发现头）；`/.well-known` 404 消失。★诚实铁律：不虚构 OAuth 端点（守卫锁死） | ~半天 |
| 2 | **Schema 单一真相源**（A20/A27 收尾） | 工具 schema 从函数签名自动生成；`_ACP_TOOLS` 手写 JSON schema 退役；两套账并一套 | 手写 schema 删除；新增**一致性守卫测试**（tools/list 与签名生成结果 diff=0，防漂移回归） | 1~2 天 |
| 3 | **每身份限流/超时** | 反代层按 token 令牌桶 + 请求超时（**零网关代码改动**） | 单 token 连发打满被 429；正常使用无感 | ~2 小时 |

## v2.1.0 —— 记忆联动 + 运维债（体验批次）

| # | 项 | 内容 | 验收门 | 前置 |
|---|---|---|---|---|
| 4 | **memory-agent 联动消费点** | 竞技场战报/agent 画像自动落 memory-agent（旧名 memory-worker，2026-09 改名交接 1549e5a，生态口径统一）；agent 经 ACP 查"我上轮学到什么"。网关侧 `delegate_to_memory_worker` 通道已存在，只缺竞技场消费点 | agent 能跨轮查到自己的历史经验，且**有真实读取行为**（不做只写不读） | ACP WebUI 前端落地 |
| 5 | **94 历史测试红清帐** | 期望漂移修复专项（修测试不改产品代码）；完成后 A/B 基线克隆回归流程退役 | 全量回归 failed=0，或每条失败附归属注释；回归判定零成本 | 无 |
| 6 | **竞技场教程** | 新 agent 入场引导（DSL 速查 / 机制说明 / T0 流程 / 常见拒绝原因） | 新 agent **只靠教程**完成 T0 并成功提交一题 | 无 |

## v2.2+ —— 条件触发（不主动排期）

| # | 项 | 触发条件 | 备注 |
|---|---|---|---|
| 7 | **B1 独立 staging NR** | 需要做写类真机 e2e 闭环时立项 | **B1 解除前，禁止声称任何写类验证"已闭环"**（既有纪律）；竞技场验收走 vhass 孪生，不受 B1 阻塞 |

## ⛔ 明确不做（负面清单，防止范围膨胀）

- OpenTelemetry 接入（结构化日志 `_slog`/`_telemetry` 已够用）
- MCP Tasks 扩展 / 长任务 Handle（当前无长任务场景）
- 多租户 / 企业网关方向（单家庭单网关形态）
- replace-all / delete-all 类工具（**安全不变量**：爆炸半径上限，永不做）
- 大型架构重构（增量改进原则）

---

## 已完成里程碑（近三次迭代，供对照）

- ✅ **v2.0.11-beta**：竞技场七轮迭代——B20/B22/B23/B24 诚实性修复、F-R5-01 死锁、F-R6.5 数值链路三层修复、JSONata 求值器补全
- ✅ **2026-09-09**：三回路闭环（knowledge_feedback 回读 / seed_health 守卫 / 效率系数）
- ✅ **2026-09-10**：记忆联动②③（agent 战绩画像端点 / 考官校准回路 / suggest_fix 单出口合并）；F-R7-03 media_player 语义对齐；F-R7-02 seed_health 正例实测闭环

## 开发节奏约定

1. **速赢优先**：≤半天的小改随时插队；1 天以上的进版本批次
2. **验收门即 Done 定义**：没过验收门不算完成，不合并
3. **每次代码结构变更同步 ARCHITECTURE.md**；测试结论进 findings-ledger，不新增报告文件
4. **push 前置**：守卫测试 + 相关回归绿；push 后 `git ls-remote` 权威校验
