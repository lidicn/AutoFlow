---
name: autoflow
command: /autoflow
description: AutoFlow Automations 一站式技能。把自然语言家居场景变成经网关校验、能自证跑通的 Node-RED
  flow；网关内部用「编译器路径」（默认，产出干净可维护、无 spaghetti function 的
  flow）或「原生手写路径」（表达编译器覆盖不了的逻辑，需人审）。你的能力范围由 WebUI 给的身份决定，你无需也无需选箱。
version: 3.3.4
disable: true
---

# /autoflow —— Automations 场景技能（单入口）

你只有一个任务：**把用户的家居场景变成经网关校验、能跑通的 Node-RED flow**。
你不必纠结「走黑箱还是白箱」——那是网关内部的实现差异，对你不可见。
网关按你的**身份能力**自动决定走哪条内部路径：

| 你的身份能力 | 你能做什么 | 连哪个端点 |
|------|-----------|-----------|
| **仅编译器**（默认、最安全） | 只写简短 DSL，网关编译 + 虚拟 HA 重放自证，产出干净可信 flow | `http://127.0.0.1:8000/mcp`（用户面） |
| **编译器 + 原生手写** | 在 DSL 表达不了时，可直写 flow JSON 走提案闸（允许 function 节点）；**落提案待人审，不直写 NR** | `http://127.0.0.1:8000/mcp-white`（原生手写面，含部署刀） |
| **管理员** | 上述全集 + 运维/测试杠杆 | `http://127.0.0.1:8000/mcp-admin`（管理面） |

> 历史术语对照（仅供理解旧文档/日志）：「黑箱」= 编译器路径；「白箱」= 原生手写路径；`auto_*` 任务池 = 编译器子池，`wb_*` = 原生手写子池。新文档统一用「编译器路径 / 原生手写路径」。

## 连接（一次性配置）
- 你只有**一个身份码**（已预置在 MCP 客户端侧——deepseek++ 浏览器扩展的 Bearer 字段，或 WorkBuddy 的 MCP 配置）。网关靠这个码识别你的**能力**（而非要你自己选箱）。
- **本说明有意不重复写该码**：既避免凭证泄露，也防止 agent 误去改/重设它（改了反而会让身份映射错乱）。你只需确保客户端已按上表连对端点。
- **技能指导自愈**：MCP 重连只刷新工具 schema，不刷新系统提示里加载的本 skill 文档。若发现本说明与网关实际工具不一致（或你怀疑 skill 已更新），调用 `autoflow_get_skill(name="autoflow")` 拉取最新版全文即可，无需重启 agent。

## 通用铁律（两条路径都守）
1. **实体必须真实**：所有 `entity_id` 只能来自 `autoflow_resolve_entity` 的返回，**绝不靠记忆拼** `light.xxx`——拼错网关闸门直接判 FAIL。
2. **你不需要猜设备是 light 还是 switch**：把自然语言设备名丢给 `autoflow_resolve_entity(name=..., area=...)`，网关返回该区域所有沾边实体（含 `domain`、`state` 当前状态、`possible_states` 可能状态），**由你挑**该用哪个。
   - ★★ **同名设备有多个子实体，必须按用途挑对的那个**：一个自然语言名可能返回同一物理设备的好几个 entity。典型：「书房人体传感器」会同时返回 `binary_sensor..._motion`(移动检测) 和 `sensor..._illuminance`(光照度数值)——做"有人移动"触发选 `_motion`；做"光照度低于 10"数值条件选 `_illuminance`。**两个是不同实体，别混用、别只取第一个。**
   - ★★ **吊灯 ≠ 台灯 ≠ 牌匾灯泡**：书房吊灯/射灯是 `switch` 域（`switch.lumi..._p_3_1` / `_p_2_1`），动作用 `switch.turn_on`；只有台灯(`light.philips...rwread...`)、牌匾灯泡(`light.philips...cbulb...`)是 `light` 域。名字里带"开关/电脑"的（如 `switch.d4f0eaeab731_switch`）是干扰项，绝不拿来冒充灯。拿不准就把候选读全、按 domain+friendly_name 挑。
3. **零信任**：你只做「发现 + 提案/部署」。不部署审批（人在 WebUI 做）、不直连 HA/NR、不提交任何地址/端口/令牌。
4. **resolve 返回 0 候选就停手**：说明该区域根本无此设备（如「客厅窗帘」客厅没窗帘）。**不要跨区 scavenge 去别的房间顺一个凑**——应改用 `autoflow_report_issue(title=…, body=…, task_id="…", severity="high", category="entity")` 上报（属任务编排错误），人类会看到。
5. **聊天框优先（交互原则）**：你是**交互式 chat agent**，人类就在这段对话里。轻量、可逆的歧义（如「开哪盏灯」「用什么亮度」「叫什么名」）**直接在聊天框问、等人回复**，不要为此调 `autoflow_request_decision`、也别让人跑去 WebUI 点选——把正在对话协作的人类切去另一个界面会有割裂感。仅当 (1) agent **无人值守**（headless / 任务池 worker / 定时任务，对话里没人）或 (2) 该选择需**持久化 / 可审计**地记进网关时，才走 `autoflow_request_decision` → WebUI 通道。完整通道选择见下方「人类决策协议」。

## 任务池（可选认领，结构化练手）
除自由创作外，还有结构化任务池供你练手/贡献语料：
- **编译器子池 `auto_*`（250 条，tier=auto）**：覆盖 15 区域 + 18+ DSL 指令面，条件类约 71 条。编译器/双能力身份领到此池。
- **原生手写子池 `wb_*`（25 条，tier=auto_wb）**：覆盖原生节点族（switch/change/template/delay/debug 等），专练 `原生节点:` 逃逸与白名单边界。原生手写/双能力身份领到此池。
- 认领：`autoflow_claim_task()` **按你的能力自动选池**——仅编译器→只领 `auto_*`；编译器+原生手写→只领 `wb_*`；**双能力→先领 `wb_*`，空了再领 `auto_*`**（一套身份干两池活）。
- 提交：`autoflow_submit_result(task_id="<id>", dsl="...")`。原生手写任务用 `原生节点:` 写手写 NR 节点；编译器禁的 function/exec 对原生手写同样永久禁止（白名单生效）。
- 浏览与进度：`autoflow_list_tasks(only_mine?, status?, limit?, offset?, fields?)` 看池子全貌/自己认领的任务；`autoflow_list_pending()` 查自己提交后的待人工确认项进度；`autoflow_set_plan(current?, overall?, completed_append?)` 把当前计划/进展写给 WebUI 的人类看（无人值守跑池时建议每领一条更新一次）。
- 这是编译器语料探针：原生手写跑通的原生节点片段，可蒸馏反哺编译器。

---

