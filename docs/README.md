# AutoFlow 文档索引

> **本目录是 AutoFlow 文档的唯一入口。** 2026-09-02 从 5 处散落位置整理归一。
> 整理前的问题：文档散落在 `E:\NAS\autoflow\docs`、`D:\Documents\HAOS\AutoFlow\docs`、
> `D:\Documents\HAOS\AutoFlow\handoff`、`D:\Documents\HAOS\workspace`、
> `\\100.112.138.64\share`，且同一份 `ARCHITECTURE.md` 有两个冲突版本。

## 分类原则

**仓库内只放「结论」，不放「过程」。** 原始报告（约 80–100 份）已提炼为台账并冷存到仓库外，
不塞进 `docs/archive/` —— 否则 dw 打开目录仍会被淹没，等于没整理。

```
docs/
├── README.md                  ← 你在这里
├── 01_product/                产品：为什么 / 是什么 / 长什么样
├── 02_architecture/           架构：单一真相源
├── 03_dev/                    开发：怎么做
├── 04_test/                   测试：给 wb2
└── 05_handoff/                交接：给接手的人
```

---

## 01_product — 产品

| 文件 | 用途 |
|---|---|
| `WEBUI_UX_PROPOSAL_v2.md` | **WebUI 文案优化 + UX 美化方案（当前权威）**。dw 撰写。面向 hassbian 极客：术语保留，仅对 AutoFlow 特有概念加注。⚠️ v2 **已推翻** v1 的「全面去技术化」路线（对 HA/NR 玩家反而是降级），v1 已冷存 |
| `RELEASE_PLAN_core_v1.md` | AutoFlow Core（专家路径）发布计划与路线图 |

## 02_architecture — 架构

| 文件 | 用途 |
|---|---|
| `ARCHITECTURE.md` | **★ 权威架构文档**。2026-09-02 合并两份冲突旧版并核对源码后定稿：39 个模块速查表、三端点与 agent mode、编译管线、护栏、部署。含「§0.1 合并时剔除的过期事实」防旧知识复活 |

## 03_dev — 开发

| 文件 | 用途 |
|---|---|
| `PLAN_webui_password_login.md` | WebUI 登录改造（令牌 → 账号密码 + 会话）的设计与 D1–D5 决策记录。状态：已实现，待 NAS 干净重部署验收 |

## 04_test — 测试

| 文件 | 用途 |
|---|---|
| `findings-ledger.md` | **★ 缺陷结论台账**。从约 80–100 份报告提炼：已闭环 16 项、未闭环 16 项（B1–B16）。**未闭环清单是那批报告唯一必须保留的知识** |

## 05_handoff — 交接

| 文件 | 用途 |
|---|---|
| `HANDOFF_dw_takeover.md` | **★ dw（豆包work）接手手册**。角色分工、能力边界、项目终点、git 约定、踩坑清单 |
| `TAKEOVER_REPORT.md` | 2026-08-14 历史接手总结（含代码成熟度评估、巨石模块观察）。结论已被 dw 交接单覆盖，留作历史存档 |
| `DOCS_CONSOLIDATION_PLAN.md` | **文档整理的决策记录**（2026-09-02 已执行）。说明 docs/ 为什么是这个结构、冷存区在哪、执行中修正了哪两处错误。要看「文档为什么这么放」就读它 |

---

## 仓库外的东西（不在 git 里，但要知道在哪）

| 位置 | 内容 |
|---|---|
| `D:\Documents\HAOS\AutoFlow_archive\2026-09-02\` | **冷存区**（仓库外）：原始测试报告 57 份 + 历史交接卡 17 项 + 两个测试工作区 + `AutoTest/` 缺陷目录 + `_TO_DELETE/` 隔离区。提炼后的结论已在 `04_test/findings-ledger.md`，原文留档备查 |
| `Z:\`（= `\\100.112.138.64\share`，已 `net use` 映射） | **活协作区**：wb2 的 `AutoFlowTestv2\tests\TEST_TICKET_NNN` 工单持续产出（已到 012）、`autoflow_devteam\handoff`、`reviews`、`TASKS.md`。**内容不复制进仓库** —— 复制即制造第二份立刻过期的副本 |
| `D:\Documents\HAOS\workspace\` | 已清出 AutoFlow 内容，只剩**其他项目**（`Poster-Wall`、`MemoryAgent_Test`、`Smarthome_*`、`Tester`）—— 这些不是 AutoFlow 的，别动 |

### 关于 Z: 盘

- 映射命令：`net use Z: \\100.112.138.64\share /persistent:yes`
- **为什么需要映射**：Bash 走 SMB UNC 路径会挂死 3 分钟以上，映射成盘符后 Bash 可直接访问。
- 未映射时，只有 `Read` 和带 `path` 参数的 `Glob` 能用；`Glob` 跨盘符不传 `path` 会**静默返回空**。

---

## 维护约定

1. **改了代码结构，同步改 `02_architecture/ARCHITECTURE.md`** —— 两份旧架构文档都因没同步而失准
   （mode 取值、模块清单、行数全部过期）。
2. **能自动生成的不要手写**：MCP 工具清单不写进文档，让 agent 调 `autoflow_whoami` 实时取；
   MCP tool schema 由函数签名装饰器自动生成。
3. **测试结论写进台账，不新增报告文件** —— 新增报告前先问：这条结论能不能进
   `04_test/findings-ledger.md` 的一行？
4. `HANDOFF_*` 文件默认被 `.gitignore` 排除（防内网 IP / 令牌泄露）。
   `05_handoff/HANDOFF_dw_takeover.md` 是**唯一显式豁免**（已核无敏感值）。
   改动它前请重新确认无真实 IP / 令牌。
