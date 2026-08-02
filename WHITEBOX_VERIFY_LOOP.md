# 白箱 Flow「先验证后部署」闭环（Agent Runbook）

> 适用：白箱 agent 经 `/mcp-white` 提交 Node-RED flow 时。
> 目标：所有 flow 在 `deploy_raw` 前，先在 staging 实例实机验证「能不能跑通」，不通就自己改、再验，直到通过才上线。

## 核心工具（网关已具备，白箱+admin 双注册，黑箱 403）

- `autoflow_run_e2e_trace(flow_json | dsl, trigger_json?, expected_path_json?, expected_postconditions_json?, live=false)`
  - 真实部署到 staging 实例 → 触发 inject / 把事件入口(test-double)点燃 → 抓**逐节点 trace** → 与期望路径比对 → **自动回滚（0 残留）**。
  - 返回 `verdict`：`通过` / `断点` / `拦截`，附 `report.reached / missing / runtime_errors / breakpoint` 和原始 `trace`。
- `deploy_raw(flow_json, agent_id, target="staging", ...)`
  - 真正落 NR。若宿主机给网关服务设了 `AUTOFLLOW_WHITEBOX_REQUIRE_E2E=1`，则 `deploy_raw` 内部会**先强制跑一次 E2E**，verdict≠通过 直接拒绝部署（fail-open：验证基建故障时仍放行）。

## 闭环（agent 每次提交都走一遍）

1. **构建** white-box flow（原始 NR flow JSON，`{nodes:[...]}`）。
   - 事件驱动流（server-state-changed 等）无需 inject——E2E 会把入口替换成合成触发(test-double)点燃下游。
   - 碰 HA 的动作节点用 `server: "REPLACE_WITH_HA_SERVER"` 占位，网关自动回填真实 HA server id。
2. **验证**：调 `autoflow_run_e2e_trace(flow_json=...)`，读 `verdict` + `report`：
   - `通过` → 进入第 4 步。
   - `断点` → 看 `report.missing / failed_at / runtime_errors`，定位哪个节点没收到消息 / 报错（如 `ConfigError: An entity is required` = 缺 entityId），**改 flow** 后回到第 2 步。
   - `拦截` → 看 `reasons`（多为缺 inject 且无可转换事件入口、或结构问题），修结构后重验。
3. **（可选）声明期望**：传 `expected_path_json`（节点 id 数组）或 `expected_postconditions_json`（HA 状态断言），让 verdict 更严格。
4. **部署**：`verdict=通过` 后调 `deploy_raw(flow_json=..., target="staging")`（或 prod）。

## 常见断点速查

| 现象 | 含义 | 修法 |
|---|---|---|
| `ConfigError: An entity is required` / `ValidationError: "entityId" is required` | server-state-changed / api-current-state 缺 entityId | 补 `entityId` 字段 |
| `runtime_errors` 含 `Cannot read properties of undefined` | 节点引用了不存在的配置/上游字段 | 检查 wires / msg 字段名 |
| `missing=[某节点]` | 信息流未到达该节点（上游分支/条件挡住） | 检查上游 switch/change 条件、连线 |
| 部署阶段 `拦截`：`wires` 数组数 ≠ output 数 | 节点 `wires` 格式不对（如 debug 用 `"wires":[]` 而非 `[[]]`） | 终端 1-output 节点用 `"wires":[[]]`；0-output 节点避免做 trace 终点 |

## 隔离与回滚

- E2E 默认 `live=false`：**不留在 NR**，跑完即删临时 flow + 清 trace 上下文，零残留。
- 需要保留现场排查时再传 `live=true`（会留在 NR，记得手动清理）。
- 当前已知存量技术债：live 实例上有一批白箱 tab 缺 entityId（会刷 `ConfigError`），已隔离到 `quarantine/`，不影响新 flow 验证。