# ▶ 编译器路径（默认路径：写 DSL，产出干净可信 flow）

你把用户给的自然语言家居场景，变成一段**经网关校验、能自证跑通**的 Node-RED flow 提案。

你**不直接写 flow JSON**，只写一段简短的语义 **DSL**。网关会：解析 → 编译成真实 NR flow →
在本机虚拟 HA 孪生里重放动作 → 断言灯真的亮了/开关真的动了。这套"自证"是编译器路径的核心价值，别绕过它。

你认领的是 `auto_*` 专属任务池（编译器子池 250 条）。直接 `autoflow_claim_task()` 领、直接写 DSL，不用手动指定 tier。

## 编译器路径标准流程（照做即可）
1. **解析设备名**（唯一发现入口）：
   ```
   autoflow_resolve_entity(name="显示器挂灯", area="书房")
   autoflow_resolve_entity(name="书房牌匾灯泡", area="书房")
   ```
   - 返回 `candidates:[{entity_id, friendly_name, domain, area, state, possible_states, confidence}]`。
   - `possible_states` 直接告诉你这设备能同步到哪些状态（如 `["on","off"]` / `["open","closed"]`），省去你猜。
   - 从候选里挑出你要的 `entity_id`（优先 `confidence=high` 或排序第一）。把选中的 id 记下来。
2. **套模板**（首选）：
   - `autoflow_list_templates()` → 现有：`motion_to_light` / `entry_announce` / `tts_announce` / `api_call_chat` / `api_call_image`
   - `autoflow_render_template(name="motion_to_light", values_json='{"room":"书房","sensor":"<真实传感器>","light":"<真实灯>","brightness":"80"}')`
   - 拿到含 `预期:` 块的 DSL，**原样保留**。没有合适模板时才手写 DSL（语法见下）。
3. **提交 + 跑闸门**：
   ```
   autoflow_propose_dsl(
     dsl="<上一步的完整 DSL>",
     expected_postconditions_json='[{"entity_id":"<真实灯>","state":"on"}]',
     resolved_entities_json='["<真实灯>","<真实传感器>"]',
     strict=False,           # 可选；默认 False=lint 仅随回执透出；True=任何 error/真实 warning 升格为阻断，不落提案（R22 inject 手动测试节点除外）
     require_e2e=False       # 可选；默认 False=部署沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E。True=提案带 e2e 意图，人类点「部署到 NR」时 deploy_proposal 会真正先跑一次实机验证闸（verdict≠通过即拦截部署）。修复 iss_8d3cffaa96
   )
   ```
   - `resolved_entities_json` 把你在第 1 步选中的 entity_id 全部列上，闸门会强制校验。
   - `require_e2e=True`（可选）：把「部署前必须实机跑通」的意图**写进提案**，人类在 WebUI「场景提案」面板点「部署到 NR」时，`deploy_proposal` 会先跑 `run_e2e_trace_raw`（部署到 staging → 触发 → 抓 trace → 比对 → 回滚），仅 `verdict=通过` 才放行，否则拦截部署逼修 flow。无法验证（如缺触发入口）fail-open 放行。**这是部署主路径真正生效的 e2e 闸**——直接调 `autoflow_run_e2e_trace` 仍是手动验证的最直接入口，但 `require_e2e=True` 让「部署即验证」成为提案的强制纪律，不再被静默吞掉。
   - `strict=True`（可选，默认 `False`）：编译产物只要被 lint 抓到**任一 error/真实 warning**，就直接挡下、返回 `stage=lint_strict` + `strict_blocked=True` + `blocked_by:[规则列表]`，**不落提案**；修完再交。适合「零容忍任何反模式」的提交。`strict=False`（默认）= lint 结果只随回执带出、不阻断（fail-open，让你先看见、人审决定）。
     - **例外**：`R22` 中「inject 节点缺 repeat/crontab/once」属编译器为每个 flow 生成的**手动测试节点**（by-design），strict **不拦**——否则所有事件驱动自动化都会被 strict 误杀。其余 error 与真实 warning（如 R26 变量↔分支作用域错配）一律挡下。
4. **核对成功判据**：`ok==true` 且 `gate.passed==true` 且 `gate.assertions` 全 ok，拿到 `proposal_id`。
   失败就按 `gate.stage` 修（`entity_check` 失败=实体不对，回第 1 步重选）。
   - **编译错误（stage=compile）**：返回里带结构化 `compile_error` 信封，机读自修正，别只盯着 `error` 文本：
     - `compile_error.code` —— `C_*` 错误码（如 `C_MISSING_TRIGGER` / `C_ACTION_FORMAT` / `C_SUBFLOW_UNKNOWN` / `C_TRIGGER_FORMAT` …），直接告诉你哪类错。
     - `compile_error.line` —— 出错行号（`None` 表示全文级，如缺触发）。
     - `compile_error.hint` —— 一句话「怎么改」（绝大多数已内嵌「（建议：…）」）。
     - 典型自修：`C_ACTION_FORMAT`+`line=3` → 第 3 行动作写成 `light.turn_on light.x`（缺括号），改成 `light.turn_on(light.x)`；`C_MISSING_TRIGGER` → 补一行 `触发: ...`；`C_SUBFLOW_UNKNOWN` → 子流程名拼错，查 `autoflow_dsl_help` 修正。改完重交 `autoflow_propose_dsl` 即可。

## 编译器 DSL 语法速查（手写时用；语法随时 `autoflow_dsl_help()` 复查）
```
场景: <名称>
触发: <entity_id> <状态值>        # 例 binary_sensor.study_motion on
触发: 定时 每天 22:30            # 或定时
触发: inject                     # 或手动
取值: <entity_id> <字段名>       # 读实体 state 进 msg.<字段名>，供下面「分支」做数值判断
条件: <jsonata 表达式>           # 可选
动作: <domain>.<service>(<entity_id>, k=值)   # 例 light.turn_on(light.study, brightness_pct=80)
构建: <JSON对象 或 JSONata表达式>   # 把 msg.payload 设为请求体；动态值用反引号 `payload`
请求: <METHOD> <url> [K=V headers]   # 不带字面 body 时自动把上游「构建」的 msg.payload 作为请求体发送
调用子流程: demo_notify(text=欢迎回家, room=书房, level=一般)
分支: <jsonata 条件>
  动作: ...
否则:
  动作: ...
时间段: [工作日|周末] HH:MM-HH:MM   # 仅在时段内才继续执行缩进块
  动作: ...
延时: 3 秒
并行:
  动作: ...
  动作: ...
```
可用子流程：`demo_notify`（智能语音播报队列：text/room/level/priority/mode…）、`bark_push`（iPhone 通知：title/body/level）、`history_*` 历史查询（entity/start/end/metric/at/state…）。
- **原生节点逃逸（Phase 4，中风险）**：若只想在 DSL 里嵌一小段手写 NR 节点（如复合 switch 条件、JSONata 变换），可用 `原生节点:` 原语，例：
  `原生节点: {"type":"switch","property":"payload.lux","propertyType":"msg","rules":[{"t":"lt","v":10}]}`
  它默认**关闭**，需在 WebUI 设置手动开启、可随时关；白名单永久禁 `function`/`exec`。能用原生节点解决的别升格整段原生手写 flow（原生手写另走 `deploy_raw`）。

