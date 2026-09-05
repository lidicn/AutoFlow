# 测试工单：v1.4.1 + v1.4.2 修复回归测试

**版本**: v1.4.2 (commit 2f31bcb, tag v1.4.2)
**测试者**: wb2
**测试环境**: 远端测试机
**测试日期**: 2026-09-03
**优先级**: P0（核心功能修复验证）

---

## 测试范围

本次测试覆盖 v1.4.1 和 v1.4.2 两个版本的修复：

### v1.4.1 修复（授权码 P0 bug）
- NRLayer.update_flow 不存在 → 改为 update_flow_nodes（4处）
- auto_deploy（raw + DSL 两条路径）恢复正常
- 全量回滚恢复正常
- target_tab 改为可选（允许不绑定 tab 走 per_flow 模式）
- 频率限制失败也计数

### v1.4.2 修复（v1.3.3 测试报告缺陷 B/C/D）
- 缺陷 B：撤回节点分类误判（网关节点被误判为用户节点）
- 缺陷 C：撤回后同 flow_id 重部署 409 冲突
- 缺陷 D：/lab/* 路由 404

---

## 前置条件

1. AutoFlow 已更新到 v1.4.2（在线更新或 git pull）
2. Node-RED 运行正常
3. MCP 连接正常，agent 身份码有效
4. WebUI 可正常访问

---

## 第一部分：v1.4.1 授权码修复验证

### T1-1: 授权码自动部署（MCP raw 路径）

**步骤**:
1. 创建授权码：名称="T1-raw"，目标 tab="客厅"，权限=部署，节点阈值=100
2. MCP 调用 `autoflow_deploy_raw`，传入简单 flow（inject→debug，2-3节点），同时传入 `deploy_token="dt_xxx"`
3. 观察返回结果

**预期**:
- [ ] `auto_deploy.ok=true`，`auto_deploy.deployed=true`
- [ ] 返回中有 `flow_id` 和 `label`
- [ ] Node-RED「客厅」tab 中出现该 flow 的节点
- [ ] **不需要**在 WebUI 手动点击部署
- [ ] 授权码列表中「部署次数」+1

---

### T1-2: 授权码自动部署（DSL 路径）

**步骤**:
1. 用同一个授权码（或新建一个）
2. MCP 调用 `autoflow_propose_dsl`，传入简单 DSL，同时传入 `deploy_token="dt_xxx"`
3. 观察返回结果

**预期**:
- [ ] 闸门通过时 `auto_deploy.ok=true`
- [ ] Node-RED 目标 tab 中出现该 flow
- [ ] 不需要人工确认

---

### T1-3: 全量回滚

**步骤**:
1. 用授权码自动部署一个 flow 到目标 tab
2. 记录部署后目标 tab 的节点数
3. 打开该授权码的快照列表
4. 找到部署前的快照，点击「回滚到此」
5. 确认回滚

**预期**:
- [ ] 回滚成功提示（不再报 `'NRLayer' object has no attribute 'update_flow'`）
- [ ] 目标 tab 恢复到部署前的状态（节点数减少）
- [ ] 快照列表中新增一条「回滚前快照」

---

### T1-4: target_tab 可选（不绑定走 per_flow）

**步骤**:
1. 创建授权码：名称="T1-nobind"，**目标 tab 留空**，权限=部署
2. MCP 调用 `autoflow_deploy_raw`，传入简单 flow，同时传入 `deploy_token="dt_xxx"`
3. 观察返回结果和 Node-RED

**预期**:
- [ ] 创建授权码时 target_tab 留空不报错（之前必填会 400）
- [ ] 前端表单标签显示"目标 tab（可选）"，有说明文字
- [ ] 自动部署成功，flow 部署到**新建的独立 tab**（per_flow 模式），而不是某个已有的 tab
- [ ] 返回中 `auto_deploy.target_tab` 为 null 或不存在

---

### T1-5: 频率限制失败也计数

**步骤**:
1. 创建授权码：频率限制=2次/分钟
2. 在 1 分钟内连续调用 3 次自动部署（可以用无效的 flow 让部署失败，或者用同一个 flow）
3. 观察第 3 次结果

**预期**:
- [ ] 前 2 次成功（或失败但计数）
- [ ] 第 3 次 `auto_deploy.ok=false`，错误信息包含"操作频率超限"
- [ ] 失败尝试也计入频率限制（之前仅成功后计数，失败风暴可绕过）

---

## 第二部分：v1.4.2 缺陷 B/C/D 修复验证

### T2-1: 缺陷 B - 撤回节点分类（纯网关节点流）

**步骤**:
1. 部署一个纯网关节点的 flow（只有 af_scene_* 节点，没有用户手动添加的节点）
2. 记录 flow_id
3. 在 WebUI 已部署页面撤回该 flow
4. 观察撤回结果

**预期**:
- [ ] 撤回结果中 `user_nodes_preserved=0`（之前误判为 1）
- [ ] `gateway_nodes_removed` 等于实际网关节点数
- [ ] 如果没有用户节点，走 `deleted_tab` 分支（删除整个 tab），而不是 `trimmed_tab`
- [ ] Node-RED 中该 tab 被删除（如果是 per_flow 模式且无用户节点）

---

### T2-2: 缺陷 B - 撤回精度（混合节点流）

**步骤**:
1. 部署一个 flow 到某个 tab
2. 在 Node-RED 中手动添加一个用户节点（如 inject 节点）到同一个 tab
3. 撤回该 flow
4. 观察撤回结果和 Node-RED

**预期**:
- [ ] `gateway_nodes_removed` 等于网关节点数
- [ ] `user_nodes_preserved=1`（手动添加的节点被保留）
- [ ] Node-RED 中手动添加的节点仍然存在
- [ ] 网关部署的节点被精确删除

---

### T2-3: 缺陷 C - 撤回后同 flow_id 重部署

**步骤**:
1. 部署一个 flow（记录 label 和 flow_id）
2. 撤回该 flow
3. 用**同一个提案**（或相同 label 的 flow）重新部署
4. 观察部署结果

**预期**:
- [ ] 重部署成功（之前返回 409 `{"conflict":true,"error":"NR 中已存在同名 flow…且非本网关部署"}`）
- [ ] 不再报冲突错误
- [ ] Node-RED 中该 flow 正常部署

---

### T2-4: 缺陷 C - 空 tab 可覆盖

**步骤**:
1. 部署一个 flow 到新建 tab
2. 撤回该 flow（tab 变空，只有 tab 节点）
3. 用相同 label 重新部署
4. 观察结果

**预期**:
- [ ] 重部署成功，空 tab 被覆盖
- [ ] 冲突检查识别空 tab 为"撤回后残留"，允许覆盖

---

### T2-5: 缺陷 C - 真实用户 flow 仍受保护

**步骤**:
1. 在 Node-RED 中手动创建一个 tab，label="我的手动流"，添加几个节点
2. 尝试部署一个 label="我的手动流" 的 flow（不 force）
3. 观察结果

**预期**:
- [ ] 部署被拒绝，返回 409 冲突错误
- [ ] 错误信息包含"非本网关部署，避免覆盖"
- [ ] 用户手动创建的 tab 不受影响
- [ ] 空 tab 判断不误伤有节点的用户 tab

---

### T2-6: 缺陷 D - /lab/validate 路由

**步骤**:
1. 调用 `POST /lab/validate`，传入一个合法的 flow JSON（如 inject→debug）
2. 观察返回结果
3. 再传入一个有错误的 flow JSON（如缺少必填字段）
4. 观察返回结果

**预期**:
- [ ] 路由不再 404（之前 404）
- [ ] 合法 flow 返回 `ok=true`，包含 `node_count`、`errors`、`warnings`、`total_issues`
- [ ] 有错误的 flow 返回 `ok=false`，包含错误详情
- [ ] 不落档到提案列表（validate 只是校验，不创建提案）

---

### T2-7: 缺陷 D - /lab/deploy 路由

**步骤**:
1. 调用 `POST /lab/deploy`，传入一个合法的 flow JSON，label="lab-test"
2. 观察返回结果
3. 检查 Node-RED

**预期**:
- [ ] 路由不再 404
- [ ] 返回 `ok=true`，包含 `flow_id`、`node_count`、`deployed_at`
- [ ] Node-RED 中出现 label="lab-test" 的 flow
- [ ] **不需要**在 WebUI 提案页面手动审批（lab/deploy 直接部署）

---

### T2-8: 缺陷 D - /lab/deploys 路由

**步骤**:
1. 先调用几次 /lab/deploy（至少 1 次成功）
2. 调用 `GET /lab/deploys`
3. 观察返回结果

**预期**:
- [ ] 路由不再 404
- [ ] 返回 `ok=true`，包含 `deploys` 数组
- [ ] 每条记录包含 `ts`、`agent`、`label`、`status`、`flow_id`、`node_count`
- [ ] 记录按时间倒序排列
- [ ] 最多保留 50 条

---

## 第三部分：回归测试

### R-1: 正常人工审批流程不受影响

**步骤**:
1. MCP 调用 `autoflow_deploy_raw`，**不传** deploy_token
2. 观察结果

**预期**:
- [ ] 提案正常落档
- [ ] WebUI 提案页面可以看到该提案
- [ ] 用户可以正常手动审批部署

---

### R-2: 授权码 fail-safe 机制不受影响

**步骤**:
1. 用无效授权码 `dt_nonexistent` 尝试自动部署
2. 用已吊销的授权码尝试自动部署
3. 用已过期的授权码尝试自动部署

**预期**:
- [ ] 三种情况都返回 `auto_deploy.ok=false`，`fallback="manual"`
- [ ] 提案正常落档，等待人工审批
- [ ] 不拒绝部署（fail-safe 设计）

---

### R-3: 撤回功能整体正常

**步骤**:
1. 部署多个 flow（per_flow 模式和 single_tab 模式各一个）
2. 分别撤回
3. 观察结果

**预期**:
- [ ] per_flow 模式撤回正常
- [ ] single_tab 模式撤回正常（只移除本 flow 节点，不删除整个 tab）
- [ ] 节点精确删除，不误伤其他 flow

---

## 测试结果记录

| 用例编号 | 用例名称 | 结果 | 备注 |
|----------|----------|------|------|
| T1-1 | 授权码自动部署（raw） | ⬜ | |
| T1-2 | 授权码自动部署（DSL） | ⬜ | |
| T1-3 | 全量回滚 | ⬜ | |
| T1-4 | target_tab 可选（per_flow） | ⬜ | |
| T1-5 | 频率限制失败也计数 | ⬜ | |
| T2-1 | 缺陷B-纯网关节点流撤回 | ⬜ | |
| T2-2 | 缺陷B-混合节点流撤回精度 | ⬜ | |
| T2-3 | 缺陷C-撤回后重部署 | ⬜ | |
| T2-4 | 缺陷C-空tab可覆盖 | ⬜ | |
| T2-5 | 缺陷C-真实用户flow仍受保护 | ⬜ | |
| T2-6 | 缺陷D-/lab/validate | ⬜ | |
| T2-7 | 缺陷D-/lab/deploy | ⬜ | |
| T2-8 | 缺陷D-/lab/deploys | ⬜ | |
| R-1 | 正常人工审批流程 | ⬜ | |
| R-2 | 授权码 fail-safe | ⬜ | |
| R-3 | 撤回功能整体 | ⬜ | |

**结果**: ✅ 通过 / ⚠️ 有问题 / ❌ 失败 / ⬜ 未测试

---

## 问题反馈模板

如发现问题，请按以下格式记录：

```
### 问题 [编号]
- 用例: [T1-1 等]
- 严重程度: [P0阻断 / P1严重 / P2一般 / P3建议]
- 复现步骤:
  1. ...
  2. ...
- 预期结果: ...
- 实际结果: ...
- 错误信息/截图: ...
- 环境: [版本/浏览器/NR版本]
```

---

**测试完成后**: 将本文件重命名为 `TEST_RESULT_v1.4.2_fix_regression.md` 并保存到测试目录。
