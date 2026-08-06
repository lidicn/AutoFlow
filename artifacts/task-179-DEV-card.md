# DEV 交付卡 · task #179

**分支** `dev/task-179-cleanup` → 已推送
**提交** `fef03cd` · `c1838f2` · `f431a6d`
**开 PR** https://github.com/lidicn/AutoFlow/pull/new/dev/task-179-cleanup
（本机无 `gh` CLI，PR 正文见 `artifacts/task-179-PR-body.md`，复制即可）

---

## A 块 · 杂项清理

| 项 | 内容 | 状态 |
|----|------|------|
| A1 | `nr_client.py` 统一矛盾注释 | ⚠️ **需重发技能**（见下） |
| A2 | `webui.py` 删除未用 import | ✅ `fef03cd` |
| A3 | `_config_fields_for_spec` 补扫 `extract` / `nr_assemble` 的 `<ENV>` | ✅ `fef03cd` |
| — | `.gitignore` 排除 `tests/fixtures_local/` | ✅ `c1838f2` |

> ⚠️ **A1 单列**：`nr_client.py` 位于用户级技能目录
> `~/.workbuddy/skills/node-red-Kai-Dai/scripts/`，**不在本仓库内**，
> 改动无法随本 PR 交付，需单独重发技能包。

## C 块 · REG-M 能力矩阵（#157）

新增 `tests/regression/reg_m/`（`f431a6d`）：

| 文件 | 职责 |
|------|------|
| `build_flow.py` | 生成 124 节点的 REG-M tab，内置 8 项结构自检 |
| `nr_admin.py` | 表单鉴权的极简 NR admin 客户端，凭据只走环境变量 |
| `deploy.py` | 只 PUT 目标单 tab，部署前后校验 tab 数不减 / 节点全部落盘 |
| `README.md` | 7 个坑 + 3 个产品侧发现 |

### 实测结果：**28 / 28 全绿**（连跑两轮一致）

环境：1990 测试实例（容器 `node-red-dev`），tab `9b5acc3441f8f201`，2026-08-06 13:41。

| 组 | 结果 | 证据类型 |
|----|------|----------|
| M1 读取与分支 ×6 | 6/6 | HA 真值（温度 34.45°C / 灯 on）+ catch 真实错误文本 |
| M2 动作 ×6 | 6/6 | 每步 1.5–2s 后**读回 HA 真值**（on/off/brightness=199/toggle 翻转/双灯） |
| M3 历史四件套 ×8 | 8/8 | 子流程真实返回体（`total_seconds:15176`、`mean:29.34`、`max:34.45`） |
| M4 通知 ×4 | 4/4 | TTS context 时间戳命中 + Bark HTTP 200 回显 |
| M5 触发时序 ×4 | 4/4 | cron 真实触发间隔 16.5s / delay 实测 2147ms / join 2-of-2 |

**部署安全性**：tab 数始终 **37 → 37**，总节点 1239 → 1289，其余 36 个 tab 零改动。

### 本轮修掉的 5 类测试设计缺陷

1. `change` 规则缺 `t:"set"` → Node-RED **静默跳过整条规则**（M1.1/1.3/1.4 首轮假失败）
2. `api-call-service` 的 `data` 被 **双重 `json.dumps`** → 节点抛错不产出消息（M2.1/2.2/2.4 静默不返回）
3. M3 未按契约注入 `msg.start/end/state/metric` → 子流程内部抛错且 catch 圈不住（M3.3–3.8 静默丢失）
4. M4 用 Bark 顶替 `demo_notify` → **测错对象**；已改为 `link out → b595563939283231`（TTS 队列入口）
5. M5.4 的 inject 既被 fan 驱动又被人工点击 → 重复上报把 `join(28)` 提前凑满，误伤慢一步的 M2.6

第 5 点的根治手段：**每个断言节点自带 `flow.regm_epoch` 幂等闸门**，同一轮每用例只上报一次。

### 一个反直觉的取证坑（值得单独记）

`global.TTS_RECENT_TRIGGERS` 是**纯时间戳数组 + 惰性过期**——队列管理器 v3 只在入队时执行
`filter(t => now - t < 7000)` 再 `push(now)`。所以「数组长度增量 > 0」是**错误证据**：
上一轮遗留的陈旧戳被清掉时，长度会不升反降（实测 **2 → 1**，M4.1 因此假失败）。
正确做法是判断「数组中存在 `ts ≥ 本条消息发出时刻` 的戳」，实测证据：
`登记了 1 个 ts≥发出时刻的触发戳(窗口内共1个, 发出前2个), 熔断=false`。

## 回归检查结论（2026-08-06 复跑）

**结论：task #179 引入的改动零回归。** 全量收集 1026 个测试全部 import 成功；
针对性子集跑完后，发现 6 个失败，**全部是 origin/main 上已存在的陈旧/环境缺陷，与 #179 无关**。

| 失败测试 | 根因 | 是否 #179 引入 |
|----------|------|----------------|
| `test_subflow_webui::test_list_returns_seeded_managed` | 期望 seed 9 条，注册表实有 8 条（前序任务改了 `SUBFLOWS` 没同步测试） | ❌ 预存在（`api_specs.py`/`subflows.py`/`test_subflow_webui.py` 均不在本分支 diff 中） |
| `test_subflow_webui::test_import_adds_entry` | 同上（计数依赖） | ❌ 预存在 |
| `test_api_capability::test_compile_emits_http_and_reply_extract` | `demo_notify` 未注册进 `API_SPECS` → 编译器拒绝 `调用子流程: demo_notify` | ❌ 预存在 |
| `test_api_capability::test_staging_gate_passes_chat` | 同上 | ❌ 预存在 |
| `test_compiler_invariants::test_harness_no_false_positive` | 同上（14/102 case 用 `demo_notify`） | ❌ 预存在 |
| `test_compiler_invariants::test_invariants_matrix` | 同上 | ❌ 预存在 |

**死证**：`demo_notify` 在 `origin/main` 与 `HEAD` 的 `api_specs.py` 中均不存在；
上述 4 个测试文件与 `dsl_engine.py` 相对 main 全部 `UNCHANGED`。
即 main 上同样红，#179 未触碰任何相关代码路径。

**根因（供 REV / 后续任务）**：`demo_notify` 注册在 `data/subflows/subflows.json`（WebUI 托管子流程数据），
但**没进 `API_SPECS`**（编译器 `SUBFLOWS` 注册表的唯一来源）。两处注册表不一致。
建议另开任务把 `demo_notify` 补进 `API_SPECS`（或让编译器同时认 `subflows.json`），并修掉 subflow 计数测试的陈旧期望。

## 待办

- [ ] 开 PR（本机无 `gh`，需手动）
- [ ] REV 审
- [ ] 合并后 SSH 同步活树 + `docker compose restart autoflow_gateway`（**A3 必须重启**）
- [ ] A1 单独重发技能包
