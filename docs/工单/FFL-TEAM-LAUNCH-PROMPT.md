# FFL 团队拉起提示词（AutoFlow P2：竞技场验收 + 工单 002 收尾）

> **用法**：在 DSH 里新建会话，**cwd 切到 `E:\NAS\autoflow`**，把下面「提示词正文」整段复制粘贴发送即可。
> 一句话拉起团队，模型分配写在正文里，不需要预先建 `team.json`（DSH 会自建）。
> 创建时间：2026-09-08 ｜ 项目版本：v2.0.11-beta

---

## 提示词正文（复制从这里开始 ↓）

请创建 FFL（FlowForge Labs）开发团队，开始 AutoFlow 项目 P2 阶段开发。

## 团队角色

1. **PM**（项目经理）- 使用 `pm-chat` 模型
2. **backend-dev-1**（后端开发 · 异步改造）- 使用 `backend-dev-chat` 模型
3. **backend-dev-2**（后端开发 · 网关健壮性）- 使用 `backend-dev-chat` 模型
4. **tester**（测试员 · 竞技场验收）- 使用 `tester-chat` 模型
5. **reviewer**（审查员）- 使用 `reviewer-chat` 模型

## 项目信息

- 项目名称：AutoFlow（Home Assistant 智能家居的 DSL 网关）
- 项目目录：`E:\NAS\autoflow`
- 版本：v2.0.11-beta ｜ 分支：**只有 `main`**
- 架构文档（唯一权威）：`E:\NAS\autoflow\docs\02_architecture\ARCHITECTURE.md`
- 待办工单：`E:\NAS\autoflow\docs\工单\TICKET-20260906-002-性能与前端安全修复回传主仓库.md`
- 缺陷台账：`E:\NAS\autoflow\docs\04_test\findings-ledger.md`

## 本阶段目标

让 **书房竞技场** 成为可验收的场地：**先把书房真实 HA 设备同步进竞技场和 vhass 孪生**，
再跑 agent 出题 → 写 DSL → 提交 → 验收 → 上榜全链路；同时清掉工单 002 里剩余的两项真实缺陷。

★ 目标是产出**真实能用的 flow**——设备必须来自真实 HA，验收执行在孪生上。

## 验收场地：书房竞技场（**真实设备驱动**）

- 分区 id：**`study_room`**，名称「书房竞技场」
- 阈值：`phase2_threshold=20`、`creativity_threshold=0.3`
- 相关 API：`/api/arena/arenas`、`/api/arena/arenas/{id}/propose`、`/api/arena/arenas/{id}/submit`、
  `/api/arena/arenas/{id}/leaderboard`、`/api/arena/stats`、
  `/api/arena/ha_areas`、`/api/arena/ha_devices`、`/api/arena/arenas/{id}/sync_devices`

### ⚠️ 关键：设备必须来自真实 HA，不能用默认占位设备

分区出厂自带的 8 个设备（`switch.computer`、`light.desk_lamp`、`light.monitor_lamp`、
`climate.study_ac`、`cover.study_curtain`、`sensor.study_temperature`、
`binary_sensor.study_motion`、`input_boolean.focus_mode`）是 **`DEFAULT_ARENAS` 里的占位种子**，
**在真实 HA 里大概率并不存在**。对着它们出的题、写的 flow 是「假可用」——
编译能过、vhass 能跑，但拿到真实环境一部署就因为实体不存在而失效。

**因此验收链路必须是：**

```
真实 HA 书房设备  ──sync_devices──▶  竞技场分区 devices（synced_from_ha=true）
                                        │
                                        └─▶ _rewrite_seed() 重写 vhass 种子 ──▶ _reset_vhass()
                                                                                    │
agent 用真实 entity_id 写 DSL ──▶ vhass 孪生上验收 ──▶ 排行榜
                                                        │
                                        （验收执行在孪生，不碰物理设备）
```

这样产出的 flow **携带真实 entity_id，经人工批准后可在真实环境直接部署**；
而验收过程跑在 vhass 孪生上，**不触碰任何物理设备**。

## 任务清单

### T0（tester，P0 前置）—— 书房真实设备同步进竞技场

**这个任务不完成，后面的验收全是假的。** 先做。

1. `GET /api/arena/ha_areas` 拿到区域列表，**确认「书房」在 HA 里的准确区域名**
   （可能叫「书房」/「Study」/别的，以实际返回为准）
