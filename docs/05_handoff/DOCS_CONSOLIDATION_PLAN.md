# AutoFlow 文档整合方案（盘点 + 分类 + 处置）

> 生成时间：2026-09-02　撰写：wb1
> 状态：**✅ 已执行（2026-09-02 同日）**。本文保留为「文档结构是怎么来的」的决策记录。
> 执行结果见 [../README.md](../README.md)（当前结构）与 [../04_test/findings-ledger.md](../04_test/findings-ledger.md)（提炼产物）。

---

## 6.1 执行结果（2026-09-02，用户已确认全部推荐项）

| 决策 | 结果 |
|---|---|
| ① 输出目录 | ✅ `E:\NAS\autoflow\docs`，建 `01_product/02_architecture/03_dev/04_test/05_handoff`，写 `README.md` 唯一索引 |
| ② ARCHITECTURE.md | ✅ 以 E 版为骨架、合并 D 版结构章节，**并按实际源码重写**（39 模块 / mode 已改 `normal·expert·developer` / 剔除 Windows nssm 服务描述）。过期事实单列 §0.1 |
| ③ 测试报告 | ✅ 提炼 → 冷存 → 清理三步完成。台账 `04_test/findings-ledger.md`（已闭环 16 + 未闭环 19） |
| ④ share 映射 | ✅ `net use Z: \\100.112.138.64\share /persistent:yes` 成功；内容不复制进仓库 |
| ⑤ 旧树清理 | ✅ `_TRASH_*`（42M）隔离到 `AutoFlow_archive/2026-09-02/_TO_DELETE/`（**可恢复，未真删**） |

### 执行中修正的两处原方案错误

1. **原判定「`AutoFlow_Test` 是 `AutoFlowTestv2` 的副本，可删」是错的** —— 实测两者**双向各有独占内容**
   （A 独占 `WB26_in/`、`WB30_31_in/`、`BENCHMARK_REPORT.md`、`FINAL_*` 等 9+ 份）。
   → 改为**两个都冷存保留**，不做删除。
2. **node_modules 未清理** —— 原方案列为「374 份噪音 md」，但它是 `.workbuddy/nr_local` 的**依赖不是文档**，
   删除会让 node-red 本地工具失效，且体量过大（`du` 超时）。文档散落的痛点已解决，不冒破坏工具的风险。

### 执行中新发现的盲区

