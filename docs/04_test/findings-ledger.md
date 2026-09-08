# AutoFlow 缺陷结论台账（findings ledger）

> 提炼时间：2026-09-02　提炼人：wb1（子代理扫描 + 人工复核）
> 来源：约 80–100 份测试/审计报告，散落在 `D:\Documents\HAOS\AutoFlow\docs`、`handoff`、
> `D:\Documents\HAOS\workspace\{AutoFlow_Test, AutoFlowTestv2}`、以及团队 share
> （`\\<SHARE_HOST>\share\AutoFlowTestv2\tests`，已映射为 `Z:`）。
>
> **本文是那批报告的替代品。** 原始报告已冷存至
> `D:\Documents\HAOS\AutoFlow_archive\2026-09-02\`（不进 git），见 §C。

## 为什么要这份台账

那批报告绝大多数是**某一时刻的过程快照**：已闭环的发现，其价值早已转移到代码和测试里
（有守卫测试守着，不会再退化），报告留在手边纯属噪音。但**未闭环的发现若直接丢弃就是知识丢失**。
所以先提炼、后丢弃。

---

## ★ 阅读优先级

1. **§B 未闭环清单** —— 这是唯一必须保留的知识，也是后续开发的输入。
2. **§B 中 B1 是架构级根因**：没有独立 staging NR 实例，导致 B2/B3 连续 4 票 BLOCKED 无法验证。
   **在 B1 解决前，不要再声称某个写类 e2e 验证「已闭环」。**
3. §A 已闭环 —— 用来回答「这个 bug 是不是早修过了」，避免重报。

---

## A. 已闭环发现

| # | 发现 | 一句话说明 | 来源 | 修复证据 |
|---|---|---|---|---|
| 1 | **F12** e2e 判据 | 只判可达性不判分支正确性，反置流（暗则关灯）仍判通过 | T006 S3 / T007§2 / T008§2 | 守卫 `test_wb93_f12_branch_assert.py`、`test_wb93_f12_e2e_allow_prod.py` |
| 2 | **O1** 取值-label 求值盲区 | 根因是编译布线缺陷（`_emit_read_state` 返回链尾 → 取值节点成孤儿 → 分支恒走 else，静默反向执行） | T006 S2 / T007§1 / T008§1 | 守卫 `test_wb93_o1_read_value_wiring.py`、`test_gate_jsonata_eval.py` |
| 3 | **O2** 未知实体 fail-open | 黑箱 propose 对未知实体 `ok=True` 还产流，爆炸半径 57/658 = 8.66% | T006 S1 | 已收口为 fail-closed，守卫 `test_wb92_o2_unknown_entity.py` |
| 4 | **c4_replay_semantics** | 重放归零 ≠ 验证通过；G2 默认 fail_closed | T001 / T010§4 | 已落代码 `gateway.py:_replay_zero_policy`；守卫 `test_wb93_c4_replay_policy.py`、`test_c4_replay.py` |
| 5 | W1/W3/B3 | `wires:[]` fail-open；`_vg_eval_switch` 闸门 false-green；retry budget | T001 / T002 | 守卫 `test_w1/w2/w3` 系列在仓（**SHA 未复核，见 §D**） |
| 6 | V-F1~V-F5 | 保守命中假绿 / checkall 语义 / function 黑箱诚实性 / change→switch 字段链 | T002 / T003 | 守卫 `test_false_green_family.py`（SHA 未复核） |
| 7 | V-NEW-1 / V-NEW-2 | 声明效果却 0 重放伪造绿；V-F4 降级在 verify 入口为死代码 | T003 / T004§2 | T004 prod 实测修复生效（SHA 未复核） |
| 8 | V-NEW-3 | `_compute_reliable_fields` 固定点不跨 subflow/link 边界 | T004§1 / T005 | 守卫 `test_subflow_*`（16 个文件在仓，SHA 未复核） |
| 9 | P3-F1 / P3-F2 | 未激活分支的后置条件被误判失败 | T006 S6a | 活网关实证 `[跳过]` 标记（SHA 未复核） |
| 10 | **VULN V1–V4/V6** | 报告称 verify/deploy_raw/validate_flow「实体不校验 + fail-open」 | VULN_REPORT | ⚠️ **判为假阳性**：同网关实跑幽灵实体得 `block/passed=False`，T001§8 已撤回 5 条 xfail。**不要再当漏洞修** |
| 11 | 工具 docstring 漂移 | `autoflow_get_entity_state` 返回嵌套在 `state` 下，docstring 却写顶层 | T007§3.1 | T008§3 docstring 校正改判 EXPECTED；守卫 `test_s3_tools_list_docs_have_new_keys` |
| 12 | **restore_snapshot 写崩实例** | 逐条遍历扁平 flows → 全用户 tab 节点归零（P0） | T011 Findings#1 | T012§2 CLOSED；`tests/test_core_v1.py` 已入仓 |
| 13 | 红线无代码硬拦 | `write_flow` 可写非 `af_*` 用户 tab | T011 Findings#5 | T012§3 CLOSED：`_guard_ownership` 以**线上 label 为准**，伪造 `af_` 前缀无效；`test_nr_client_guard(s).py` |
| 14 | R10 lint 末端误报 | `debug`/`link out` 被当 1 输出，合法 flow 被拒 | T011 Findings#2 | T012§4 CLOSED：`_SINGLE_OUTPUT_TYPES` 移除二者 |
| 15 | MCP schema 漂移 A20/A27 | 手工 schema 与实现漂移 | T006 S5 | **结构性缓解**：62 处 `@mcp.tool()` 由函数签名自动生成，0 处手工 `inputSchema` |
| 16 | delegate 后端接线 | T006 标「后端接线待确认」 | T006 S5c | 已接线：`acp_client.py:152` + `config.py:130/239` |
| 17 | **FFL F-01** submit 并发竞态（P0） | 守卫「in_progress 且 locked_by≠agent」被同 agent 并发绕过，后写覆盖前写；修法=只允许 available 进入 + 写回前复核 | FFL R1（`D:\Documents\WorkSpace\Test\FINDINGS.md`） | commit 07ed982；R2 回归 200/400 实证（`r2-t4-f01-concurrent-submit.json`） |
| 18 | **FFL F-02** 判重失效（P1） | 根因=判重只扫 locked 题（非竞态，串行即复现）；作用域改 available/in_progress/locked | FFL R1 | commit 07ed982；R2 串行重复 propose 第 2 次 400（`r2-t4-f02-propose-2.json`） |
| 19 | **FFL F-04~F-07**（P2×4） | F-04 类型校验 500→400（webui+Manager 双层）；F-05 幽灵实体 400+unknown_entities；F-06 标题 2-50/描述≤2000；F-07 stats 增 valid_submissions | FFL R1 | commit 07ed982；R2 回归 4/4 通过（`r2-t4-f0[4567]-*.json`）；test_arena 24→34 |
| 20 | **FFL R2 真实 flow 验收** | 3 道真题（有人进书房开灯/光照不足开挂灯/关门关空调）全链路 locked+vhass 孪生验证通过 | FFL R2（T5） | `results/t5-flow-*-verify.json`；生产 API 实证 valid_submissions=4 |
| 21 | **FFL R3 场景扩充 + B20 压测** | 新增 10 道真实场景题（温湿度/人体感应/光照/门窗联动），10/10 有 changed_by_replay=true 真转变证据；B20 三项语义（前置已满足降级 / 零断言降级 / 正常流不误伤）在真实链路全部符合设计 | FFL R3（T8a/T8b/T9/T10） | 生产 API 实证：15 locked / 15 valid_sub / 93.8%；`results/t8a-*.json`、`t8b-*`、`r3-t9-*` |

---

## B. 未闭环 / 存疑清单（★ 这是唯一必须保留的知识）

按建议优先级排序。

| # | 发现 | 为什么认为未闭环 | 来源 | 建议动作 |
|---|---|---|---|---|
| **B1** | **无独立 staging NR 实例**（staging 与 prod 同为 1990） | **架构级基建缺口**：`src/` 下 grep `AUTOFLOW_STAGING_NR` / `staging_nr_url` / `STAGING_PORT` **零命中**。导致所有写类 e2e 验证被 prod 护栏拦，跨 T007/T008/T009/T010 **连续 4 票 BLOCKED 且从未解除** | T007§2.2、T008§1.2(b)、T009§1.2、T010§2 | **最高优先**。立项独立 staging NR；这是 B2/B3 的共同根因 |
| **B5** | **编译产物缺「NR 真能跑」oracle** | `PLAN.md:722` 自述：「金标准编译 diff 未闭环——**编译产物 == NR 能跑的 JSON** 仍缺 oracle，过判分 ≠ 真机能触发，是当前最大风险面」。与 B1 同源，无守卫 | `docs/PLAN.md:722` | 与 B1 合并立项。**这是最根本的架构风险** |
| **B2** | F12 真机「反例翻案」从未实跑 | 守卫仅为 FakeNR 单测 + 分支断言单测，**真机 `verdict=断点` 四票均未取到**。T010§2 明确「需 `allow_prod=True` 透传部署/回滚 → 本环境未跑」 | T007§2.2 / T008§2 / T009§1.2 / T010§2 | 待 B1 落地后跑真机反例。**勿因单测绿即宣称 F12 全闭环** |
| **B3** | 150→turn_off 反向分支真机未验证 | 仅 50→turn_on 经 vhass 实证；反向被 prod 护栏拦，靠「布线正确+活动分支」**间接佐证** | T008§1.2(b) | 同 B1；补反向真机断言 |
| **B4** | 取值-label 流 verify 恒 fail-closed **未裁决** | T008§7 挂起两个选项（①视为期望行为并文档明示 ②在 vhass 加 JSONata 符号化评估），**未找到该裁决落地的文档或守卫**。`test_wb93_f13_*.py` 只覆盖「有数值种子→True」，T010§1 承认真实目录实体无种子时仍 fail-closed | T008§1.3/§7、T009§1.3、T010§1 | 请 REV 明确二选一并落文档/守卫；否则结论悬空 |
| **B6** | vhass 无 HA websocket —— 「决策关闭」≠「能力闭环」 | T010§3 决策不引入（合成事件+虚拟时间轴覆盖），但 T006 S4a 列出 **4 类流在 staging 根本不可验**（实时事件 / 时间等待 / 状态时间跃迁 / 跨自动化连锁）。T006 S4、T007§4.3 双次警告「合成 inject 是 test-double，**不得过度 claim**」 | T006 S4、T007§4、T010§3 | 保留决策，但须在文档标注不可验流类型，防「已验证」过度声明 |
| **B7** | 裸 24-hex 可靠建模缺口 | T005 交 REV「补解析」，安全侧。`tests/` 下 grep `24hex`/`hex` **零命中** | T005 S4a | 补解析 + 守卫 |
| **B8** | 子流程字段 drift 未捕获 | T005 明列，未找到对应守卫（仅有 `test_mcp_contract_drift.py`，非子流程字段） | T005 S4d | 补 drift 检测守卫 |
| **B9** | `SubflowSpec.outputs` under-declared | `source`/`samples`/`error` 实际发射但未声明 → 安全侧过度拦截（可用性回退）。T005 交 REV「收敛声明」，未见落地 | T005 S1/S2 | 补齐 outputs 声明 |
| **B10** | 20 个 MCP 工具未解析到 backing 方法 | T006 S5a 明确「匹配器局限，**需 REV 在 NAS 源码人工复核**」，该人工复核未见交付 | T006 S5a | 人工复核 20 项，区分「已删工具仍暴露」与「动态别名」 |
| **B11** | O2 收口后的误伤面未处理 | T006 S1c 建议未知实体降级为 `require_human_confirm`（应对实体刚重命名 / catalog 未同步）。O2 已 fail-closed，但**降级路径是否实现未验证** | T006 S1c | 确认是否需要人工确认降级通道 |
| **B12** | 陈旧守卫技术债（17 个红） | T003§1 指出 `test_w1/w2/w3` 的 17 个 failed 是「**为记录 bug 而写的陈旧守卫**（断言修复前行为）」，非回退。未见清理/翻转记录 | T003§1 | 翻转或删除，否则长期污染回归信号 |
| **B13** | 环境卫生：脏提案与遗留 flow | staging 残留 3 个提案（`pr_e9c274f3a593`/`pr_d30dc5007566`/`pr_17cb54ad146a`）未清；658 pending 提案中 57 个含未知实体（101 个 `light.fake_*` 探针残留）；`af_t011_smoke` 仍驻留 | VULN_REPORT§5、T006 S1a、T012 | 清理提案队列，否则后续爆炸半径统计持续失真 |
| **B14** | guard(0) 报错文案不精确 | T012 遗留#1：报「缺失 70 个 tab/subflow」实为「缺失 id 集（含节点）」。**行为正确，仅措辞** | T012 遗留#1 | P3 文案修正 |
| **B15** | `config.json` 明文密码 | T011/T012 判 P2 不阻塞、文档已推荐 env，**但文件本身仍明文** | T011§8、T012§6 | 已接受风险，登记不遗忘 |
| **B16** | WB16/WB17「仍未修 8 个 issue」未核 | 两个报告 §7/§6 各有「复测确认仍 broken」清单。**本次未逐条核验** —— 属 WB16/17 旧世代，多数可能已被 WB83–93 线覆盖，但**不能假定** | WB16§7、WB17§6 | **需单独一轮**逐条比对，是本次最大覆盖盲区 |
| **B18** | **A23 — e2e 错误文案环境错乱，缺守卫** | round5 工单（A23/A24/A26）要求补测试。实测：`test_decision_id_consistency.py`（A24）✅、`test_templates_brightness.py`（A26）✅、**A23 的文案断言测试在 `tests/` 下零命中**（grep `e2e_msg`/`e2e_reason`/`error_msg`/`a23` 均无）。优先级 LOW 但确实未闭环 | `AutoTest/WORKORDER_DEV_round5_cheap_fixes.md` §1 | 补一个纯字符串构造的最小单测，断言 `target` 进入 reasons 文案 |
| **B19** | `AutoTest/` 缺陷目录 A1–A31 未逐条核验 | 冷存阶段才发现该目录：`gateway-bug-report-20260808.md`（opencode 13 轮报告，A1–A31）+ `gateway-arch-optimization-report-20260810.md` + `architecture-landing-plan-20260809.md`。**只核验了 round5 工单的 A23/A24/A26，其余 28 项未核** | `AutoTest/*.md` | 与 B16 合并为「历史缺陷目录核验」专项 |
| **B17** | 子流程「安装到 NR」缺解释弹窗 | 需求原文：点击前需**弹提示向用户解释「安装」是做什么**（推送到用户自己的 NR 实例）。实测：安装按钮（`data-sf-ensure` → `/subflows/{key}/ensure`）已实现且幂等、删除按钮有 `confirm` 二次确认，**但安装按钮无解释弹窗** | `FEEDBACK_backlog_2026-08-02.md` §2 | UX 收尾项，归 dw 的文案/UX 工作 |
| **B22（已修，待部署）** | **外部子流程返回值驱动的分支 → 断言被「未激活分支」跳过 → 假绿放行**（P1，复核新发现） | 竞技场 DSL 可用 `调用子流程: history_state_at(...)`（编译通过、外部调用被记录），但 **vhass 不建模 HA 历史**（`vhass.py` 零命中 history）→ 分支取 `payload.found` 恒不可求值 → 走 else 分支。实测：DSL「历史有记录则**开灯**否则关灯」+ 预期 on → 实际重放 **`light.turn_off`**，断言却被标 `[跳过]（该后置条件来自未激活分支，按 P3-F1/P3-F2 跳过）` → `passed=True, fully_verified=True, verdict=放行`。**flow 做了与期望相反的动作却判放行** | 负责人 R4 前置探针（生产容器内 `arena._verify_flow` 实跑 A/A2/B 三组对照） | 改法：`inactive_effects` 跳过逻辑加前提——**该分支的判定依据必须可求值**；若分支条件引用 vhass 未建模的外部子流程返回值（history_* / http / 外部 link），不得静默 skip，应降级 `未充分验证` 或 fail-closed。与 B5/B20 同族（缺真 oracle），需守卫 + 部署一轮。**在修好前，竞技场禁止把外部子流程返回值当分支判据** |
| **B21（已修，待部署）** | **判重实体层不对称 → 超集可绕过**（P2，复核发现） | `_entity_overlap(new, existing) = "|new ∩ existing| / |new|"` **只按新题实体数做分母**：新题是旧题实体的**超集**时被稀释到阈值下。实况证据：「高湿自动开空调除湿」({湿度,空调}) vs 「高湿自动开空调除湿模式」({湿度,空调,温度,光照}) → overlap=**0.50**（≤0.6 漏判），但反向=1.00。即**挂 2 个无关传感器即可绕过实体层**；文本层该对仅 0.765，落 0.6-0.85 模糊区交 LLM 考官，LLM 不可用则 fail-open 放行 | 负责人 R3 复核（生产 tasks.json 实测 + arena.py:541-563） | 改法：`overlap = max(|∩|/|new|, |∩|/|existing|)`（双向取大，即任一方向覆盖率 >60% 即判重）。副作用面小：仅当新题实体面基本覆盖旧题时判重，属 fail-safe。需守卫 + 部署一轮 |
| **B20** | **竞技场验收 oracle 弱验证**（F-R2-01，P3） | ① state 断言在目标实体**已处于期望状态**时空转通过（`pre_state=on`、`changed_by_replay=false` 仍 ok，R2 T5 case1 实录）；② 自指 DSL **零断言**也放行。与 B5 同根：验收判分 ≠ 真行为改变 | FFL R2 `FINDINGS.md` R2 节 | **已修（待部署）**：`run_staging_gate` 沿 A22 诚实降级模式——①`pre_satisfied` 标记 + 全部 state 断言前置已满足时 fully_verified 降级 / require_change 硬失败；②零断言 fully_verified 降级。守卫 `tests/test_b20_vacuous_assertions.py`（4 例）；与 B1/B5 的深整合（真机转变验证）仍排期 |

---

## C. 原始报告去哪了

冷存至 **`D:\Documents\HAOS\AutoFlow_archive\2026-09-02\`**（仓库外，不进 git）：

| 冷存子目录 | 内容 |
|---|---|
| `docs/` | E 盘 `docs/` 的 3 份过程文档（CONVERGE / 旧 session handoff / WEBUI_UX_PROPOSAL v1） |
| `AutoFlow_docs/docs/` | `D:\Documents\HAOS\AutoFlow\docs` 全部历史报告（**57 份**） |
| `AutoFlow_handoff/handoff/` | `D:\Documents\HAOS\AutoFlow\handoff` 历史工单/交接卡（**17 项**） |
| `workspace/AutoFlow_Test/` | 测试工作区 A（44 份 md） |
| `workspace/AutoFlowTestv2/` | 测试工作区 B（63 份 md） |
| `workspace/AutoFlowTestv3/`、`AutoFlow_qwen/` | 其余 AutoFlow 工作区 |
| `workspace/AutoTest/` | ⚠️ **缺陷目录区**（A1–A31 报告 + round5 工单），见 B19 |
| `workspace/FEEDBACK_backlog_2026-08-02.md`、`TASK_safe_gate_ui.md` | 用户反馈汇总 + 安全闸 UI 工单（B17 来源） |
| `_TO_DELETE/` | 隔离区（**未删除，可恢复**）：`_TRASH_2026-08-05`(12M) + `_TRASH2_2026-08-05`(30M) |

> **冷存时未动的东西**：`D:\Documents\HAOS\workspace` 下的 `Poster-Wall`（**另一个项目**）、
> `MemoryAgent_Test`、`Smarthome_computer`、`Smarthome_nas`、`Tester` 均保留原位。
> `D:\Documents\HAOS\AutoFlow` 下的 `smarthome-assistant/`（个人 HA 配置）保留。
> **node_modules 不动** —— 它是依赖不是文档，删了会让 `.workbuddy/nr_local` 的 node-red 本地工具失效。

### ⚠️ 冷存时必须知道的三个坑

1. **`AutoFlow_Test` 不是 `AutoFlowTestv2` 的旧副本** —— 两者**双向各有独占内容**。
   `AutoFlow_Test` 独占 `WB26_in/`、`WB30_31_in/`、`BENCHMARK_REPORT.md`、`BREADTH_TEST_REPORT.md`、
   `DEEP_TEST_REPORT.md`、`FINAL_REPORT_V2.md`、`FINAL_SUMMARY.md`、`FINAL_TEST_REPORT.md`、
   `NEW_TEST_REPORT.md` 等。**删任一方都会丢知识，两个都留。**
2. **两个工作区的 `tests/` 都是旧快照**（只到 TEST_TICKET/RESULT_005）。
   活队列在 **`Z:\AutoFlowTestv2\tests\`（已到 012）**，share 原位保留继续协作，**不复制进仓库**。
3. **`FLOW_TEST_REPORT.md` / `TEST_REPORT.md` 不可信** —— 宣称 100% 通过，但同目录
   `flow_test_results.json` 记录 scenario_9/14 FAIL，属「假通过」报告。

### 可安全丢弃的纯过程物（若将来要再瘦身）

- 字节完全相同的重复：`BUG_REPORT.md`、`BUG_REPORT_RUNTIME.md`、`dsl_bug_hunt_round2.md`（两工作区各一份）
- `dsl_bug_hunt_round2~26`（25 份逐轮挖 bug 过程记录，结论已汇入 T001–T005 修复总账）
- `devlog_2026-07-29_WB22/WB23/WB24/WB25_WB26-fix.md`（4 份单日过程日志）
- 历史世代审计快照 `autoflow_WB2/3/4/5/18~21/23审计报告.md`、`autoflow_审计_report_WB12-WB15.md`
  —— ⚠️ **例外：WB16/WB17 暂勿删**，见 B16
- 测试提示词素材：`blackbox_hard_prompts_v2.md`、`deepseek_test_prompt.md`、`test_prompt_deepseek.md`、
  `doubao.md`、`opencode.md`
- 工具产物：`.pytest_cache/README.md`

> `VULN_REPORT.md` 建议**保留一份**并加「假阳性教材」标注 —— 它示范了
> 「报告声称通过/失败都必须以可执行证据为准」，比删掉更有教育价值。

---

## D. 覆盖度与证据强度声明（诚实边界）

按项目「禁止猜测式报告」的纪律，明确本台账的证据边界：

- **§A/§B 的主要证据**来自**活测试队列全文**（`Z:` 的 VULN_REPORT + TEST_RESULT_001–012）
  与 `E:\NAS\autoflow\tests\` 的**守卫测试文件存在性核验**。
- **所有 commit SHA（`77608f8` / `f2ffd00` / `b057aea` / `13cf5fc` / `1416fd8` 等）均为报告自述，未逐条复核。**
  表格中标注「SHA 未复核」的行即属此类 —— 证据强度是「守卫测试文件在仓」，不是「SHA 已验证」。
- `D:\Documents\HAOS\AutoFlow\docs` 的 57 份与 `handoff/` 的 17 份**只做了清单化 + 未闭环关键词扫描，
  未逐份通读**。B16（WB16/WB17 的 8 个 issue）是已知覆盖盲区。
- **第二处盲区（冷存阶段才发现）**：`D:\Documents\HAOS\workspace\AutoTest\` 是独立缺陷目录
  （A1–A31），子代理扫描时未覆盖。只核验了 round5 工单的 A23/A24/A26 → 发现 B18。见 B19。
- 提炼过程中未修改、移动或删除任何报告原文（冷存用的是**移动**而非删除，全部可恢复）。