2. `GET /api/arena/ha_devices?area=<书房区域名>` 列出书房真实设备
3. 挑选 **6–10 个**进竞技场，挑选原则：
   - 覆盖多个 domain（`light` / `switch` / `climate` / `cover` / `sensor` / `binary_sensor`），
     否则 agent 写不出有意义的自动化
   - 优先选状态可读、动作可逆的（灯、开关、窗帘），**避开强副作用设备**（门锁、燃气阀、摄像头）
4. `POST /api/arena/arenas/study_room/sync_devices`，body `{"entity_ids": [...]}`
5. **校验**（缺一不可）：
   - 响应 `added_ids` 与提交清单一致（不存在的 entity_id 会被静默跳过，必须逐个核对）
   - 分区 devices 里这些项带 `synced_from_ha: true` 和 `synced_at`
   - vhass 种子文件已重写（`_rewrite_seed`），新实体在孪生里能被读到
6. 同步后**清空/替换掉那 8 个占位设备**，避免 agent 拿到假 entity_id

**验收**：输出一份「书房竞技场真实设备清单」（entity_id + friendly_name + domain），
写进 `docs/04_test/findings-ledger.md`，后续所有出题都只能用这批 entity_id。

### T1（backend-dev-1，P1 性能）—— webui.py 同步调用阻塞事件循环

- **现象**：`src/autoflow_gateway/webui.py` 里仍有 **20 处** 同步 `gw.*` 调用未包 `asyncio.to_thread`
  （已有 46 处包了）。单个慢请求（e2e 闸门、部署）会卡住所有并发请求，**包括健康检查**，
  容器编排会因此误判实例死亡。
- **做法**：把剩余同步 `gw.*` 调用改为 `await asyncio.to_thread(gw.func, ...)`，与现有 46 处风格一致。
- **验收**：
  1. `grep -n "gw\.\w*(" src/autoflow_gateway/webui.py | grep -v asyncio.to_thread` 结果显著下降（目标 0）
  2. 新增或补充单测：慢 `gw` 调用期间 `/api/health` 仍能在超时前返回 200
  3. `python -m pytest tests/test_webui.py -q` 不新增失败

### T2（backend-dev-2，P1 健壮性）—— 截断 JSON 混入实体解析

- **现象**：gateway 实体解析阶段没有跳过带 `__truncated__` 标记的截断 JSON，
  截断片段会被当成合法结构参与编译/校验，产出错误的 DSL 判定。
  （目前 `__truncated__` 只在 `debug_bridge.py` 有处理，gateway 侧缺失。）
- **做法**：实体解析时跳过 `__truncated__` 标记的截断 JSON；`schema_block` 阶段返回增加
  `would_block_on_schema: True` 标记，让调用方能区分「实体不存在」和「数据被截断」。
- **验收**：
  1. 构造含 `{"a":1,"__truncated__":true}` 的实体快照，断言该实体被跳过而不被当作合法
  2. `schema_block` 响应含 `would_block_on_schema: True`
  3. 补一份回归测试

### T3（tester，依赖 T0）—— 书房竞技场端到端验收

- **前置**：T0 已完成，竞技场里是真实 HA 同步来的设备。
- **做法**：在 `study_room` 分区完整走一轮：
  `propose`（出题，实体集取自 T0 清单）→ 三层去重（实体重叠 >60% / 文本相似 >85% /
  LLM 判定 0.6–0.85）→ `submit`（提交 DSL）→ vhass 验收 → `leaderboard` 上榜 → `stats` 计数正确。
- **验收**：
  1. 至少 3 个不同 agent_id 各提交 1 个 flow，全部出现在排行榜
  2. **每个 flow 引用的 entity_id 100% 来自 T0 真实设备清单**（脚本断言，不允许出现占位设备）
  3. 故意重复出题（实体重叠 >60%）被判重拒绝，返回明确原因
  4. 创意分低于 `creativity_threshold=0.3` 的提交被拒
  5. 全过程 **0 次部署到真实 NR**、**0 次触碰物理设备**（见下方红线）
  6. 验收脚本落在 `tests/`，可重复执行
  7. ★ 抽 1 个产出的 flow 做「真实可用性抽查」：确认它经人工批准后能直接在真实环境部署
     （只做静态核对：实体存在、动作合法；**不实际部署**）