## 编译器四类易错场景范例（照抄结构，把 entity_id 换成你 resolve 到的真实值）

**① 多触发 OR 汇聚**（"有人 或 开门 → 都开灯"）：连写多行 `触发:`，彼此之间不夹动作。
```
场景: 书房有人或开门开台灯
触发: binary_sensor.0x00158d0001a2520d_motion on
触发: binary_sensor.e4aaec34e80f_contact on
动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)
```

**② 数值条件 + 否则**（"光照度<10 且有人 → 开灯，否则不动"）：`取值:` 读光照度进 msg，`分支:` 用 `$number()` 数值比较。
注意 motion 与 illuminance 是"书房人体传感器"这一个名字下的两个子实体，各自 resolve。
```
场景: 书房暗且有人开台灯
触发: binary_sensor.0x00158d0001a2520d_motion on
取值: sensor.0x00158d0001a2520d_illuminance lux
分支: $number(lux) < 10
  动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)
否则:
  注释: 光线足够，不动作
```

**③ 工作日时间段触发**（"工作日 20:00-23:00 有人 → 开吊灯"）：`时间段:` 带星期限定；吊灯是 switch 域。
```
场景: 工作日晚间有人开吊灯
触发: binary_sensor.0x00158d0001a2520d_motion on
时间段: 工作日 20:00-23:00
  动作: switch.turn_on(switch.lumi_cn_lumi_158d000239c546_aq1_on_p_3_1)
```

**④ 开灯 + TTS 语音播报**（跨域：light 动作 + demo_notify 子流程）：
```
场景: 书房有人开灯并播报
触发: binary_sensor.0x00158d0001a2520d_motion on
动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)
调用子流程: demo_notify(text=书房已有人，灯已打开, room=书房, level=一般)
```

**⑤ 条件分支必包**（"如果光线<10 才开灯，否则不动"——典型的「如果…才…」必须包 分支/否则）：
❌ 错误写法（裸写动作，条件被丢，闸门会判 `lint_error`）：
```
场景: 书房暗才开灯
触发: binary_sensor.0x00158d0001a2520d_motion on
动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)
```
✅ 正确写法（取值+分支+否则，条件成立才开）：
```
场景: 书房暗才开灯
触发: binary_sensor.0x00158d0001a2520d_motion on
取值: sensor.0x00158d0001a2520d_illuminance lux
分支: $number(lux) < 10
  动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)
否则:
  注释: 光线够，不动作
```

## 编译器提交前自检（防丢分支）
写 DSL 前扫一眼用户原话：只要出现 **如果 / 才 / 超过 / 当…则 / 只有…才** 任一词，就必须确认 DSL 里含 `分支:`（且配 `否则:`）。
- 漏了 `分支:` → 动作变成无条件执行，**编译器入口（`autoflow_propose_dsl`）现在对自由 DSL 也会硬拦**：只要 DSL 含条件语义（如果/才/超过/当…则/只有…才 等）但编译产物不含任何分支/条件门节点，直接返回 `ok=False` + `stage=lint_branch_required` + `error=R_branch_required`，**不落提案**，白干。这是修复「静默无条件执行」的硬闸（iss_ebfe742222），无论是否 `strict` 都生效。
- 不确定怎么写分支 → 看上方第⑤类范例，或直接 `autoflow_dsl_help()` 查 `examples.数值条件`。
- 想让闸门「零容忍任何反模式」自动挡下 → 提交时加 `strict=True`（见上方 `autoflow_propose_dsl` 说明）：任何 lint error/warning 都会升格为阻断、不落提案，比靠肉眼扫 DSL 更稳。

## 编译器路径交付（给用户）
完成后只回三样：`proposal_id`、闸门摘要（replayed_services + assertions）、一句话结论
「编译器链路通畅：发现→模板→提案→闸门断言通过；真机部署请在 WebUI 一键完成」。
**不要**自己调任何部署/批准工具（你也调不到，除非你有原生手写能力且连了 /mcp-white 或 /mcp-admin）。

---

# ▶ 原生手写路径（逃生舱：直写 flow，需人审）

你具备**原生手写能力**（仅当你的身份模式含原生手写时这段才适用）——你仍是同一个 agent，只是多了直写 flow 的权限。这里**放手让你直接写 Node-RED flow JSON**，
只有很少的约束。网关不编译你的逻辑，只做安全 sanitize 后**落为提案**（供人类审核后部署），并**留存一份你产出的
JSON**（供人类迭代编译器）。这是"看看 agent 到底能写出什么、编译器边界在哪"的实验通道。

你认领的是 `wb_*` 专属子池（25 条）。`autoflow_claim_task()` 按你的原生手写能力自动取 tier=auto_wb，直接返回 `wb_*` 任务。

## 你的固定 tab（隔离约定，务必遵守）
- 你产出的每个 flow，`label` **必须以 `[DS白箱] ` 开头**，例如 `[DS白箱] 书房人来开灯`。
- 这样所有产出在 NR tab 列表里成组、和用户/网关自身的 flow 互不干扰，人类可一键清理。
- **别碰**不带这个前缀的任何 flow/tab。

## 原生手写少量硬约束（只有这些）
1. **实体真实（先 resolve 再写）**：`entity_id` 必须来自 `autoflow_resolve_entity`（名称→候选，首选）或 `autoflow_list_entities`（按域/区域/关键词浏览目录）的返回，别编造。
   **严禁「名字相近就猜」**（例：绝不能用 switch 域的『书房电脑 开关』冒充『显示器挂灯』），必须以 resolve 返回为准。
   （原生手写不强制编译，但编造实体在真机跑不起来，也污染留存语料。）
   **若 `resolve_entity` 对指定区域+设备返回 0 候选，立即停手**——说明该区域根本没有此设备，不要去其他房间顺一个来凑。改用 `autoflow_report_issue` 上报。
2. **HA server 占位符**：所有 Home Assistant 节点的 `"server"` 字段填字符串
   `"REPLACE_WITH_HA_SERVER"`，网关部署时自动替换成真实 server id。别自己猜 server id。
3. **不提交 HA/NR 地址、端口、令牌**；不直连 HA/NR。
4. 其余放手：节点类型、连线、function（原生手写允许）、结构由你决定。

