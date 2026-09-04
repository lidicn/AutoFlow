## 1.4.4 (2026-09-04)
- 【健壮性修复】撤回 fail-atomic：NR 侧删除/更新失败时不再清账本，保持注册表与 NR 实际状态一致。之前失败仍清账本，导致"注册表说已删但 NR 实际没删"的不一致，出现孤儿边界注释残留、网关节点实际未删等问题。现在失败时返回明确错误（ok=False），用户可修复 NR 侧问题后重试撤回。
- 【副作用修复】部署归一化不再反向补全 api-call-service 节点的 domain/service：`_normalize_api_call_service` 之前会对所有 api-call-service 节点做 action ⇄ domain/service 双向补全，包括用户手动添加的节点（如「💾 存档」节点的空 domain/service 被补为 input_text/set_value），属部署副作用。v7 格式只需要 action 字段，domain/service 是可选的，反向补全非必须。现在只正向补全 action（从 domain+service 推出），不反向补全，避免改写用户节点原始字段。

## 1.4.3 (2026-09-04)
- 【P1 修复】缺陷 D /lab/* 路由仍 404：v1.4.2 注册的路由缺少 `/api` 前缀（注册为 `/lab/validate`，前端调用 `/api/lab/validate`）。改为 `/api/lab/validate|deploy|deploys`，三路由恢复正常。
- 【P1 修复】授权码频率限制完全不生效：`validate_token` 中重置时间窗口时设置了 `rate_window_start=now` 但未调用 `_save_tokens` 持久化，导致 `rate_window_start` 永远为 null，限流永不拦截。修复为重置后立即保存。
- 【P2 修复】空 tab「覆盖」语义未实现：v1.4.2 冲突检查时空 tab 仅 `pass` 放行，但后续 force 分支仍执行，把 label 改成 `(网关副本)` 另建 tab，原空 tab 残留。修复为空 tab 时设置 `target_flow_id=existing.id` 走 update_flow 分支直接覆盖原 tab，force 分支仅对非空用户 tab 生效。

## 1.4.2 (2026-09-03)
- 【缺陷B修复】撤回节点分类：网关节点判定不仅靠 deployed_ids（可能遗漏边界 comment），增加特征判断：AF_START/AF_END 边界 comment 节点、af_scene_ 前缀节点均识别为网关节点。修复了纯网关节点流撤回时 `user_nodes_preserved=1` 的误判。
- 【缺陷C修复】部署冲突检查增加空 tab 判断：撤回后残留的空 tab（只有 tab 节点，无其他节点）视为可覆盖，修复了网关自管流撤回后同 flow_id 重部署被 409 拦截的问题（"NR 中已存在同名 flow 且非本网关部署"）。
- 【缺陷D修复】实现 /lab/* 路由：新增 POST /lab/validate（flow JSON 校验，不落档）、POST /lab/deploy（直接部署到 NR，不需要提案审批）、GET /lab/deploys（部署历史，最多保留50条）。修复了前端 Lab 沙盒部署功能调用 404 的问题。

## 1.4.1 (2026-09-03)
- 【P0 修复】修复 auto_deploy 和回滚 100% 失败：gateway.py 混合模式部署逻辑中两处调用 `self.nr.update_flow`，但 NRLayer 只暴露 `update_flow_nodes`，导致 `'NRLayer' object has no attribute 'update_flow'`。改为 `update_flow_nodes` 后 auto_deploy（raw+DSL）和全量/选择性回滚均恢复正常。
- 【P0 修复】snapshot_manager.py 中两处 `nr_client.update_flow` 同样改为 `update_flow_nodes`。
- 【P2 改进】创建授权码时 `target_tab` 改为可选：留空表示不绑定 tab，走 per_flow 模式（每个 flow 自动创建独立 tab）；绑定后 Agent 只能在指定 tab 部署。前端表单增加说明文字。
- 【P3 改进】频率限制计数器改为成功和失败都累计：之前仅成功后 `rate_window_count+1`，失败风暴可绕过限流；现在失败尝试也计数，防止恶意/异常高频调用。

## 1.4.0 (2026-09-03)
- 【P4 重大功能】部署授权码（Trusted Agent Auto-Deploy）：用户可为受信任 Agent 发放授权码，Agent 持码可在限定范围内自动部署 Flow，无需用户在 WebUI 手动确认。
  - 授权码基础机制：生成/验证/吊销，SHA-256 哈希存储（不存明文），创建时只显示一次。
  - 多重限制：目标 tab 绑定、有效期（默认4小时）、权限（deploy/modify/undeploy）、节点数阈值（超过仍需人工审批）、资源配额（最大节点数/最大flow数）、操作频率限制（次/分钟）。
  - 可回溯机制：授权前全量快照、每次操作前增量快照、全量回滚/选择性回滚、回滚前自动创建新快照（防误回滚）、快照差异对比。
  - 审计日志：每次操作记录时间、Agent、操作类型、Flow、节点数、成功/失败、错误信息，WebUI 可查看。
  - 使用统计面板：每个授权码显示部署/修改/撤回次数、已用节点数、失败次数。
  - 危险操作二次确认：require_confirm_dangerous 配置，前端吊销/回滚均需确认。
  - 并发控制+NR实例绑定：bound_agent/bound_nr_instance 配置，授权码可绑定特定 Agent 和 NR 实例。
  - fail-safe 设计：授权码无效/过期/吊销/超配额时自动回退到正常人工审批流程，不拒绝部署。
- 新增模块：deploy_tokens.py（授权码存储/验证/日志）、snapshot_manager.py（快照/回滚/差异对比）。
- 后端 API：GET/POST /api/deploy-tokens、DELETE /api/deploy-tokens/{id}、GET /api/deploy-tokens/{id}/logs、GET /api/deploy-tokens/{id}/snapshots、POST /api/deploy-tokens/{id}/rollback、GET /api/deploy-tokens/{id}/diff。
- MCP 工具：autoflow_deploy_raw 和 autoflow_propose_dsl 增加 deploy_token 参数，持码自动部署。
- WebUI：新增「授权码」管理页面（创建/列表/吊销/日志/快照/回滚），创建成功弹窗显示授权码（只显示一次）。

## 1.3.3 (2026-09-03)
- P4 混合模式完善：WebUI 部署界面增加「目标 tab」选择器，用户部署提案时可选择：
  - 留空=按当前 Tab 组织模式自动部署
  - AutoFlow 集中 tab（单 tab 模式）
  - 已有 tab（混合模式，flow 部署到该 tab 中）
  - 新建 tab（创建新 tab 并部署）
- 后端 deploy_proposal API 增加 target_tab 参数接收。
- 前端增加 _loadNRTabs 辅助函数加载 Node-RED tab 列表。
- 部署确认对话框从原生 confirm() 改为自定义 modal，支持 tab 选择器交互。

## 1.3.2 (2026-09-03)
- 修复 P0：`/tab-org/status` 和 `/tab-org/migrate` 恒 500 —— 根因是用 `request.app.state.gateway` 而非闭包变量 `gw`，且遗留记录缺 `tab_org_mode` 字段时缺少异常处理。
- 修复 P1：WebUI「保存设置」用 POST `/settings`（405），改为 PUT（后端仅接受 PUT）。
- 修复 P1：前端 `/tab-org/status` 失败时静默吞错，改为显式显示红色错误提示卡片，避免用户误以为「没有可迁移的 flow」。
- 修复 P1：P4 混合模式 `target_tab` 参数在 MCP 工具中未暴露 —— `autoflow_deploy_raw` 增加 `target_tab` 参数，`propose_raw` 增加 `target_tab` 参数并写入提案 content，`deploy_proposal` 从提案读取 `target_tab`。
- 增强：`get_migration_status` 增加 mixed_count 统计，对非 dict 类型的 flow meta 做健壮性处理。
- 增强：迁移 API 失败时返回 traceback 最后 500 字符，便于排障。

## 1.3.1 (2026-09-03)
- P2 模式切换+一键迁移：新增 per_flow ↔ single_tab 双向迁移功能，迁移过程重新分配坐标、更新账本、删除原 tab，WebUI 高级设置页提供迁移按钮和状态统计。
- P3 自动分流预警：单 tab 模式下监控 AutoFlow tab 节点总数，超过阈值（默认200，可通过 AF_SINGLE_TAB_WARN_THRESHOLD 配置）时在 WebUI 显示黄色预警卡片。
- P4 混合模式：deploy_proposal 增加 target_tab 参数，单个 flow 可手动指定目标 tab（按 tab id 或 label 匹配，不存在则自动创建），flow_catalog 记录 tab_org_mode=mixed。
- 撤回逻辑支持 mixed 模式：按 flow 的 tab_org_mode 读取对应 tab，精确删除本 flow 节点。
- 新增 API：GET /api/tab-org/status（迁移状态+预警）、POST /api/tab-org/migrate（执行迁移）。
- WebUI 高级设置页重构：增加分流预警显示、迁移状态统计、一键迁移按钮、混合模式说明。

## 1.3.0 (2026-09-03)
- Tab 组织模式分级方案 P0+P1：新增 tab_organizer.py 模块，支持单 tab 集中模式（所有 flow 合并到固定「AutoFlow」tab，用 AF_START/AF_END comment 节点标记边界，每个 flow 独立坐标区域）。
- 部署逻辑支持单 tab 模式：deploy_proposal 增加 tab_org_mode 分支，单 tab 模式下合并节点到 AutoFlow tab 而非创建新 tab。
- 撤回逻辑支持单 tab 模式：undeploy 按 flow 的 tab_org_mode 读取对应 tab，单 tab 模式下只移除本 flow 节点不删除整个 tab。
- flow_catalog 增加 tab_org_mode / tab_id / boundary_comment_ids / y_offset 字段。
- WebUI 设置页新增「高级设置」tab，可切换 Tab 组织模式（per_flow / single_tab），运行时生效免重启。
- 配置优先级：运行时 feature_flags > 环境变量 AF_TAB_ORG_MODE > 默认 per_flow。

## 1.2.5 (2026-09-03)
- 全局文案修正：「待人类在 WebUI 审批」→「待用户在 WebUI 审批」，共修正 43 处（mcp_server.py 27处为根因——工具描述写"人类在 WebUI 审核"，AI 模仿用词）。
- README 新增核心版章节：完整版 vs 核心版对比表、给 agent 的安装提示词、使用示例。
- 一键安装命令改为安装到当前目录推荐。

## 1.2.4 (2026-09-03)
- 在线更新页新增版本简介（CHANGELOG），每个版本更新内容展示给用户。
- 全面 UI 审查：统一标题（去掉部分 emoji）、统一术语（Agents/Agent管理、raw flow/原生 flow、身份模式/权限模式）、概览快速上手旧文案更新。
- 新增 CHANGELOG 记录 7 个版本。

## 1.2.3 (2026-09-03)
- 在线更新页：去掉副标题、增加更新进度条、更新失败提示、国内镜像选择（self_update.py 增加 mirror 参数）。
- Link API 首次访问引导增加示范链接。
- ACP 对等令牌标题明示跟 memory-agent 对接，去掉"改名前叫 memory-worker"。
- 教程系统重构为 6 教程 25 步，新增「两种使用途径」排第一、黑白箱概念解释、精简步骤。

## 1.2.2 (2026-09-02)
- WebUI 文案 v2：面向 hassbian 极客用户，保留 flow/DSL/子流程/Link API/MCP 等技术术语，仅 AutoFlow 特有概念调整。
- 配色微调 #2F6BFF→#3B6FE8。
- 新增教程系统（8教程37步→后续重构为6教程25步）、首次访问引导（7页面）、帮助系统（7概念详解）。
- 侧边分组、Toast 类型、列表色条等 UX 优化。

## 1.2.1 (2026-09-01)
- 每个 Link API 单独的「安装到 Node-RED」安装按钮 + 卸载按钮（#C）：新增 POST /api/link-apis/{name}/install 单装端点，list_subflows 增加 needs_nr_flow 标志供前端显隐安装按钮。

## 1.2.0 (2026-09-01)
- tab 链接逆生成 Link API（#C-tab）：WebUI 粘贴 NR tab 链接只读自省，注册 link_out 薄桥接，agent DSL 调用即命中用户 tab 入口

## 1.1.0 (2026-08-31)
- 版本管理落地后首个递增版：self_update 语义化「已是最新」判定 + WebUI 显示运行版本号

# AutoFlow 网关 Changelog

> 本文件记录**网关本体**（WebUI + MCP + 编译/校验/部署）的发布版本。
> 注意：`core/` 下的 `VERSION`/`CHANGELOG.md` 是 **AutoFlow Core（专家路径 skill）** 的独立版本轨道，与本文件无关。
> 发布流程见 `scripts/tag_release.py`；`v*` tag 即「方案 C 受控自更新」的可用更新来源。

## 1.0.0 (2026-08-31) — 网关首个发布 tag
- 受控自更新（方案 C）落地：WebUI「更新」页 + `POST /api/admin/self-update`
  + `GET /api/admin/update-check`（owner 专属，RBAC fail-closed 把关）。
- 自更新安全约束：仅接受 v* 版本 tag 或白名单 SHA；备份→fetch→checkout -f→
  py_compile 校验→失败回滚→成功 SIGTERM 重启；绝不 `git clean -f` / `reset --hard`。
- 版本管理：根 `VERSION` 文件随自更新一并 checkout，故「更新」页显示的「当前版本」
  即为实际运行版本；比对待比对保证「已是最新」判定准确。
- 中国网络适配：自更新 remote 默认走 SSH（`git@github.com:lidicn/AutoFlow.git`），
  容器内 `GIT_SSH_COMMAND` 直指私钥、跳过挂载的 `~/.ssh/config`（属主为 lidicn，root 拒访）；
  git 调用统一 `-c safe.directory=*` 绕过 dubious ownership。
