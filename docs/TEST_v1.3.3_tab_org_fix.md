# 测试工单：v1.3.2 + v1.3.3 修复验证

**工单编号**：TEST-v1.3.3-tab-org-fix
**创建时间**：2026-09-03
**创建人**：dw
**测试人**：wb2（远端测试机）
**优先级**：高
**状态**：待测试

---

## 测试目标

验证 v1.3.2 和 v1.3.3 的修复：
- v1.3.2：修复 wb2 测试发现的 P0/P1 问题（tab-org API 500、保存设置 405、P4 target_tab MCP 未暴露）
- v1.3.3：P4 混合模式 WebUI 目标 tab 选择器

---

## 测试环境

- 测试机：\\ <SHARE_IP>\share\AutoFlowTestv2
- AutoFlow 版本：需在线更新到 v1.3.3
- Node-RED：需可访问
- 测试前务必备份 Node-RED flows

---

## 测试用例

### 1. v1.3.2 P0 修复验证

#### 1.1 /tab-org/status 正常返回
- 步骤：GET /api/tab-org/status
- 预期：200，返回 current_mode/per_flow_count/single_tab_count/total_flows
- 结果：

#### 1.2 /tab-org/migrate 参数校验
- 步骤：POST /api/tab-org/migrate {target_mode: "bogus"}
- 预期：400，提示 target_mode 必须是 single_tab 或 per_flow
- 结果：

#### 1.3 遗留记录兼容
- 前置：有 v1.3.0 前部署的 flow（缺 tab_org_mode 字段）
- 步骤：GET /api/tab-org/status
- 预期：200，不崩溃，遗留记录按 per_flow 统计
- 结果：

### 2. v1.3.2 P1 修复验证

#### 2.1 WebUI 保存设置
- 步骤：WebUI → 设置 → 高级设置 → 切换 Tab 组织模式 → 保存
- 预期：保存成功，提示"已保存"，/api/config 中 tab_org_mode 已更新
- 结果：

#### 2.2 状态服务不可用时显示错误
- 步骤：（模拟 tab-org/status 失败）查看高级设置页面
- 预期：显示红色错误提示卡片，不静默吞错
- 结果：

#### 2.3 P4 target_tab MCP 暴露
- 步骤：MCP tools/list 查看 autoflow_deploy_raw 的 inputSchema
- 预期：包含 target_tab 参数
- 结果：

#### 2.4 P4 target_tab MCP 部署
- 步骤：MCP 调用 autoflow_deploy_raw，指定 target_tab="测试tab"
- 预期：提案落档成功，提案 content 中包含 target_tab 字段
- 结果：

### 3. v1.3.3 P4 WebUI 目标 tab 选择器验证

#### 3.1 部署对话框显示 tab 选择器
- 步骤：WebUI → 提案 → 点击「部署到 NR」
- 预期：显示自定义 modal，包含目标 tab 下拉选择器
- 结果：

#### 3.2 tab 列表加载
- 步骤：查看目标 tab 下拉选择器中的选项
- 预期：包含"按当前模式自动"、"AutoFlow 集中 tab"、已有 tab 列表、"新建 tab"
- 结果：

#### 3.3 选择已有 tab 部署（混合模式）
- 步骤：选择一个已有的 tab → 确认部署
- 预期：flow 部署到指定 tab 中，flow_catalog 中 tab_org_mode=mixed
- 结果：

#### 3.4 新建 tab 部署
- 步骤：选择"新建 tab" → 输入名称 → 确认部署
- 预期：创建新 tab 并部署 flow 到该 tab
- 结果：

#### 3.5 留空按当前模式部署
- 步骤：选择"按当前模式自动" → 确认部署
- 预期：按当前 tab_org_mode 部署（per_flow=独立tab / single_tab=AutoFlow集中tab）
- 结果：

#### 3.6 提案已有 target_tab 时默认选中
- 步骤：MCP 部署时指定 target_tab → WebUI 查看该提案的部署对话框
- 预期：目标 tab 选择器默认选中提案指定的 tab
- 结果：

#### 3.7 子流程部署不显示 tab 选择器
- 步骤：部署一个子流程提案
- 预期：使用原生 confirm 对话框，不显示 tab 选择器（子流程不走 tab 组织模式）
- 结果：

### 4. 回归测试

#### 4.1 per_flow 模式部署/撤回正常
- 结果：

#### 4.2 single_tab 模式部署/撤回正常
- 结果：

#### 4.3 撤回精度（不误伤用户节点）
- 结果：

#### 4.4 其他 WebUI 页面不受影响
- 结果：

---

## 已知限制

1. P4 混合模式的自动修复功能在混合模式下尚未完整验证（建议后续单独测试）
2. 单 tab 模式下的 flow 坐标冲突检测尚未实现（顺序分配，理论上不会冲突）

---

## 测试结果汇总

| 用例 | 结果 | 备注 |
|------|------|------|
| 1.1 status 正常返回 | ☐ | |
| 1.2 migrate 参数校验 | ☐ | |
| 1.3 遗留记录兼容 | ☐ | |
| 2.1 WebUI 保存设置 | ☐ | |
| 2.2 状态错误提示 | ☐ | |
| 2.3 target_tab MCP 暴露 | ☐ | |
| 2.4 target_tab MCP 部署 | ☐ | |
| 3.1 部署对话框 tab 选择器 | ☐ | |
| 3.2 tab 列表加载 | ☐ | |
| 3.3 选择已有 tab 部署 | ☐ | |
| 3.4 新建 tab 部署 | ☐ | |
| 3.5 留空按当前模式部署 | ☐ | |
| 3.6 提案 target_tab 默认选中 | ☐ | |
| 3.7 子流程不显示 tab 选择器 | ☐ | |
| 4.1 per_flow 回归 | ☐ | |
| 4.2 single_tab 回归 | ☐ | |
| 4.3 撤回精度回归 | ☐ | |
| 4.4 其他页面回归 | ☐ | |

**测试人**：wb2
**完成时间**：
**总体结论**：☐ 全部通过 / ☐ 有问题需修复

---

## 问题记录

（测试过程中发现的问题请记录在此）
