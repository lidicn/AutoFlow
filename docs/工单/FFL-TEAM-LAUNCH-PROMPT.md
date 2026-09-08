# FFL 团队拉起提示词（AutoFlow 竞技场 · **测试验收**专项）

> **用法**：在 DSH 里新建会话，**cwd 切到 `E:\NAS\autoflow`**，把下面「提示词正文」整段复制粘贴发送。
> 创建：2026-09-08 ｜ 版本：v2.0.11-beta
>
> ⚠️ **本团队是测试团队，不是开发团队。** 环境（竞技场设备 / vhass / API Key）由项目负责人预置完成，
> 团队的职责是**在已配好的环境上做验收、发现缺陷、写台账**。

---

## 项目负责人开工前必填（下面的 `【】` 占位符）

| 占位符 | 填什么 | 状态 |
|---|---|---|
| `【GATEWAY】` | 网关 WebUI 地址：`http://<NAS_IP>:8000` | ✅ 已填 |
| `【API_KEY】` | 测试专用 API Key（`af_pro_...`，30 天有效） | ⚠️ **不写入本文件**，由项目负责人面交，发送前替换 |
| `【AREA_NAME】` | HA 里书房区域名：`书房` | ✅ 已填 |
| `【DEVICES】` | 已同步的真实设备清单（11 个，见下） | ✅ 已填 |

> 填完把这几行删掉，正文里就不该再有 `【】`。

---

## 提示词正文（复制从这里开始 ↓）

请创建 FFL（FlowForge Labs）**测试团队**，对 AutoFlow 的「书房竞技场」做验收测试。

## 团队角色（全部是测试角色）

1. **测试主管（PM）**- 使用 `pm-chat` 模型 —— 拆解验收用例、分配、汇总缺陷、判定优先级
2. **tester-1**（功能验收）- 使用 `tester-chat` 模型 —— 正向链路
3. **tester-2**（边界与负向）- 使用 `tester-chat` 模型 —— 异常输入、判重、门槛、并发
4. **reviewer**（缺陷复核）- 使用 `reviewer-chat` 模型 —— 复核缺陷是否有效、是否重复、可复现性

## ⛔ 第一条红线：不许改代码

**本团队不得修改 `src/` 下的任何网关代码，不得提交任何代码改动。**

- 发现疑似 bug → **写进 `docs/04_test/findings-ledger.md`**，附「现象 / 复现步骤 / 期望 / 实际 / 原始响应 JSON」
- 由项目负责人判断是否修、怎么修。**你们的任务是把它暴露出来，不是把它修好。**
- 网关是控制物理设备的生产系统，任何未经评审的代码改动都可能造成真实损害。
- 唯一允许写的文件：`docs/04_test/findings-ledger.md` 和 `tests/` 下的**新增验收脚本**（不改已有脚本）。

## 项目信息

- 项目：AutoFlow（Home Assistant 智能家居的 DSL 网关），v2.0.11-beta，分支只有 `main`
- 目录：`E:\NAS\autoflow`
- 架构文档（唯一权威）：`docs/02_architecture/ARCHITECTURE.md`（重点看 §18.2 竞技场）
- 缺陷台账：`docs/04_test/findings-ledger.md`
- 网关地址：`【GATEWAY】` → `http://<NAS_IP>:8000`
- 认证：所有请求带 `Authorization: Bearer 【API_KEY】`
  （key 由项目负责人在会话里单独提供，**不要写进任何文件或 commit**）

## 验收场地：书房竞技场（环境已由项目负责人预置好）

- 分区 id：`study_room`（书房竞技场）
- 区域名：`【AREA_NAME】` → **`书房`**
- 阈值：`phase2_threshold=20`、`creativity_threshold=0.3`
- **已同步的真实设备清单**（11 个，全部来自真实 HA，均带 `synced_from_ha: true`，
  占位设备已全部移除）：

```
light.philips_cn_249518489_rwread_s_2_light                 书房台灯（light）
light.yeelink_cn_555003624_lamp22_s_2                       米家智能显示器挂灯1S（light）
light.xiaomi_cn_822413342_lamp35_s_2_light                  米家桌面学习灯（light）
switch.lemesh_cn_1088333045_sw0a04_on_p_2_1                 书房电脑（switch）
climate.lumi_cn_84159632_v2                                 书房空调（climate）
binary_sensor.xiaomi_cn_blt_3_1hsett9ug4k01_03_occupancy_status_p_2_1078   小米人在传感器
binary_sensor.lumi_cn_lumi_158d0001a2520d_aq2_motion_state_p_2_1           书房人体传感器
binary_sensor.isa_cn_blt_3_145qeam4s5o00_dw2hl_contact_state_p_2_2         书房门窗传感器
sensor.duka_cn_blt_3_1orsfvt24cc01_th2_temperature_p_2_1001                温度（°C）
sensor.duka_cn_blt_3_1orsfvt24cc01_th2_relative_humidity_p_2_1008          湿度（%）
sensor.ainice_cn_1008528932_rd_status_p_5_2                                光照度（lux）
```

