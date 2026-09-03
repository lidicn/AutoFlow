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