- 冷存阶段才发现 `D:\Documents\HAOS\workspace\AutoTest\` 是**独立缺陷目录**（A1–A31 报告 + round5 工单），
  子代理扫描未覆盖。抽查 round5 的 3 项 → **A23 未闭环**（缺守卫测试），已记为台账 B18，目录级盲区记为 B19。

---

## 1. 盘点结论

| 位置 | 性质 | 有效 md | 噪音 md | 判定 |
|---|---|---|---|---|
| `E:\NAS\autoflow\docs` | **git 仓内**，部署真相源 | 7 | 0 | ✅ **输出目标（推荐）** |
| `D:\Documents\HAOS\AutoFlow\docs` | 旧树文档（设计/架构 + 20+ WB审计报告 + devlog + prompt） | 64 | — | 提炼后并入，原始冷存 |
| `D:\Documents\HAOS\AutoFlow\handoff` | 2026-08 历史工单/交接卡 17 份 | 17 | — | 归档（结论已并入 dw 交接单） |
| `D:\Documents\HAOS\AutoFlow`（根） | **非 git 仓**；含 `_TRASH2_2026-08-05` 整棵重复树、`.workbuddy/nr_local/node_modules`、另一项目 `smarthome-assistant` | 187 | **374** | ❌ 排除（90%+ 是噪音/重复） |
| `D:\Documents\HAOS\workspace` | **多项目混杂**：`Poster-Wall`（另一项目）、`AutoTest`、`AutoFlow_Test`、`AutoFlowTestv2`（≈前者的副本）、`AutoFlowTestv3`、`Tester` | 162 | 34 | 提炼后并入，原始冷存 |
| `\\100.112.138.64\share` | 团队协作区：`AutoFlowTestv2\tests\TEST_TICKET_NNN`、`autoflow_devteam\handoff`、`reviews`、`TASKS.md` | 可读（精确 Read 成功；顶层枚举超时） | — | 见 §5，不复制入库 |

**噪音构成**：`node_modules` 下第三方包的 README/CHANGELOG（408 份）、`_TRASH2_2026-08-05`（整棵重复树）、
`.pytest_cache`、`AutoFlow_Test` 与 `AutoFlowTestv2` 的重复副本。**这些不是文档，排除在整理范围外。**

---

## 2. 输出目录决策：推荐 `E:\NAS\autoflow\docs`

| | `E:\NAS\autoflow\docs` | `D:\Documents\HAOS\AutoFlow\docs` |
|---|---|---|
| git 版本管理 | ✅ 在仓内，随版本走 | ❌ **该树无 `.git`** |
| 与部署真相源一致 | ✅ 是 NAS prod 同步源 | ❌ 否 |
| dw 工作位置 | ✅ dw 接手在此开发 | ❌ 旧树，易与 E 盘产生第二真相源 |
| 噪音/重复 | 干净（7 份） | 混着 `_TRASH2` 重复树 + 374 node_modules |

**结论：输出到 `E:\NAS\autoflow\docs`**，`D:\Documents\HAOS\AutoFlow` 整体降级为**历史冷存**（不再作为文档主入口）。

> ⚠️ **必须先解决的冲突**：两侧都有 `ARCHITECTURE.md`——D 版 18,392 字节（Jul 25）、E 版 8,632 字节（Aug 3，更新）。
> 建议以 **E 版为权威**，合并 D 版中 E 版缺失的章节，避免 dw 读到过期架构。

---

## 3. 推荐的 docs/ 下级分类

```
E:\NAS\autoflow\docs\
├── README.md                      # 文档索引（唯一入口，列出下面各文件及用途）
├── 01_product/                    # 产品（面向"为什么/是什么"）
│   ├── product-positioning.md     ← 由 VALUES.md / LANDSCAPE.md / ROADMAP.md 合并去技术化
│   └── user-manual.md             ← 【P0 待写】非技术用户白话手册
├── 02_architecture/               # 架构（单一真相源）
│   ├── ARCHITECTURE.md            ← 权威（合并 D 版后定稿）
│   ├── DECISIONS.md
│   ├── dsl-design.md              ← dsl_design.md
│   ├── nr-primitives.md           ← NR_PRIMITIVES.md
│   └── subflows.md                ← SUBFLOWS.md / white_produce_queue.md / subflow-store-design.md
├── 03_dev/                        # 开发（怎么做）
│   ├── DEVELOPER_GUIDE.md
│   ├── conventions.md             ← git 纪律 / 工单体系 / 双路径红线
│   ├── release.md                 ← 发布流程（tag_release + 自更新）
│   └── deploy.md                  ← 部署（autoflow-nas-deploy / NAS 更新页）
├── 04_test/                       # 测试（给 wb2）
│   ├── TESTER_GUIDE.md            ← 测试者手册（T00x 工单、回写格式）
│   └── findings-ledger.md         ← 【核心】从百份报告提炼的「缺陷结论台账」
├── 05_handoff/
│   └── HANDOFF_dw_takeover.md     ← 已写
└── （原始报告不入库，见 §4）
```

**原则：仓库内只放「结论」，不放「过程」。** 原始报告若塞进 `docs/archive/` 会让 git 仓库膨胀，
且 dw 打开目录仍会被百份报告淹没——等于没整理。

---

## 4. 测试报告：有价值吗？要保留吗？

**判断：绝大多数「报告本身」不值得保留，但必须先「提炼」再丢弃——不能直接删。**

理由：
- 报告的**结论**若已闭环（修好 + 有测试守卫，如 WB93 F12/O1 已有 `test_wb93_*` 守卫），
  价值已转移到代码与测试里，报告只是**某一时刻的快照**，留着是纯噪音。
- 报告的**过程**（round-by-round bug hunt、逐轮验证）更是典型过程产物，已被后续轮次覆盖。
- **例外**：未闭环的发现若直接丢弃 = 知识丢失。所以必须先提炼成台账。

**数量**（约 80–100 份）：`dsl_bug_hunt_round2~26`(25)、`round13/20~25_verify_report`(7)、
`TEST_TICKET_001-005`+`TEST_RESULT_001-005`(10，**且 AutoFlow_Test/AutoFlowTestv2 两套重复**)、
`BUG_REPORT*`(8)、`autoflow_WB2~WB25 审计报告`(20+)、压测/覆盖率/entityId/trigger 回归等专题报告(~10)、
`*_TEST_REPORT/FINAL_REPORT/BENCHMARK/VULN_REPORT`(~10)、`devlog_2026-07-29_*`(5)。

### 三步处置（推荐）
1. **提炼**：产出 `04_test/findings-ledger.md` —— 每份报告压成一行：
   `发现 → 是否已修 → 对应 commit/测试 → 是否仍开放`。
   未闭环的单独列「遗留未闭环」清单（这是唯一必须保留的知识）。
2. **冷存**：原始报告整体移到**仓库外** `D:\Documents\HAOS\AutoFlow_archive\<日期>\`（不进 git）。
3. **清理**：确认台账无误后，删除纯过程物与重复副本
   （`AutoFlow_Test` 副本、`dsl_bug_hunt_round*`、`_TRASH2_2026-08-05`、node_modules 噪音）。

> 🔴 **删除前必做**：所有删除项会先列出完整清单交你确认，并**先备份再删**（不用 `rm -rf`，走回收站/先移冷存）。

---

## 5. share 能映射进来吗？

**可以映射，但不建议把内容复制进 docs/。**

- **映射（推荐，解决工具限制）**：`net use Z: \\100.112.138.64\share /persistent:yes`
  → 映射成盘符后，Bash 也能访问（当前 Bash 走 SMB 会挂死 3 分钟+，只有 Read/Glob 能用）。
- **不复制**：share 是**活的协作区**（wb2 的 `TEST_TICKET_NNN` 工单在此持续产出）。
  复制进 docs/ = 制造第二份立刻过期的副本，正是文档散落的病因。
- **正确做法**：映射盘符方便访问 + **单向抽取结论**并进 `04_test/findings-ledger.md`，share 原位保留继续协作。

---

## 6. 待你确认（确认后才执行）

1. 输出目录 = `E:\NAS\autoflow\docs`？（推荐）
2. `ARCHITECTURE.md` 以 E 版为权威、合并 D 版缺失章节？（推荐）
3. 测试报告走「提炼 → 冷存 → 清理」三步？（推荐）　还是「只归档不删」/「全部保留」？
4. 是否执行 `net use` 映射 share 为盘符？
5. `D:\Documents\HAOS\AutoFlow` 旧树（含 `_TRASH2`、node_modules）是否允许清理？（会先列清单+备份）
