# AutoFlow 文档索引

> **本目录是 AutoFlow 文档的唯一入口。**
> 2026-09-02 从 5 处散落位置整理归一；2026-09-08（v2.0.11-beta）重新接手后再次刷新。

## 当前项目状态

- **版本**：v2.0.11-beta（竞技场 beta）｜分支：仅 `main`
- **三条产品线**：Core（nr_client 直连）／Standard（MCP + 网关）／Pro（nr_client `--gateway`）
- **活跃方向**：Arena 竞技场（后端 + 前端已落地，待真实部署验证与教程）
- **架构真相源**：[`02_architecture/ARCHITECTURE.md`](02_architecture/ARCHITECTURE.md)
  （⚠️ `00_overview/` 与 `03_dev/ROADMAP.md` 是**已作废的重复文档**，别读）

## 分类原则

**仓库内只放「结论」，不放「过程」。** 原始报告已提炼进台账并冷存到仓库外，不塞进 `docs/`。

```
docs/
├── README.md                  ← 你在这里
├── 01_product/                产品：为什么 / 是什么
├── 02_architecture/           架构：★ 单一真相源
├── 03_dev/                    开发：怎么做
├── 04_ideas/                  想法草稿（未定型）
├── 04_test/                   测试结论台账
├── 05_handoff/                交接
├── 协作/                      ⛔ 已作废（FFL 团队已解散）
└── 工单/                      任务工单
```

## ⚠️ 占位符对照（2026-09-08 起）

发布门禁 `tests/test_no_secrets.py` 禁止版本库内出现真实内网地址，文档已统一脱敏。
**看到占位符请按下表代入你自己的环境：**

| 占位符 | 含义 |
|---|---|
| `<NAS_IP>` | 运行 AutoFlow 网关 / Node-RED 的 NAS 内网 IP |
| `<SHARE_HOST>` | 团队 SMB 协作共享的主机（IP 或主机名），映射后为 `Z:` |
| `<NR_HOST>` | Node-RED 实例地址（测试与 UI 示例用） |

> 门禁原理与豁免规则见 `tests/test_no_secrets.py` 顶部注释。
> 在**同一行**使用 `<XXX>` 占位符、`your_xxx`、`example`、`os.getenv(...)` 等写法即视为安全。

---

## 01_product — 产品

| 文件 | 用途 |
|---|---|
| `PRODUCT_AutoFlow_Pro.md` | **Pro 版产品定义**：定位、API Key、模板库、经验库、与 Core/Standard 的差异 |
| `WEBUI_UX_PROPOSAL_v2.md` | **WebUI 文案 + UX 方案（当前权威）**。面向 hassbian 极客：术语保留，仅对 AutoFlow 特有概念加注。⚠️ v2 已推翻 v1 的「全面去技术化」路线 |
| `RELEASE_PLAN_core_v1.md` | AutoFlow Core（专家路径）发布计划与路线图 |

## 02_architecture — 架构

| 文件 | 用途 |
|---|---|
| `ARCHITECTURE.md` | **★ 唯一权威架构文档**。50 个模块速查表、三条产品线、三端点与 agent mode、编译管线、护栏、部署。§18 为「项目阶段与当前重点」 |

## 03_dev — 开发

| 文件 | 用途 |
|---|---|
| `autoflow_core_usage_feedback.md` | **Core 版实测反馈**（2026-09-02，TV Cam 保活 flow 实战）。含 NR 的 OAuth2 Password Grant 取 token 细节，接手 Core 版前建议先读 |
| `PLAN_v1.5_pro_and_experience.md` | Pro 版 + 经验库的设计与实现计划 |
| `PLAN_webui_password_login.md` | WebUI 登录改造（令牌 → 账号密码 + 会话）的设计与 D1–D5 决策记录 |
| `ROADMAP.md` | ⛔ **已作废**（停在 v1.5.6），路线图见 `05_handoff/PROJECT_HANDOFF_20260907.md` |

## 04_ideas — 想法草稿

| 文件 | 用途 |
|---|---|
| `ARENA_idea.md` | **竞技场原始设计意图**。✅ 已实现（v2.0），本文说明「为什么要做」 |
| `ARENA_*.md`（其余 5 份） | dw 时期的讨论记录与问卷（blank/dw/filled），过程草稿，结论已进 `ARCHITECTURE.md` §18.2 |

## 04_test — 测试