## 网关会替你自动兜底（放心，不用手动处理）
- 节点 id 撞车 → 自动重映射为全局唯一 id（你可以随便用 `n1`/`a` 等短 id）。
- `z` 占位符（如 `"1"`）→ 自动改写为真实 flow id。
- 节点缺 `x`/`y` 坐标 → 自动补网格坐标（**但建议你还是带上**，否则 NR 静默丢节点是最坑的）。
- HTTP Request 的 `body`（`bodyType:"json"`）→ 记得写成 **JSON 对象**，不要写成字符串。

## ⚠️ `flow_json` 传参格式（实测踩坑，务必看）
网关对 `autoflow_deploy_raw` 的 `flow_json` 做了**宽容解析**，下面三种都能成功落为提案：
- ① **完整 flow 对象字符串**（首选，最稳）：
  `"{\"id\": \"...\", \"label\": \"[DS白箱] x\", \"nodes\": [ ... ]}"`
- ② **节点数组字符串**：`"[ {\"id\":\"n1\",...}, {\"id\":\"n2\",...} ]"`
  → 网关自动包成 `{"nodes":[...]}` 再部署。
- ③ **直接传对象/数组本身**（MCP 框架有时会把 JSON 直接解析成对象再传进来）→ 也接受。

**会失败的写法（别这样）**：
- 传「纯字符串但内容不是合法 JSON」（如漏了引号/多了换行）→ 解析失败被拒，自动进 `raw_deploys.jsonl` 失败模式库。
- 字符串内部出现**未转义引号或裸换行** → JSON 解析失败。

**结论**：最省心的做法是 **① 直接传「完整 flow 对象字符串」**；若只写了节点列表，传 **② 节点数组字符串** 也行。两种都能一次过，不用反复重试。

## 列房间设备清单（inventory）
- **按条件浏览目录（首选，P1 新增的受控工具）**：`autoflow_list_entities(domain?, area?, keyword?, limit=50, offset=0)`
  返回该过滤条件下所有实体（`entity_id`/`friendly_name`/`domain`/`area`/`state`/`possible_states`），
  分页透明回报 `matched_count`/`returned`/`truncated`/`next_offset`。**列全房间设备**就调
  `autoflow_list_entities(area="主卧室")`，按 `next_offset` 翻页直到取全——这是网关现在唯一 sanctioned 的目录浏览入口。
- **按自然语言设备名解析（写 flow 前必用）**：`autoflow_resolve_entity(name="显示器挂灯", area="书房")`
  返回该区域所有沾边实体（含 `domain`/`state`/`possible_states`），**由你挑**该用哪个（同名多子实体别混用，见通用铁律 2）。
- **跨会话找回已部署/待办 Automations（P3 注册表）**：`autoflow_list_automations(keyword?, only=all/deployed/pending)`
  列出历史上已落地部署的 flow 与待审/待部署提案，写新 flow 前先查有没有可复用的，避免重复造轮子。
- **⚠️ 弱客户端响应截断（如 deepseek++ 浏览器插件 ~32KB 会失败）**：
  用 `autoflow_list_entities(area="主卧室")` 分页拉全（每页默认 50，绝不会超阈值）；
  **不要反复重试大全域导出**——必败。逐域下钻 `autoflow_list_entities(domain="light", area="主卧室")` 也行。
- **注意**：目录浏览返回的是**全部实体**（含 sensor/event/device_tracker），比旧 `room_summary` 的 key_states 骨架全；
  写 flow 时实体 id 一律以 `autoflow_resolve_entity` 返回为准，勿凭记忆拼。