### T4（reviewer）—— 审查

- 审查 T1/T2/T3 的 diff：是否引入新的阻塞调用、是否守住红线、是否有测试覆盖。
- 审查结论**写进 `docs/04_test/findings-ledger.md`**，**不要新建报告文件**。

## 工作方式

1. PM 先读 `ARCHITECTURE.md` §18（项目阶段与当前重点）和工单 002，再细化任务分配
2. **顺序**：T0 必须最先完成（阻塞 T3）；T1 / T2 可与 T0 并行
3. 每个任务独立 commit，message 带任务号（如 `[T0] ...`、`[T1] ...`）
4. 遇到阻塞立即上报，不要卡住
5. **不要 push** —— 提交留在本地，由项目负责人签收后统一推送

## 关键约束（红线，违反即打回）

1. **prod NR 只读铁律**：`<NAS_IP>:1880` / `:1990` 一律只读。任何写操作需显式 `allow_prod`
   且 PM 同意。**禁止触碰用户手工搭建的 flow**。
   - 竞技场的**设备**必须是真实 HA 同步来的（见 T0），否则产出的 flow 是假的
   - 竞技场的**验收执行**一律跑在 vhass 孪生上，**不部署到真实 NR、不驱动物理设备**
2. **测试基线**：当前全量 `95 failed / 1461 passed`，这 95 个是**历史遗留的测试漂移**（测试写死旧 API 名），
   **不是本次引入的，也【不要】去修**。只保证：自己改动相关的测试绿、且不新增红。
   判断方式：跑全量必须加
   `--basetemp=E:/NAS/autoflow/tmp/pytest_basetemp`（否则宿主安全策略会拦截临时目录批量删除、
   吞掉 summary）。
3. **禁止写真内网 IP**：版本库内出现 `192.168.x.x` / `100.112.x.x` 会让发布门禁
   `tests/test_no_secrets.py` 变红。文档与测试里一律用占位符
   `<NAS_IP>` / `<SHARE_HOST>` / `<NR_HOST>`（对照表见 `docs/README.md`）。
4. **代码唯一真相源** = `src/autoflow_gateway/gateway.py`。不要改仓库里任何 `.bak*` 文件。
5. **不要新建架构文档**。改了代码结构就同步 `docs/02_architecture/ARCHITECTURE.md`。
6. **commit 只显式 add 改动文件**，禁止 `git add -A`（`.gitignore` 已排除 `outputs/`、`tmp/`、
   `gen_r21_flows.py`，但历史上多次被误夹带）。
7. pytest 用这个解释器：`C:/Users/lidicn/.workbuddy/binaries/python/envs/default/Scripts/python.exe`

请 PM 先读文档、细化任务，然后开始 T1/T2 并行推进。

（复制到这里结束 ↑）

---

## 备注（给项目负责人，不用发给团队）

- **为什么必须是真实设备**（2026-09-08 项目负责人的修正）：初稿曾让团队直接用分区自带的 8 个
  占位设备（`switch.computer` 等）做验收。这 8 个是 `arena.py::DEFAULT_ARENAS` 的种子数据，
  真实 HA 里大概率不存在 —— 对它们写的 flow 编译能过、vhass 能跑，但一部署到真实环境就因
  实体不存在而失效，等于「假可用」。正确链路是
  `真实 HA → sync_devices → 分区 devices(synced_from_ha=true) → _rewrite_seed → vhass 孪生`。

- **已知风险**：DSH 的 `subagent-model-selection.allowedModels` 目前只列了 `dsh-power-chat` 和
  `pm-chat`，但 FFL v1 团队当年确实用 `backend-dev-chat` / `tester-chat` 等跑通过。
  若拉起时报模型不允许，先照跑，报错再放宽 `settings.yaml`。
- **测试债（94 个历史红）由项目负责人自己清**，不进团队任务 —— 这些是「测试写死旧 API 名」的漂移
  （如 `Gateway._ensure_history_subflow_for` 在 `5e1c66d` 被移除、迁到 `subflows.ensure_history_subflow()`），
  需要逐条精确判断，交给团队容易误改成掩盖问题的假绿。
- **TICKET-002 第 2 项（M7 令牌明文打印）已于 2026-09-08 由项目负责人修复**（commit `d6134c0`），
  不需要团队再做。
