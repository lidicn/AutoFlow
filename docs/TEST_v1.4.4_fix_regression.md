# 测试工单：v1.4.3 + v1.4.4 修复回归验证

**版本**: v1.4.4 (commit 56ea404, tag v1.4.4)
**测试者**: wb2
**测试环境**: 远端测试机
**测试日期**: 2026-09-04
**优先级**: P1（修复验证 + 回归）

---

## 测试范围

本次测试覆盖 v1.4.3 和 v1.4.4 两个版本的修复：

### v1.4.3 修复
- /lab/* 路由缺少 /api 前缀导致 404
- 授权码频率限制不生效（rate_window_start 未保存）
- 空 tab 覆盖语义未实现（另建副本而非覆盖原 tab）

### v1.4.4 修复（观察项）
- 撤回 fail-atomic（NR 写失败时不清账本）
- 部署归一化副作用（反向补全 domain/service 改写用户节点）

---

## 前置条件

1. AutoFlow 已更新到 v1.4.4
2. Node-RED 运行正常
3. MCP 连接正常
4. WebUI 可正常访问

---

## 第一部分：v1.4.3 修复验证

### T1-1: /api/lab/validate 路由

**步骤**:
1. 调用 `POST /api/lab/validate`，传入合法 flow JSON（inject→debug）
2. 调用 `POST /api/lab/validate`，传入有错误的 flow JSON
3. 确认旧路径 `/lab/validate`（无 /api 前缀）返回 404

**预期**:
- [ ] `/api/lab/validate` 返回 200，不再 404
- [ ] 合法 flow 返回 `ok=true`，包含 `node_count`、`errors`、`warnings`、`total_issues`
- [ ] 有错误的 flow 返回 `ok=false`，包含错误详情
- [ ] 旧路径 `/lab/validate` 返回 404（确认路由已迁移）
- [ ] 不落档到提案列表

---

### T1-2: /api/lab/deploy 路由

**步骤**:
1. 调用 `POST /api/lab/deploy`，传入合法 flow JSON，label="lab-test-v143"
2. 检查 Node-RED 是否出现该 flow
3. 调用 `GET /api/lab/deploys` 查看历史

**预期**:
- [ ] `/api/lab/deploy` 返回 200 `ok=true`，包含 `flow_id`、`node_count`、`deployed_at`
- [ ] Node-RED 中出现 label="lab-test-v143" 的 flow
- [ ] 不需要 WebUI 手动审批（lab/deploy 直接部署）
- [ ] `/api/lab/deploys` 返回历史记录，包含本次部署

---

### T1-3: /api/lab/deploys 路由

**步骤**:
1. 先调用 2-3 次 /api/lab/deploy
2. 调用 `GET /api/lab/deploys`
3. 检查返回结果

**预期**:
- [ ] 返回 `ok=true`，包含 `deploys` 数组
- [ ] 每条记录包含 `ts`、`agent`、`label`、`status`、`flow_id`、`node_count`
- [ ] 记录按时间倒序排列
- [ ] 最多保留 50 条（如有超过 50 条的测试环境可验证）

---

### T1-4: 授权码频率限制生效

**步骤**:
1. 创建授权码：频率限制=2次/分钟，节点阈值=100
2. 在 1 分钟内连续调用 3 次自动部署（用简单 flow，确保前 2 次成功）
3. 观察第 3 次结果
4. 检查授权码的 rate_window_start 是否被设置（通过 GET /deploy-tokens 查看 stats）

**预期**:
- [ ] 前 2 次自动部署成功
- [ ] 第 3 次 `auto_deploy.ok=false`，错误信息包含"操作频率超限"
- [ ] 授权码 stats 中 `rate_window_start` 不为 null（之前永远为 null）
- [ ] `rate_window_count` 正确累计
- [ ] 等待 1 分钟后可以再次部署

---

### T1-5: 频率限制失败也计数

**步骤**:
1. 创建授权码：频率限制=2次/分钟
2. 在 1 分钟内连续调用 3 次自动部署，其中第 2 次用无效 flow（让部署失败）
3. 观察第 3 次是否被限流

**预期**:
- [ ] 失败尝试也计入频率限制（之前仅成功后计数，失败风暴可绕过）
- [ ] 第 3 次被限流（即使第 2 次失败）
- [ ] 错误信息包含"操作频率超限"

---

### T1-6: 空 tab 覆盖（不另建副本）

**步骤**:
1. 部署一个 flow 到新建 tab，label="空tab覆盖测试"
2. 撤回该 flow（tab 变空，只有 tab 节点）
3. 用相同 label="空tab覆盖测试" 重新部署
4. 检查 Node-RED 中的 tab 数量

**预期**:
- [ ] 重部署成功，无 409 冲突
- [ ] **不另建**「空tab覆盖测试 (网关副本)」tab
- [ ] 原空 tab 被覆盖更新（tab id 不变）
- [ ] Node-RED 中只有一个 label="空tab覆盖测试" 的 tab（不是两个）

---

### T1-7: 非空用户 tab 仍受保护

**步骤**:
1. 在 Node-RED 中手动创建一个 tab，label="我的手动流"，添加几个节点（非空）
2. 尝试部署一个 label="我的手动流" 的 flow（不 force）
3. 观察结果

**预期**:
- [ ] 部署被拒绝，返回 409 冲突错误
- [ ] 错误信息包含"非本网关部署，避免覆盖"
- [ ] 用户手动创建的 tab 和节点完好无损
- [ ] 空 tab 判断不误伤有节点的用户 tab

---

## 第二部分：v1.4.4 修复验证

### T2-1: 撤回成功时正常清账本

**步骤**:
1. 部署一个 flow（per_flow 模式，无用户节点）
2. 撤回该 flow
3. 检查撤回结果和 flow_catalog

**预期**:
- [ ] 撤回成功 `ok=true`，action="deleted_tab"
- [ ] `gateway_nodes_removed` 等于实际网关节点数
- [ ] `user_nodes_preserved=0`
- [ ] flow_catalog 中该 flow 被移除
- [ ] Node-RED 中该 tab 被删除

---

### T2-2: 撤回失败时不清账本（fail-atomic）

**步骤**:
1. 部署一个 flow
2. 构造一个 NR 写失败的场景（如暂时断开 NR，或构造畸形节点结构）
3. 尝试撤回该 flow
4. 检查撤回结果和 flow_catalog

**预期**:
- [ ] 撤回失败 `ok=false`（之前返回 `ok=true` + `nr_warning`）
- [ ] 错误信息包含"NR 侧撤回失败，账本未清理"
- [ ] **flow_catalog 中该 flow 仍然存在**（之前会被清除）
- [ ] Node-RED 中该 flow 仍然存在（NR 写失败，节点未删）
- [ ] 注册表与 NR 实际状态一致（都认为该 flow 还在）

**注**：如果无法构造 NR 写失败场景，可通过代码审查确认逻辑：NR 写失败时不调用 `self.state.remove_flow()`。

---

### T2-3: 撤回失败后可重试

**步骤**:
1. （接 T2-2）撤回失败后，修复 NR 侧问题
2. 再次尝试撤回该 flow
3. 观察结果

**预期**:
- [ ] 第二次撤回成功（因为账本还在，flow_catalog 中仍有记录）
- [ ] 之前如果清了账本，第二次撤回会报"flow 不存在"（fail-atomic 修复后不会）
- [ ] flow_catalog 中该 flow 被移除

---

### T2-4: 部署归一化不反向补全 domain/service

**步骤**:
1. 在 Node-RED 中手动创建一个 api-call-service 节点，只填 action="input_text.set_value"，不填 domain/service
2. 把这个节点放到一个 tab 中
3. 通过网关部署一个 flow 到同一个 tab（混合模式或单 tab 模式）
4. 部署后检查手动创建的节点的 domain/service 字段

**预期**:
- [ ] 手动创建的节点的 domain/service **仍然为空**（之前会被补为 domain="input_text", service="set_value"）
- [ ] 节点功能正常（v7 格式只需要 action 字段）
- [ ] 网关部署的节点不受影响（编译产物已有 domain/service/action 三字段）

---

### T2-5: 部署归一化仍正向补全 action

**步骤**:
1. 构造一个 api-call-service 节点，只有 domain="light"、service="turn_on"，没有 action 字段
2. 通过网关部署这个 flow
3. 部署后检查节点的 action 字段

**预期**:
- [ ] 节点的 action 字段被补全为 "light.turn_on"（正向补全仍然生效）
- [ ] 节点版本被升为 v7
- [ ] 节点功能正常

---

## 第三部分：回归测试

### R-1: 授权码自动部署（raw + DSL）

**步骤**:
1. 创建授权码（目标 tab 绑定）
2. MCP 调用 autoflow_deploy_raw + deploy_token
3. MCP 调用 autoflow_propose_dsl + deploy_token

**预期**:
- [ ] 两条路径都自动部署成功
- [ ] 不需要人工审批
- [ ] 授权码统计正确更新

---

### R-2: 授权码 fail-safe

**步骤**:
1. 用无效授权码部署
2. 用已吊销授权码部署
3. 用已过期授权码部署

**预期**:
- [ ] 三种情况都返回 `auto_deploy.ok=false`，`fallback="manual"`
- [ ] 提案正常落档，等待人工审批

---

### R-3: 全量回滚

**步骤**:
1. 用授权码部署一个 flow
2. 查看快照列表
3. 回滚到部署前的快照

**预期**:
- [ ] 回滚成功
- [ ] tab 恢复到部署前状态
- [ ] 回滚前自动创建新快照

---

### R-4: 撤回精度（混合节点）

**步骤**:
1. 部署一个 flow 到某个 tab
2. 在 Node-RED 中手动添加一个用户节点到同一 tab
3. 撤回该 flow
4. 检查用户节点是否保留

**预期**:
- [ ] 网关节点被精确删除
- [ ] 用户节点保留
- [ ] tab 不被删除（trimmed_tab 模式）

---

### R-5: 正常人工审批流程

**步骤**:
1. MCP 调用 autoflow_deploy_raw，不传 deploy_token
2. 在 WebUI 提案页面手动审批部署

**预期**:
- [ ] 提案正常落档
- [ ] WebUI 提案页面可见
- [ ] 手动审批部署成功

---

## 测试结果记录

| 用例编号 | 用例名称 | 结果 | 备注 |
|----------|----------|------|------|
| T1-1 | /api/lab/validate 路由 | ⬜ | |
| T1-2 | /api/lab/deploy 路由 | ⬜ | |
| T1-3 | /api/lab/deploys 路由 | ⬜ | |
| T1-4 | 授权码频率限制生效 | ⬜ | |
| T1-5 | 频率限制失败也计数 | ⬜ | |
| T1-6 | 空 tab 覆盖（不另建副本） | ⬜ | |
| T1-7 | 非空用户 tab 仍受保护 | ⬜ | |
| T2-1 | 撤回成功时正常清账本 | ⬜ | |
| T2-2 | 撤回失败时不清账本 | ⬜ | |
| T2-3 | 撤回失败后可重试 | ⬜ | |
| T2-4 | 部署归一化不反向补全 domain/service | ⬜ | |
| T2-5 | 部署归一化仍正向补全 action | ⬜ | |
| R-1 | 授权码自动部署（raw+DSL） | ⬜ | |
| R-2 | 授权码 fail-safe | ⬜ | |
| R-3 | 全量回滚 | ⬜ | |
| R-4 | 撤回精度（混合节点） | ⬜ | |
| R-5 | 正常人工审批流程 | ⬜ | |

**结果**: ✅ 通过 / ⚠️ 有问题 / ❌ 失败 / ⬜ 未测试

---

## 问题反馈模板

```
### 问题 [编号]
- 用例: [T1-4 等]
- 严重程度: [P0阻断 / P1严重 / P2一般 / P3建议]
- 复现步骤:
  1. ...
  2. ...
- 预期结果: ...
- 实际结果: ...
- 错误信息/截图: ...
```

---

**测试完成后**: 将本文件重命名为 `TEST_RESULT_v1.4.4_fix_regression.md` 并保存到测试目录。