## 只读辅助工具（三面板通用，无需部署权限）
写 flow / 调工具前，这几个只读工具能省去你搭节点或猜身份：
- `autoflow_whoami()`：返回你的身份（name/agent_id/mode/tier/status）、当前所连面板 `endpoint`、该模式能做什么，以及**你当前面板实际可调用的全部工具名清单**（实时自省，绝不会过期）。先调它一眼看清自己连对没连错面板、能干嘛。
- `autoflow_get_entity_state(entity_id)`：直连 HA 读某实体**当前实时状态**（返回 `{entity_id,state,attributes,last_changed,last_updated,...}`，`source:"live"`）。省去为「查当前状态」专门搭一个 `api-current-state` 节点；实时读不到（HA 不可达）会自动回退到网关设备目录缓存（`source:"catalog_cache"`，并标注可能非最新）。写 flow 前用它确认设备此刻到底 on/off。
- `autoflow_list_entities(domain?, area?, keyword?)`：受控目录浏览（列房间设备清单的唯一 sanctioned 入口，见上）。
- `autoflow_list_automations(keyword?, only=all/deployed/pending)`：跨会话 Automations 注册表，写新 flow 前先查可复用项（见上）。
- `autoflow_get_flow(flow_id)`：只读**回看**已部署 flow 的完整节点图（`flow_json.nodes`）+ `source` 来源标记（编译器 / 原生手写 / 外部导入）+ `label` / `node_count` / **`disabled`**（该 tab 当前是否停用，与 `autoflow_list_tabs` 同源一致）。**不修改任何状态**——用于差分测试、复现问题时取回真实落盘结构、确认部署产物长什么样。正常返回 `ok=True`；`flow_id` 为空 / 不存在 / 该 tab 无节点会分别返回明确 `ok=False`（含原因）。注意它取的是「已部署到 NR 的真实 flow」，不是提案——提案看 `autoflow_list_automations(only=pending)`。
- `autoflow_list_tabs(only_disabled?, keyword?)`：**只读·巡检**——列出 Node-RED 中**所有 tab 流程**（每个 tab=一个 flow，含用户手工/第三方创建的「卧室灯」「客厅」等），每条含 `id / label / disabled / node_count / source`，按 `node_count` 降序；返回附 `count_disabled`（全集里被禁用的 tab 总数）。`only_disabled=True` 只看被停用的 tab；`keyword` 按 label/id 模糊过滤。**纯旁路只读、幂等（5s 缓存）**，不 dump 节点内容——想看某 tab 节点详情再拿 id 去 `autoflow_get_flow`。这是发现「用户手工建的 tab 被关了/开着」的唯一 sanctioned 入口（编译器路径看不到这些 tab）。
- `autoflow_get_nr_flow()`：**只读·诊断**——一次性扫描 Node-RED **全部子流程定义**的结构完整性，灭绝「空壳假 PASS」。返回 `{ok, source, subflows:[{id,name,internal_node_count,empty_shell,has_mustache_entity,internal_types}], empty_shells:[id...], any_empty_shell}`。**不修改任何状态**。用途：子流程（如 `history_*` 历史查询）部署后 / 日常巡检时，一次调用即可确认线上 def 是否真实带满内部节点（空壳=内部节点数 0=无取数能力，比取空更糟）、是否存在 entityId 仍为 `{{...}}` 的降级节点。这是验证「子流程真部署成功」的权威手段——`autoflow_get_flow` 只能看单个 tab，本工具直接审计所有子流程 def 的结构。建议提案被人类部署后、或怀疑子流程「看似成功实则空壳」时调用。
- 诊断对（trigger_inject + debug_read，三面板通用）：
  - `autoflow_trigger_inject(flow_id?, inject_id?)`：真实触发 NR 中的 inject 节点让 flow 跑一次（返回 `triggered:[{id,name,status}]`，status=200 即触发成功）。注意：若目标 flow 引用了**未注册的子流程实例**，NR 会对 inject 返回 404——先确认子流程已注册（`autoflow_get_nr_flow` 审计），而非误判为触发端点坏了。
  - `autoflow_debug_read(flow_id?, node_id?, since?, limit?, full?)`：读回 NR debug 帧（探针输出）。`since` 传触发前的 Unix 秒过滤旧帧。
    **返回帧结构契约（C-7 实测真值，消费方务必遵守）**：
    - `events[]` 每个元素：`payload` 是 **`str`（JSON 字符串，不是 object）**——消费前必须 `json.loads(payload)`；`payload_preview` 是独立的 **~186 字符短预览**（不含完整结构，排错别只看它）。
    - **排序：最新在前**（`received_at` 降序）——`events[0]` 即最新帧，多实体流一次触发产多帧时直接取 `[0]`，**不要再 `reversed()`**（实测同节点 4 帧 385.76→383.27→380.79→346.66，数组序与之一致）。
    - **截断**：`payload`（即便 `full=True`）在 **~2000 字符**处截断并追加 `...(truncated,N chars)`（N=原始长度，实测 57961）。截断后**不再是合法 JSON**，`json.loads` 前须先剥离 `(truncated,...)` 标记；超长帧常被截断掩盖 `_hist_error` 之类根因，必要时换更短窗口或分段触发。
    - `full=False`（默认）只给 `payload_preview`；排错/断言帧内容务必 `full=True` 并处理好上面的「string + 截断」契约。
  - 标准诊断闭环：部署 → `trigger_inject` → `debug_read(since=触发前时间戳)` → 断言探针帧到齐。这是「flow 真的跑起来了」的最直接实证链。
  - `autoflow_apply_state_from_debug(entity_id?, state?, ...)`：**debug 帧 → 状态推断 → apply B 段确认闸的胶水工具**（`autoflow_debug_read` 取帧 + 解析 entity_id/state + 推断 `light.turn_on/off` 等服务 + 进 `commit_ha_service` 确认闸 三步合一；黑箱不可见，调用级 `-32601` 拦截）。
    - 行为：内部读 debug 帧 → 取 `events[0]`（**最新帧**，见上排序契约）→ 解析出 entity_id 与 state → 推断服务 → 进确认闸返回 `pending_id`（同 B 段，需人批准才真下发 HA）。
    - ⚠️ **`entity_id` / `state` 直传不是绕过路径**：即便你直传这两个参数，工具**仍要求先存在 debug 帧**，否则报「无 debug 回读帧：无法基于空观测写回 HA（#607 证据要求）」。这两个参数只用于帧内**筛选/覆盖**（指定看哪个实体、用哪个状态），**不是独立写通道**——没有真帧就不要调它。
    - fail-closed：帧里 state 为 `unavailable`/`unknown` 等禁止态会明确拒绝（绝不静默下发 turn_off）；无帧 → 明确报错而非空转。
- **apply 闭环三件套**（apply / apply_rollback / get_trace，**黑箱不可见，仅原生手写/管理员面板**，调用级 `-32601` 拦截；A/C 改 flow 高风险、B 落状态低风险）：
  - `autoflow_apply(flow_id?, correction_json, mode, reason?, auto_approve=False, trace_id?)`：**apply 闭环唯一编排入口**。
    - `mode`：`A`=观测驱动改 flow（重编译整条 DSL）、`C`=热补丁（node_patches 外科式局部改）、`B`=回读数据落 HA 状态（写 `commit_ha_service` 确认闸）。
    - `correction_json` 形如 `{"dsl":"...", "reason":"为什么改"}`（A）或 `{"node_patches":[{"match":{"id":"n2"},"set":{"name":"新名"}}], "reason":"..."}`（C）或 `{"domain":"light","service":"turn_on","data":{"entity_id":"light.x"},"reason":"..."}`（B）。
    - 安全模型：**A/C 高风险默认 `auto_approve=False` 只请示不落地**——返回 `{pending:true, decision_id, trace_id, snapshot_path}`，人类在 WebUI 批准后**以相同 trace_id + `auto_approve=True` 重调**才真正写回（系统未批准零改动）；**B 低风险本层 audit auto-pass** 直接透传 `commit_ha_service`（其自带确认闸）。
    - 返回统一审计信封 `{ok, applied, pending, mode, trace_id, flow_id, snapshot_path, decision_id?, stage, result, warnings[], error?}`。
  - `autoflow_apply_rollback(trace_id, auto_approve=False)`：把某次 apply 改动的 flow 还原到 apply 前快照。**还原也是改 flow=高风险**：默认进决策闸（返回 `{pending, decision_id}`），`auto_approve=True` 重调才执行。mode=B 无回滚点会明确报错；空快照拒绝写回（绝不空 flow 覆盖线上）。
  - `autoflow_get_trace(trace_id)`：**读回某次 apply 的完整审计轨迹**（`data/apply_traces/<trace_id>.json`）。用于独立核对闭环证据：两阶段决策闸是否同 trace_id 复用回滚点、pending→approved 是否真写回、ROLLBACK 是否落痕、审计字段是否齐全。**只读不改任何状态**。返回 `{ok, trace_id, trace:{events:[...], flow_id, snapshot_path, ...}}`；不存在返回 `{ok:False, error}`。