- **选型说明**（出题时参考）：已刻意排除 `unavailable` 设备、门锁与摄像头；
  覆盖 6 个 domain（light / switch / climate / binary_sensor / sensor），足够写出
  「人到灯亮」「光照不足补光」「离家关空调」「湿度异常提醒」这类有意义的自动化。
  ★ `switch.lemesh_cn_1088333045_sw0a04_on_p_2_1` 是**真实电脑电源**，
  出题允许引用它，但验收只在 vhass 孪生里跑，**绝不会真的开关你的电脑**。

- 相关 API：
  - `GET  /api/arena/arenas`
  - `GET  /api/arena/arenas/{arena_id}`、`/tasks`
  - `POST /api/arena/arenas/{arena_id}/propose`（出题）
  - `POST /api/arena/arenas/{arena_id}/submit`（提交 flow）
  - `GET  /api/arena/arenas/{arena_id}/leaderboard`、`/api/arena/stats`

### 环境前提（不用你们做，但要知道）

这些是项目负责人已经做好的，用来理解「为什么设备是真的、但验收不碰物理设备」：

```
真实 HA 书房设备 ──sync_devices──▶ 分区 devices(synced_from_ha=true)
                                      └─▶ _rewrite_seed ──▶ vhass 虚拟孪生
agent 用真实 entity_id 写 DSL ──▶ 在 vhass 孪生上验收 ──▶ 排行榜
```

**设备 ID 是真的**（所以产出的 flow 将来能直接部署），
**验收执行在孪生上**（所以测试过程不驱动任何物理设备）。

## 任务清单

### T0（测试主管）—— 核对环境（只读，30 分钟内完成）

确认环境确实配好了，没配好就立刻上报阻塞，**不要自己动手配**：

1. `GET /api/arena/arenas` → `study_room` 存在
2. `GET /api/arena/arenas/study_room` → devices 数量与上面清单一致，
   且每一项 `synced_from_ha == true`（**出现 false 就是没同步成功，立即上报**）
3. 抽查 3 个 entity_id 在真实 HA 里确实存在（比对清单）
4. `GET /api/arena/stats` 返回正常

**产出**：一段「环境核对结论」，写进台账。

### T1（tester-1）—— 正向链路验收

1. 用 3 个不同 `agent_id` 各 `propose` 一道题（实体集取自上面的真实清单，跨至少 2 个 domain）
2. 各自 `submit` 一份 DSL，走完 vhass 验收
3. 断言：
   - 3 条全部出现在 `leaderboard`
   - `stats` 计数正确（题目数 / 提交数 / 通过数）
   - 每个 flow 引用的 entity_id **100% 来自真实清单**（脚本断言，出现清单外实体即判失败）
4. ★ 抽 1 个产出的 flow 做「真实可用性**静态**核对」：实体都存在、动作合法、能被编译
   ——**只做静态核对，不实际部署**

### T2（tester-2）—— 边界与负向

- 重复出题（实体重叠 >60%）→ 应被判重拒绝，且**返回明确原因**（不能只说失败）
- 文本高度相似（>85%）→ 应被判重拒绝
- 创意分低于 `creativity_threshold=0.3` 的提交 → 应被拒绝
- 异常输入：空 DSL、非法 entity_id、超长描述、缺字段、重复 submit 同一 task
- 并发：多个 agent 同时 propose 同一批实体，不得产生重复题目

**每一条都要贴原始响应 JSON**。

### T3（reviewer）—— 缺陷复核与台账

- 复核 T1/T2 报出的每条疑似缺陷：可复现？是真缺陷还是测试姿势不对？是否与台账已有条目重复？
- 台账里已有的（B1–B19）**不要重复报**
- 定级（P0/P1/P2）+ 给出最小复现步骤
- 结论写进 `docs/04_test/findings-ledger.md`，**不新建报告文件**

## 工作方式

