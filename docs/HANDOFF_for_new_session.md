# AutoFlow 交接文档（供新对话继续开发）

> 生成时间：2026-08-14。本文档是「新开对话继续开发」的单一入口。

## 0. 一句话现状
AutoFlow 网关代码已收敛到 `E:/NAS/autoflow`（本地盘，非 SMB），NAS prod 已部署 LLM UI 修复
（气泡对话/账号池/测试按钮/裸500根因修复）。GitHub 待推送。LLM 助手 agent 身份注入 + 聊天
输入框 UI 改造 + pytest 5 红 待 DEV 完成。deepseek2api 对接文档已出，待对方开发者接。

## 1. 路径与拓扑（重要，别再走偏）
- **本地唯一定源**：`E:/NAS/autoflow`（原 `E:/autoflow` 已损坏归档为 `E:/autoflow.bak-20260814`）
- **NAS prod 活树**：`192.168.2.200:/vol1/1000/docker/autoflow/src/autoflow_gateway`（干净部署态，无 .git）
- **GitHub**：`git@github.com:lidicn/AutoFlow.git`（main；可能落后 NAS 活树，llm-ui 未推）
- **recovery 仓**（历史）：`E:/af_recov_llmwebui`（master @ fa23ea9，4 文件 UI 修复未提交；参考用）
- **安全网快照**：`E:/NAS/autoflow_snapshot_20260814`（含 data/ secrets）
- **绝不**：把 `\\192.168.2.200\docker\...` SMB 直挂成本地盘符实时编辑/跑 git（8-14 因此冻 NAS）

## 2. 部署方式（黄金法则，不变）
本地写 `E:/NAS/autoflow` → `git commit/push`（本机终端）→ NAS 同步用 `autoflow-nas-deploy` 技能
（scp 4 文件 + restart + 运行时内省验收，非 grep）。**不在沙箱里跑 git 写**。

## 3. 当前已部署/已实现
- ACP 开关（black/white/admin 三端点），`/api/acp/enabled` 在线
- LLM 内置客户端：OpenAI function-calling 形状，账号池 fallback，裸500→LLMError 修复
- LLM 设置页：账号池卡片化、`/api/llm/test` 测试按钮（guarded JWT）
- LLM 助手页：气泡对话 + localStorage(50条) 持久化
- api_key 清空 bug 已修（`_preserve`→`_resolve_key`）
- Help 页（NAS 独有热修，在 index.html，未进 git）

## 4. 待办（DEV / 新对话）
| 优先级 | 事项 | 交付物 |
|---|---|---|
| P0 | LLM 助手 agent 身份注入（修「未识别 agent」使 DSL 提案可提交） | `handoff/WORKORDER_DEV_llm_agent_identity_and_chat_input.md` §1 |
| P1 | 聊天输入框改底部居中固定条、无滚动条（仿 MiMo 截图） | 同上 §2 |
| P1 | pytest 5 红修复（纯测试文件，非生产回归） | `handoff/HANDOFF_DEV_llm_ui_pool_test_apply.md` §2 |
| P1 | deepseek2api ↔ AutoFlow 内置 LLM 对接（tools 格式） | `handoff/INTEGRATION_deepseek2api_autoflow_llm.md`（转交对方开发者） |
| P2 | `E:/autoflow` 主树 .git 恢复（已由收敛替代——见 §1） | 已收敛，无需 |
| P2 | _mask_secret 把后端名也遮成乱码（cosmetic） | 建议名字走 esc 不 mask |

## 5. 关联 handoff 文档索引（均在 `D:/Documents/HAOS/AutoFlow/handoff/` 或本 docs/）
- `handoff/WORKORDER_DEV_llm_ui_pool_test.md` — 原 LLM UI 工单（气泡/账号池/测试按钮）
- `handoff/WORKORDER_DEV_llm_agent_identity_and_chat_input.md` — agent 身份 + 输入框 UI 工单
- `handoff/HANDOFF_DEV_llm_ui_pool_test_apply.md` — DEV 合并应用卡（4 文件 cp + 5 测试改法 + --no-ff 合并）
- `handoff/INTEGRATION_deepseek2api_autoflow_llm.md` — deepseek2api 对接文档
- `handoff/DEPLOY_llm_ui_nas_20260814.md` — 8-14 NAS 部署验收记录
- `handoff/DECISION_autoflow_local_path.md` — 本地路径决策卡
- `E:/autoflow/.workbuddy/REVIEW_llm_ui_pool_test.md` — REV 报告（verdict=LOOKS_GOOD_WITH_TEST_FIX_REQUIRED）
- `E:/autoflow/.workbuddy/pr_review_state.json` — 第 18 条状态

## 6. 关键坑（别踩）
- 沙箱 git 写不落盘 → 所有 commit/merge/push 在本机真实终端跑
- NAS 与本地都是 CRLF，`git show` 是 LF → 跨格式 md5 必不等，辨伪用 `diff --strip-trailing-cr` 或同格式比
- webui.py 必须保留 llm_client **惰性导入**（缺 httpx 不崩网关）
- app.js 三方合并时冲突块两侧各以未闭合函数结尾会少 `}` → 需补闭合符
- 部署后验收用「运行时内省 + 真实 HTTP 端点」，不止 grep

## 7. 用户数据红线
`data/.webui_token`、`data/llm_config.json`、`.env`(非 example)、Bark key、NAS IP 绝不进 git。
交付卡片/PR 正文禁写真实 IP/UNC，一律放 artifacts/ + .gitignore 排除。