- 部署后状态管理三工具（modify_flow / observe_deploy / run_e2e_trace）：
  - `autoflow_modify_flow(flow_id, dsl?, node_patches?)`：**外科式改已部署 flow**（本段少见的「可写」工具，其余皆只读）。必填 `flow_id`；`dsl` 与 `node_patches` **二选一**（都空 → 报错）。**无 `flow_json` 参数**（别和 `autoflow_run_e2e_trace` 混）。`dsl`=重编译并复用该 flow 的 id/label 原地更新；`node_patches`=JSON patch 列表 `[{match:{id|name|type}, set:{...}, remove:[...]}]` 只改匹配节点。两路都过实体校验 + 节点注册表硬拦 + 部署。`返回 {ok, flow_id, label, changed_nodes, node_count, mode}`。
  - `autoflow_set_tab_state(flow_id, enabled, reason?)`：**切 tab 开关（写·确认闸，仅管理员/原生手写面板可见，编译器面板不可见）**——启用/禁用单个 NR tab 流程（如临时停用「客厅语音播报」tab，而非删它）。`enabled=True` 启用、`False` 禁用；`reason` 进待确认项供人审。**注意它不是删除**：节点仍在，只是不跑。这是经人审确认闸的写操作——提交即校验 `flow_id` 存在性（不存在→`unknown=True` 直接拒绝，不会落幽灵待确认），返回 `{ok, pending_id, needs_approval:true}`，人类在 WebUI 批准后才真正翻 NR 的 tab `disabled` 字段。⚠️ 禁用**核心受保护 tab**（心跳/HA 桥接）会被拦截（防误关全家瘫痪）；启用核心 tab 不受限。先 `autoflow_list_tabs` 拿 `flow_id` + 看 `disabled` 现状，再决定切不切。
  - `autoflow_observe_deploy(flow_id, window_sec?, poll_interval?)`：部署后短窗只读观测（仅 staging 实例）。**查账本（网关部署记录）而非 NR**：flow 不在本网关账本 → `{ok:False, error:"flow 不在本网关账本，无法观测"}`；即仅「经 propose→deploy 链路部署的」才能观测，`autoflow_run_e2e_trace` 的临时 flow 不在账本无法观测。只读：仅轮询 HA 状态，不写 HA/NR、不自动撤回。返回 `{ok, flow_id, trace_id, observed, changed_entities, missing_entities, attribution, suggestion, window_sec, polls}`。
  - `autoflow_run_e2e_trace(dsl? | flow_json?)`：端到端断点追踪。必填 `dsl` 或 `flow_json` **二选一**（都空 → 报错）；`dsl`→编译器路径，`flow_json`→原生手写路径（裸节点数组或 `{"nodes":[...]}`）。流程：编译/归一 → 实体校验 → 插桩 → 部署到 staging 实例 → 触发 → 读回 trace → 比对 → **回滚删除临时 flow**。返回的 `flow_id` 是临时 id、调用即删：随后 `get_flow`/`observe_deploy` 该 id 必 404 /「不在账本」。要回看用 `get_flow` 查「正式部署」的 flow_id。返回 `{e2e, flow_id(临时), verdict, reasons, report, trace, postconditions, live, ...}`，直接消费 `report.failed_at`/`verdict`。

> **⚠️ 注册表 / NR 分叉告警（别默认 deployed flow_id 能 `get_flow` 回来）**：网关有**两份认知**——账本（记「经网关部署了什么」）与 NR 真实流（staging 实例上的 tab），二者会分叉。`autoflow_get_flow` 读 **NR**；`autoflow_observe_deploy` 查 **账本**；`autoflow_run_e2e_trace` 的临时 flow **两边都清理**。故：经 `run_e2e_trace` 拿到的 flow_id 既不能 `get_flow` 也不能 `observe_deploy`（已删、不在账本）；即便「正式部署」的 flow，若用户在 NR UI 手动删了 tab，账本仍记 deployed 但 NR 已无 → `get_flow` 404。结论：别缓存 e2e 临时 flow_id 去做回看；要回看就用 `get_flow`（查 NR）或 `observe_deploy`（查账本）针对**正式部署**的 flow_id，且两者可能不一致——这正是需求 1「账本↔NR 流真值对账」要根治的根因。


## 原生手写标准流程
1. `autoflow_whoami()` 确认自己有原生手写能力（或双能力）——返回的 `mode`/`endpoint`/`工具清单` 就是你真实可调用的权限，照着调即可。
2. 列设备：写 flow 前**必须先用 `autoflow_resolve_entity(name="显示器挂灯", area="书房")` 解析每个设备中文名**，取返回的 entity_id（优先 confidence=high）写进 flow_json；
   列全房间设备清单用 `autoflow_list_entities(area="书房")` 分页拉全（见上「列房间设备清单」）；拿具体实体也可 `autoflow_list_entities(domain=..., keyword=...)` 按条件浏览。
3b. **⚠️ 原生手写节点字段命名铁律（NR5 ha-websocket）**：Node-RED 5 的 `api-current-state` / `api-call-service` 等
   HA 节点要求 **camelCase** 字段名——实体用 `entityId`（不是 snake_case 的 `entity_id`），服务调用用
   `entityId` + `dataType:"json"` 的 `data`。写错 `entity_id` 会被 L1 lint 的 **R19** 拦下（提示会直接告诉你
   改成 `entityId`），但部署前最好就写对，别等闸门报错。社区/旧文档示例多用 `entity_id`，**别照抄**。
3. 直接写完整 flow JSON（`{id, label:"[DS白箱] xxx", nodes:[...]}`）。
4. 提交提案（**注意：现已统一为提案闸，不再直写 NR**）：
   ```
   autoflow_deploy_raw(
     flow_json="<你的完整 flow JSON 字符串>",
     label="[DS白箱] 书房人来开灯",
     target="staging",
     require_e2e=False    # 可选；默认 False=部署沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E。True=提案带 e2e 意图，人类点「部署到 NR」时真正先跑实机验证闸（verdict≠通过即拦截）。修复 iss_8d3cffaa96
   )
   ```
   返回 `ok==true` + **`proposal_id`**（不再是 `flow_id`）。提案进入 WebUI「场景提案」面板，
   **需人类审核后点「部署到 NR」才真正写入 Node-RED**。这是与编译器 DSL 路径完全统一的放行权。
   - 若提交时带 `require_e2e=True`，该意图随提案落档；人类部署时 `deploy_proposal` 会在写 NR 前
     真正先跑一次实机验证闸（verdict≠通过即拦截部署），让「部署即验证」成为强制纪律。
5. 看返回：`ok==true` + `proposal_id` + `validation`（error/warning 列表）+ `logic`（有动作终点不可达时提示）。
   有 `validation` error/硬伤时**提案仍会照常落档**（fail-open，供人审决定），你应据此修正下一版。

> **部署前自检（仅原生手写/管理员面板可用）**：落提案前可先 `autoflow_validate_flow(flow_json)`（静态 lint，硬拦 R17/R20/R22 等结构/实体错误）或 `autoflow_simulate_flow(flow_json)`（逻辑预检，看动作终点能否在所有分支触达）；复杂 flow 还可 `autoflow_run_e2e_trace(flow_json, ...)` 在 staging 真机点燃验证。三者都不阻断落提案，仅给你 verdict 决定要不要改完再交。