| 文件 | 用途 |
|---|---|
| `findings-ledger.md` | **★ 缺陷结论台账**。已闭环 16 项、未闭环 B1–B19。**新测试结论写进台账，不新增报告文件** |
| `TEST_v1.3.1_tab_org_p2p3p4.md` | 历史测试报告（已进 git）。后续同类报告一律冷存，不再进仓库 |

## 05_handoff — 交接

| 文件 | 用途 |
|---|---|
| `PROJECT_HANDOFF_20260907.md` | **★ 最新交接文档（v2.0.11-beta，dw → 原主导 agent）**。442 行，含完整模块清单、TODO、踩坑。接手先读这份 |
| `HANDOFF_dw_takeover.md` | 2026-09-02 的上一版交接单（角色分工、项目终点、git 约定）。已被上面那份覆盖 |
| `DOCS_CONSOLIDATION_PLAN.md` | 2026-09-02 文档整理的决策记录（为什么是这个结构、冷存区在哪） |
| `TAKEOVER_REPORT.md` | 2026-08-14 历史接手总结，留作存档 |

## 工单

| 文件 | 状态 |
|---|---|
| `TICKET-20260906-001-整理分支并迁移到autoflow-dev.md` | 已完成（FFL 已解散，AutoFlow-dev 废弃） |
| `TICKET-20260906-002-性能与前端安全修复回传主仓库.md` | **待认领**。3 项实质修复：① webui.py 20+ 处同步 `gw.*` 阻塞事件循环 → `asyncio.to_thread`；② M7 `_bootstrap_webui_token` 明文打印令牌（**2026-09-08 已修**）；③ gateway 实体解析跳过 `__truncated__` |

---

## 仓库外的东西（不在 git 里，但要知道在哪）

| 位置 | 内容 |
|---|---|
| `D:\Documents\HAOS\AutoFlow_archive\` | **冷存区（仓库外）**：`2026-09-02\`（原始测试报告 57 份 + 历史交接卡 17 项 + 两个测试工作区 + `_TO_DELETE/`）、`2026-09-08\process_reports\`（4 份 v1.3.3–v1.4.4 过程报告）。结论已在 `04_test/findings-ledger.md`，原文留档备查 |
| `Z:`（= `\\<SHARE_HOST>\share`，已 `net use` 映射） | **活协作区**：wb2 的 `AutoFlowTestv2\tests\TEST_TICKET_NNN` 工单（已到 012）、`autoflow_devteam\handoff`、`reviews`、`TASKS.md`。**内容不复制进仓库** —— 复制即制造第二份立刻过期的副本 |
| `D:\Documents\HAOS\workspace\` | 只剩**其他项目**（`Poster-Wall`、`MemoryAgent_Test`、`Smarthome_*`、`Tester`）—— 不是 AutoFlow 的，别动 |

### 关于 Z: 盘

- 映射命令：`net use Z: \\<SHARE_HOST>\share /persistent:yes`
- **为什么需要映射**：Bash 走 SMB UNC 路径会挂死 3 分钟以上，映射成盘符后 Bash 可直接访问。
- 未映射时，只有 `Read` 和带 `path` 参数的 `Glob` 能用；`Glob` 跨盘符不传 `path` 会**静默返回空**。

---

## 维护约定

1. **改了代码结构，同步改 `02_architecture/ARCHITECTURE.md`** —— 历史上已有 3 份架构文档因没同步而失准
   （mode 取值、模块清单、行数全部过期）。**不要新建第二份架构文档。**
2. **能自动生成的不要手写**：MCP 工具清单不写进文档，让 agent 调 `autoflow_whoami` 实时取；
   MCP tool schema 由函数签名装饰器自动生成。
3. **测试结论写进台账，不新增报告文件** —— 新增报告前先问：这条结论能不能进
   `04_test/findings-ledger.md` 的一行？
4. **文档里不写真内网 IP**，用 `<NAS_IP>` / `<SHARE_HOST>` / `<NR_HOST>` 占位符（见文首对照表），
   否则发布门禁 `tests/test_no_secrets.py` 会红。
5. `HANDOFF_*` 文件默认被 `.gitignore` 排除（防内网 IP / 令牌泄露）。
   `05_handoff/HANDOFF_dw_takeover.md` 是**唯一显式豁免**（已核无敏感值）。
   改动它前请重新确认无真实 IP / 令牌。