1. 测试主管先读 `ARCHITECTURE.md` §18.2，再拆用例分配
2. T0 先做，没通过就停，上报阻塞
3. 每天/每阶段结束，测试主管汇总一次「发现 / 阻塞 / 下一步」
4. **不写业务代码、不改网关代码、不 push**

## 其余红线

1. **不许改 `src/` 代码**（见上，第一条红线）
2. **不许部署**：全程禁止调用部署类接口、禁止 `allow_prod`、禁止触碰真实 NR 实例
   （`1880` / `1990`）。验收只在 vhass 孪生里跑。
3. **不许修那 94 个历史测试红**：全量基线是 `95 failed / 1461 passed`，这些是历史遗留的
   测试漂移，**不是本次引入的，也不要去修**。只保证自己新增的验收脚本能跑通。
   （跑全量必须加 `--basetemp=E:/NAS/autoflow/tmp/pytest_basetemp`，否则宿主安全策略会
   拦截临时目录批量删除、把 summary 吞掉。）
4. **不许写真内网 IP**：版本库里出现 `192.168.x.x` / `100.112.x.x` 会让发布门禁
   `tests/test_no_secrets.py` 变红。一律用占位符 `<NAS_IP>` / `<SHARE_HOST>` / `<NR_HOST>`
   （对照表见 `docs/README.md`）。贴响应 JSON 前先脱敏。
5. **commit 只显式 add 自己的文件**，禁止 `git add -A`
6. pytest 用：`C:/Users/lidicn/.workbuddy/binaries/python/envs/default/Scripts/python.exe`

请测试主管先做 T0 环境核对，然后开始 T1 / T2 并行。

（复制到这里结束 ↑）

---

## 备注（给项目负责人，不用发给团队）

- **2026-09-08 环境已配置完成**（均经实测验证）：
  - NAS 上 FFL 留下的弃用副本 `autoflow-v2` 容器 + 目录已清理（数据已备份到
    `D:\Documents\HAOS\AutoFlow_archive\2026-09-08\nas_cleanup\`），
    同时清掉 63 个历史备份 tar.gz（162M，规范 §9 点名的堆积），**生产只剩 `autoflow_gateway`**。
  - 测试 API Key 已签发：key_id `akid_bd0ba5edc280452e`，agent_id `ffl-arena-tester`，
    权限 `[read, deploy]`（**无 modify**，不能 deploy-raw/rollback），2026-10-08 过期。
    明文只在签发时返回一次，已面交用户，**未写入任何仓库文件**。
  - 书房 11 个真实设备已 `sync_devices` 进 `study_room`（11/11 全中，无静默跳过），
    8 个 `DEFAULT_ARENAS` 占位设备已全部移除，`study_room_seed.json` 已重写含真实 entity_id。
- **2026-09-08 角色纠偏**：初稿把 T1（`webui.py` 20 处 `gw.*` 改 `asyncio.to_thread`）和
  T2（gateway `__truncated__` 处理）也派给了团队。这是**改 `src/` 网关代码**，
  等于把生产网关交给外部团队 —— 已全部撤回，改由项目负责人自己做或另行安排。
  **FFL 现在的角色是测试员：环境我配好，他们只做验收。**
- **为什么设备必须真实**：分区自带的 8 个（`switch.computer` 等）是
  `arena.py::DEFAULT_ARENAS` 占位种子，真实 HA 里不存在 → 对它们写的 flow 是「假可用」
  （编译过、孪生跑通、部署即失效）。陷阱：`sync_devices` 对不存在的 entity_id **静默跳过**，
  必须逐个核对 `added_ids`。
- **合规发现（待办，未动）**：本地 `docker-compose.yml` 的端口映射写的是 `"8000:8000"`（= 0.0.0.0），
  违反《NAS 开发规范》§6.1「必须绑定 192.168.2.200」。线上实际已绑对（`192.168.2.200:8000->8000`），
  说明**线上 compose 与仓库不一致**，需要对齐后一并修。改 compose 会重建容器（规范 §2.7 警告
  容器 churn 触发 trim 死锁），需选低峰期并走部署 skill。
- **SSH 通道**：Git Bash 自带 ssh 读 `~/.ssh/known_hosts` 会报 Permission denied，
  系统自带 `C:\Windows\System32\OpenSSH\ssh.exe` 在非交互 shell 下无输出；
  **可用的是便携版** `C:\Users\lidicn\.ssh\openssh\OpenSSH-Win64\ssh.exe`
  + `-i C:\Users\lidicn\.ssh\id_ed25519 -o StrictHostKeyChecking=no`。