- `autoflow_verify_flow(flow_json, run_gate=True, require_e2e=False, target="staging")`：**白箱质量验证·只读·绝不部署**（原生手写/管理员面板专用，黑箱不可见、调用级 `-32601` 拦截）。按需跑与 `autoflow_deploy_raw` **同源的质量闸**，但**不写 NR、不登记 catalog、不落提案**——纯检查，是「部署前最后一道质量关」。
  - **签名（真实）**：`autoflow_verify_flow(flow_json: str, run_gate: bool = True, require_e2e: bool = False, target: str = "staging") -> str`。`flow_json` 为完整 flow 对象字符串 `{"id":...,"label":...,"nodes":[...]}`，或节点数组字符串 `[{...},{...}]`（自动包成 `{nodes:[...]}`），务必是字符串；`run_gate`=是否跑 vhass staging 闸（仅含 HA 动作且能提取实体时才生效）；`require_e2e`=是否跑 e2e 实机追踪（落 staging 后回滚，默认关）；`target`="staging"。
  - **它跑**：① Schema 校验（与 deploy_raw 同源）；② 静态 Flow Linter（R13/R15/R17/R20/R22 等硬伤集，fail-open 不拦）；③ 可选 **vhass staging 闸**（仅含 HA 动作且能提取实体时，真实预演服务调用能否在 staging 跑通）；④ **结构金丝雀**（内省 NR 子流程完整性：空壳 / 仍含 `{{mustache}}` 占位 → WARN）；⑤ 可选 e2e 实机追踪（`require_e2e=True` 才跑，落 staging 后回滚）。
  - **返回统一 verdict**：`{ok:true, deployed:false, verdict:"pass"|"warn"|"block", passed, gate:{verdict,passed,layers:{vhass_staging,e2e_trace,structure_canary},notes}, validation:[...], lint:[...], lint_error_count, lint_warning_count}`。`ok` 恒为 `true`（是检查不是动作），`deployed` 显式声明未部署。
  - **与 `autoflow_validate_flow` 的区别**：`validate_flow` 只做静态 schema+lint+L2 逻辑仿真（看动作终点能否在所有分支触达）；`verify_flow` 在静态检查之上**额外跑 vhass staging 闸 + 结构金丝雀 + 可选 e2e**，给出**统一的 deploy 前质量 verdict**（pass/warn/block）。两者都只读不部署；需要「这个 flow 真能跑 / 子流程没空壳 / staging 预演 HA 动作」这类判断时用 `verify_flow`，只看静态结构/逻辑时用 `validate_flow`。
  - 典型用法：写完 flow_json 后、落 `autoflow_deploy_raw` 提案前调一次，`verdict:"block"` 先修再交，`"warn"` 酌情修，`"pass"` 可直接提案。

> **单次 HA 服务调用（不用写 flow）**：只是想让 HA 执行一次动作（如立刻开个灯验证设备）而非建自动化时，用 `autoflow_commit_ha_service(domain, service, data_json)`（原生手写/管理员面板）。它**进确认闸**——落为待批准操作，人类在 WebUI 点头后才真正调 HA，不是即时执行。别用它替代 flow：重复性逻辑仍应写成 flow/提案。

## 编写子流程（需人审注册到网关）

如果你在多个 flow 里反复用到同一段逻辑（如「取历史数据」「Bark 推送」），可把它抽象成一个**子流程**，
供编译器 / 原生手写 path 通过 `调用子流程 <dsl_name>` 复用。子流程的编写同样走**提案闸**——你提交
定义，人类在 WebUI「场景提案」面板点「部署」后才真正注册到网关（写 NR 子流程实例 + 入 subflow_registry）。
**你绝不直连 NR 注册子流程。**

调用：
```
autoflow_create_subflow(
  dsl_name="my_subflow",                  # DSL 调用名（调用子流程 <my_subflow> 引用）
  name="我的子流程",                       # 人类可读名（提案卡片标题）
  definition_json='{"id":"sf_my_subflow","nodes":[...],"in_ports":[...],"out_ports":[...]}',
  description="做什么用的"                 # 可选，仅用于提案卡片检索
)
```
- `definition_json` 必填字段：`id`（NR 子流程 id，如 `"sf_my_subflow"`）、`nodes`（节点数组）、
  `in_ports`（输入端口定义数组）、`out_ports`（输出端口定义数组）；其余可选：info/category/env 随定义透传。
- 子流程内部节点同样遵守「NR5 字段命名铁律」（见上 3b）：HA 节点用 `entityId`（camelCase），写错 `entity_id` 会在部署闸门被 R19 拦下。
- 返回 `ok==true` + `proposal_id`。**不注册、不直写 NR**——进入提案面板后，人类审核点「部署到 NR」
  才原子完成：①写 NR 子流程实例（增量 append，不整实例替换）；②登记 subflow_registry（dsl_name 为主键）。
- 注册成功后，编译器 / 原生手写即可用 `调用子流程 <dsl_name>` 引用它；同 id 已存在会拒绝覆盖（除非 force）。
- 子流程提案**不能升格为经验 skill**（那是 flow/经验类提案的路径），只能走「部署」注册到网关。

## 调用外部 API 的 flow（必须 function 节点）
**重要经验（deepseek++ 实测）**：像「调 OpenAI 兼容聊天接口 / 文生图」这类 **API 编排 flow**，
**必须用 `function` 节点** 来构造请求体，再交给 `http request`。纯靠 http request 节点很难拼出
正确 body，deepseek++ 会反复报错、需要人反复引导。标准链路：

    inject(手动触发/payload) → function(构建 JSON body) → http request(POST, ret:"obj")
           → function(从响应提取字段) → debug / 下游

**可复用的本地代理（doubao2api，无需 API Key）**：
- 聊天补全：`POST http://<NAS_IP>:9090/v1/chat/completions`，
  body `{"model":"doubao","messages":[{"role":"user","content":<文本>}]}`
- 文生图：`POST http://<NAS_IP>:9090/v1/images/generations`，
  body `{"model":"doubao-seedream-3-0-250715","prompt":<文本>,"n":1,"size":"1024x1024"}`，返回 `data[0].url`
- 完整可跑参考实现见 `docs/reference_flows/doubao_chat_imagegen.json`（用户手搓、已验证）。
- 模板骨架见 `docs/templates/api_call_openai_compat.md`。

**注意**：`http request` 的 `ret` 设 `"obj"` 让 NR 自动解析 JSON；body 写成 JSON 对象（不是字符串）。

## 逃生舱定位：编译器表达不了 → 原生手写兜

AutoFlow 两条内部路径职责互补：

- **编译器路径（DSL）**：稳、可控、自证跑通，产出干净可维护的 flow（无 spaghetti function）。但 DSL 只覆盖约 80% 家居场景——复合 AND/OR 条件、自然语言条件、间隔触发等仍是软肋。
- **原生手写路径（autoflow_deploy_raw）**：你直接写 flow JSON，网关 sanitize + 校验后**落为提案**（content.type=raw_flow），待人审后部署，几乎无约束（含允许 function 节点）。**这是编译器表达不了时的兜底逃生舱。**
- **编译器内次逃生舱（Phase 4 原生节点逃逸）**：若只想在 DSL 里嵌一小段手写 NR 节点（如 switch 复合条件），可用 `原生节点:` 原语——但它是**中风险特性，默认关闭**，需 WebUI 设置里手动开启、可随时关；且白名单永久禁 `function`/`exec`。能用原生节点解决的，不必升格到整段原生手写 flow。

