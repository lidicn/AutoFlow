# AutoFlow 开发交接单（移交 dw / 豆包work 主导）

> 生成时间：2026-09-02
> 撰写人：wb1（WorkBuddy，原主导 agent）
> 背景：项目核心能力已具备并发布至 v1.2.1；用户决定引入新开发者 **dw（豆包work）** 主导后续开发，
> 收尾期聚焦「前端文案去技术化 + UX 美化 + 文档整理 + 稳定」。wb1 改为接 dw 工单做功能开发，wb2 继续负责测试。
> 本文是开发交接的单一入口，取代 `docs/HANDOFF_for_new_session.md`（那是「新对话续开发」用的旧卡，已归档参考）。

---

## 0. 一句话现状

AutoFlow = **Home Assistant 智能家居的 DSL 网关**（不是桌面 RPA / 画布编辑器）。链路：AI 写语义 DSL
→ 编译 → 静态校验 → vhass 虚拟孪生重放自证 → 网页人工批准 → 部署 + 快照。当前最新发布 **v1.2.1**
（含 #C-tab 从 NR tab 链接逆生成 Link API、每个 Link API 单独安装/卸载按钮）。代码单一定源 `E:/NAS/autoflow`，
GitHub `git@github.com:lidicn/AutoFlow.git`，NAS prod 经「更新」页自更新拉取 v* tag。

**产品定位（双路径，已拍板，勿改）**：
- **安全路径（现有网关）**：DSL-first，面向公众/普通用户 agent。写 DSL → 编译 → vhass 重放自证 → WebUI 人工批准 → 部署。
- **专家路径（AutoFlow Core）**：面向 lidicn 本人，agent 直连 NR 写 JSON，护栏内置，无 WebUI 批准闸。发行物真相源 = `core/`。
- 二者**并存非替代**；网关=安全默认，核心版=专家模式。权威来源：`README.md` + `docs/ARCHITECTURE.md`。

---

## 1. 角色与分工（推荐方案）

| 角色 | 身份 | 职责 | 是否直推 GitHub |
|---|---|---|---|
| **dw（豆包work）** | 主导开发者 | 架构决策、前端/UX 美化与文案去技术化、文档整理、发布/merge/review、派发工单 | **是（唯一 write 入口）** |
| **wb1（WorkBuddy / 我）** | 功能开发 agent | 接收 dw 工单，实现具体功能/修复，产出代码交 dw 合入；**不主导方向、不碰前端文案风格**（历史上下文偏技术化，改文案易带回技术腔） | 否（经 dw review/PR） |
| **wb2** | 测试 agent | 独立测试，T00x 工单体系，验证 dw/wb1 产出；回写 `TEST_RESULT_NNN.md` | 否（测试结果交 dw/review） |

**为什么这样分**：用户对前端推广的最大阻力是「文案过于技术化」，而 wb1 的历史上下文已把前端带入技术向。
故前端文案/UX 方向交由 dw 主导，wb1 退居「接单做功能」的执行位，避免技术腔回流。wb2 独立测试保证质量门不失。

### GitHub 身份 / SSH key 方案（开放问题，需用户拍板）

两个开发者并存**不一定**需要两把 key，取决于 wb1 是否直推：

- **推荐（方案 A · 单一 write 入口）**：dw 用自己的 GitHub 身份（新账号 + 独立 SSH key，或作为 `lidicn/AutoFlow` 的 collaborator 持 write），
  **唯一持 push 权限**；wb1/wb2 只产代码/测试结果，经 dw 的 review + merge 落库，提交用 `Co-Authored-By` 标注归属。
  → **只需一把 key（dw 的）**，安全面最小、归属清晰。
- 方案 B（两把 key）：若 wb1 也要直推独立分支，则 dw、wb1 各一 GitHub 身份 + 各一把 key（collaborator write）。
  归属最清晰但多一把 key 要管、且 wb1 的「身份」对 AI agent 而言是伪身份，审计价值有限。

**wb1 建议**：采用方案 A。dw 完全接管推送权，wb1 的交付走 PR/工单，干净收尾、不扩散密钥。

---

## 2. 项目当前已具备能力（dw 接手清单）

- DSL 编译器 + 静态校验 + vhass 虚拟孪生重放自证（F12 分支正确性已闭环）
- WebUI 人工批准闸（安全路径核心不变量：agent 永不自批准，只能下线自己部署的东西）
- **Link API 桥接**：`http_api`（网关内联）/ `link_out`（fire-and-forget 到用户 tab 的 link in 入口）；#C-tab 从 NR tab 链接只读自省注册薄桥接
- 每个 Link API 单独的「安装到 Node-RED」+「卸载」按钮（v1.2.1）
- 受控自更新：WebUI「更新」页拉取 v* tag 部署（SSH remote，镜像 git+openssh 已固化进镜像）
- 测试体系：wb2 的 T00x 工单 + pytest 回归（link_api/subflow/verify 等）

**测试纪律现状**：`tests/test_install_single_link_api.py` 等 25 项 link_api 相关全绿；`test_subflow_webui` 有 2 条**预存失败**
（种子数漂移断言 count=9，实际 7，豆包系列转 `self_use` 被列表排除）——**非回归，dw 接管后顺手修断言即可**。

---

## 3. 收尾期目标（定义项目终点）

核心能力已齐，建议把 v1.2.x 定为**「可推广收尾版」**，不再堆大功能，只做三件收尾：