**决策树（用哪个）**：
1. 能用 DSL 模板/语法表达 → 编译器路径（首选，自证跑通）。
2. 只需一小段 DSL 没覆盖的节点（如复合 switch）→ 编译器 `原生节点:`（开开关后用）。
3. 整段逻辑 DSL 都表达不了（API 编排、复杂 function 流）→ 原生手写 `autoflow_deploy_raw` 兜底（落提案待人审）。

### 原生手写成功 → 蒸馏成编译器 few-shot（反哺闭环）

原生手写跑通的 flow 是编译器最好的语料。每完成一个原生手写 flow，按下面最小流程反哺：

1. **标注边界**：这段 flow 里哪部分是「DSL 本应能表达却没表达出来的」？（如某段 switch 复合条件、某次取值+分支）
2. **提炼 DSL 等价物**：试着把那部分改写成 DSL（`分支:`/`否则:`/`原生节点:`），作为编译器成功样例。
3. **入库**：把样例加进本技能（autoflow.md）的「编译器四类易错场景范例」或 `gateway.dsl_help()` 的 `examples`（让 deepseek++ 下次直接照抄）。
4. **记录失败模式**：部署 error/warning 自动进 `raw_deploys.jsonl`，定期复盘哪些模式可下沉为 DSL 原语。

> 这条闭环是 AutoFlow 编译器持续变强的核心：原生手写探边界 → 蒸馏成 DSL 能力 → 编译器覆盖更多 → 原生手写只在真边界出现。

## 原生手写交付（给用户）
只回：`flow_id`、`validation` 里 error/warning 条数与要点、`gate` 结论（若跑了）、一句话结论。
若部署失败，回 `error` 原文——它会自动进 `raw_deploys.jsonl` 失败模式库反哺网关。

---

## 人类决策协议（遇歧义 / 不可逆分叉点请人类拍板）

向人类确认分叉点有两条通道，**优先在聊天框确认，WebUI 决策工具只用于特定场景**，避免把正在对话框协作的人类频繁切去另一个界面（割裂感）。

### 通道选择（关键）
- **✅ 聊天框直接问（首选）**：你是「交互式 chat agent」、人类就在这段对话里，且分叉点是**轻量、可逆**的歧义（如「开哪盏灯」「用什么亮度」「叫什么名」）——直接用自然语言在对话里问，人类在聊天框回复即可。**不要**为此调 `autoflow_request_decision`，也别让人跑去 WebUI 点选。
- **🌐 autoflow_request_decision → WebUI（仅限）**：(1) agent **无人值守**（headless / 任务池 worker / 定时任务），唯一能把人类选择取回的方式就是经工具；或 (2) 该选择需**持久化 / 可审计**地记进网关（供其他 agent 或人类稍后在 WebUI 复查），而非仅当前一轮对话用一下。
- 两条通道都**禁止自己假设**分叉点答案——要么聊天拿到，要么经工具拿到。黑箱 / 白箱 / 管理员身份都带这套工具（属用户工具集，不在部署刀里）。

### 工具（仅 WebUI 通道用）
- `autoflow_request_decision(question, options)` → 返回 `decision_id`。`question` 是问题，`options` 是 2~N 个候选（字符串列表）。
- `autoflow_get_decision(decision_id)` → 取回完整决策记录：`{id, question, options, status, chosen_idx, chosen_text, resolved_at, ...}`。
- `autoflow_list_decisions(status="", limit=50)` → 列出决策（默认 pending 优先）；人类选完后据此用 get_decision 取具体项。

### 闭环协议（必须照此，否则回路在 MCP 通道断开）
1. **发起**：调 `autoflow_request_decision`，拿到 `decision_id`。
2. **让出回合**：把 `decision_id` 原样告诉用户，**然后停下等人类在 WebUI 拍板**。不要在同一个回合里反复轮询 `get_decision`——人类要离开当前会话去 WebUI 点选，你轮询也拿不到结果，只是空转（这正是「死循环」错觉的来源）。
3. **取回**：人类选完（新的用户消息触发本回合）→ 调一次 `autoflow_get_decision(decision_id)`，确认 `status=="resolved"` 后读 `chosen_text`（人类的选择文本）续跑。

### 关键规则：options 必须用「真实实体名」，别用人类口语别名
`options` 应优先取自 `autoflow_resolve_entity` 返回的**真实实体 friendly_name 或 `entity_id`**，**不要**凭人类口语（如「主灯」「氛围灯」）编造选项。否则人类选了「氛围灯」后，你仍不知道要操作哪个 `entity_id`，还得回头做二次映射，多一轮往返。
- ❌ `options=["主灯","氛围灯"]`（书房里根本没有这两个实体）
- ✅ 先 `resolve_entity("书房 灯")` → 拿到 `light.philips_..._cbulb_s_2_light` / `light.mijia_..._group4_s_2_light` → 用其 friendly_name 当 options
若确无合适实体（业务真要做概念性选择），在 `question` 里显式说明「选项为概念名，选后需映射到真实实体」，避免落地时茫然。

---

# 质量自报（发现网关 / 任务缺陷）

跑任务或写 flow 时若发现**网关行为缺陷**或**任务本身编排错误**（如指定区域无该实体、实体解析发散、DSL 语义不符预期），请经此通道上报，进入人类 backlog：

```python
autoflow_report_issue(
    title="客厅窗帘任务：客厅实际无窗帘，三家选了不同实体",
    body="resolve_entity('客厅窗帘') 返回 0 候选，agent 被迫跨区 scavenge → 实体发散。根因是任务编排引用了不存在的实体。",
    task_id="hist2_duration_curtain_01",   # 可选：关联任务
    severity="high",                       # low|medium|high|critical
    category="entity",                     # defect|doc|dsl|entity|feature|other
)
```
返回 `{ok:true, issue_id:"iss_xxx"}`。**注意：这不是向用户喊话，是登记一条结构化缺陷**，人类在 WebUI/CLI 审阅后闭环（`autoflow_list_issues` / `autoflow_resolve_issue` 在管理面）。原生手写常撞到 DSL 表达不了的边界，正是这个通道最有价值的来源。

**技能指导自愈**：MCP 重连只刷新工具 schema，不会刷新你系统提示里加载的本 skill 文档。若发现本说明与网关实际工具不一致（或你怀疑 skill 已更新），调用 `autoflow_get_skill(name="autoflow")` 拉取最新版全文即可，无需重启 agent。