| 优先级 | 事项 | 交付物 | 负责 |
|---|---|---|---|
| **P0** | 前端文案去技术化 | 全站面向非技术用户的文案（按钮/提示/空态/错误），去掉 `link out`/`subflow`/`entry_link_id` 等术语，改白话 | dw |
| P0 | UX 美化 | 布局/配色/响应式/加载态一致性（当前偏工程师审美） | dw |
| P1 | 文档归一 | 见 §5，docs/ 单一真相源 + 非技术用户手册 | dw + wb1 协助 |
| P1 | 测试闭环 | wb2 把剩余 T 工单闭环；修 `test_subflow_webui` 2 条预存断言漂移 | wb2 + dw |
| P2 | 稳定维护态 | 锁定后只修 bug，不加新大功能；发布节奏＝修一版打一个 v* tag | dw |

**项目终点建议**：以「非技术用户能看懂 WebUI 并完成一次 DSL→部署」为验收标准，达成即进入维护态。
不推荐继续扩展新功能域（如新协议接入、画布编辑器）——那会重新引入技术复杂度，与「去技术化推广」目标相悖。

---

## 4. 开发约定（dw 接手后沿用）

- **git 纪律**（血泪教训，必守）：所有 git 写操作本机真实终端跑（`dangerouslyDisableSandbox` 等价物）；
  显式 `git add` 仅源+测试，绝不夹带 `gen_r21_flows.py`/`outputs/`；`core.gc.auto=0`；提交前 `git fsck --full`；
  push 后 `git ls-remote` 权威校验 master==main==本地 HEAD。
- **发布到 WebUI 升级**：必须打 **v* tag**（`scripts/tag_release.py patch`），裸 commit 不会被「更新」页识别。
- **部署**：本地 `E:/NAS/autoflow` → commit/push → NAS 经 `autoflow-nas-deploy` 技能（scp 差异文件 + restart + 运行时内省验收），或 NAS「更新」页自更新。
- **工单体系**：dw 派工单 → wb1 实现 → wb2 测 → 回 `TEST_RESULT` → dw review/merge。
- **双路径红线**：网关只处理自己部署的 flow，用户手工 NR flow 一律只读，绝不误删（README 明写「想误伤都做不到」）。
- **数据红线**：`data/.webui_token`、`data/llm_config.json`、`.env`、Bark key、NAS IP 绝不进 git。

---

## 5. 散落文档整理计划（需整理，建议做）

当前文档散落：

| 位置 | 内容 | 处置 |
|---|---|---|
| `docs/` | ARCHITECTURE / README 指向、CONVERGE_E_NAS、HANDOFF_for_new_session、PLAN_webui_password_login、RELEASE_PLAN_core_v1、TAKEOVER_REPORT | 权威文档保留；旧 session handoff 归档到 `docs/archive/` |
| 根 `*.md` | README（用户向）、ARCHITECTURE（开发者向，权威）、DEPLOY、CHANGELOG、WHITEBOX_VERIFY_LOOP | README/ARCHITECTURE/CHANGELOG/DEPLOY 保留为顶层入口 |
| `core/` | INSTALL/VERSION/CHANGELOG/skill | 专家路径发行物真相源，保留 |
| `src_backup_*`、`skills_backup_*` | 历史备份 | 确认无需后归档/删除 |
| 旧 `handoff/`（`D:/Documents/HAOS/AutoFlow/handoff/`，见旧卡 §5） | 历史工单卡 | 合并要点进本文 §2/§4 后归档 |
| `.workbuddy/memory/` | 会话记忆 | 运维参考，不进产品文档 |

**目标结构（docs/ 单一真相源）**：
```
docs/
  README_产品.md           ← 非技术用户视角（可由 README.md 提炼去技术化版）
  ARCHITECTURE.md         ← 开发者向（已权威，保留）
  DEV_GUIDE.md            ← 开发/部署/git 纪律（本文 §4 展开）
  TESTER_GUIDE.md         ← wb2 测试者手册（工单体系、回写格式）
  RELEASE.md              ← 发布流程（tag_release + 自更新）
  USER_MANUAL.md          ← 面向最终用户的「怎么用」白话手册（P0 配套产出）
  archive/                ← 旧 handoff / 历史卡
```

---

## 6. 开放问题（待用户拍板）

1. **GitHub 身份**：dw 用新 GitHub 账号还是 `lidicn` 下 collaborator？→ 推荐 dw 独立账号 + 独立 SSH key（方案 A）。
2. **wb1 是否保留直推权**：推荐否（走 dw review）；如未来需要再给 deploy key。
3. **文档整理是否现在做**：建议 dw 接手即做（§5），避免散落文档继续增殖。
4. **项目终点验收标准**：以「非技术用户自助完成一次 DSL→部署」为准，达成即维护态——是否认同？

---

## 7. 关键坑（dw 别踩，wb1 已踩过）

- 沙箱 git 写不落盘 → 所有 commit/merge/push 在本机真实终端跑。
- NAS 与本地都是 CRLF，`git show` 是 LF → 跨格式 md5 必不等，辨伪用 `diff --strip-trailing-cr`。
- webui.py 必须保留 llm_client **惰性导入**（缺 httpx 不崩网关）。
- 仓库对象库曾 corrupt（184 missing），恢复用 **`git fetch origin` 就地补齐** 最稳，别折腾 clone+拷贝（沙箱临时目录跨命令不保活）。
- 部署后验收用「运行时内省 + 真实 HTTP 端点」，不止 grep。
