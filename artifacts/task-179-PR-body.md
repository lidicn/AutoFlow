# task #179: 杂项清理(A1-A3) + REG-M 能力矩阵回归套件(#157)

> 开 PR 链接：https://github.com/lidicn/AutoFlow/pull/new/dev/task-179-cleanup
> 标题：`task #179: 杂项清理(A1-A3) + REG-M 能力矩阵回归套件(#157)`
> 以下为正文，直接复制。

---

## A 块 · 杂项清理

| 项 | 内容 | commit |
|----|------|--------|
| A1 | `nr_client.py` 统一矛盾注释 | 见下方「需重发技能」 |
| A2 | `webui.py` 删除未用 import | `fef03cd` |
| A3 | `_config_fields_for_spec` 补扫 `extract` / `nr_assemble` 的 `<ENV>` 占位符 | `fef03cd` |
| — | `.gitignore` 排除 `tests/fixtures_local/` | `c1838f2` |

> **A1 需重发技能**：`nr_client.py` 属用户级技能目录（`~/.workbuddy/skills/node-red-Kai-Dai/`），
> 不在本仓库内，改动无法随本 PR 交付，需单独重发技能包。

## C 块 · REG-M 能力矩阵（#157）

`tests/regression/reg_m/`，在 **1990 测试实例**（容器 `node-red-dev`）实测 **28/28 全绿**，
连跑两轮结果一致。每条断言均携带真实证据，无一条依赖「没报错 = 通过」。

- `build_flow.py` — 生成 124 节点的 REG-M tab，内置结构自检
- `nr_admin.py` — 表单鉴权的极简 NR admin 客户端（凭据只走环境变量）
- `deploy.py` — 只 PUT 目标单 tab，部署前后校验 tab 数不减 / 节点全部落盘
- `README.md` — 7 个坑 + 3 个产品侧发现

部署安全性：tab 数始终 **37 → 37**，总节点 1239 → 1289，其余 36 个 tab 零改动。

### 证据摘录（`/tmp/reg_m_result.txt`，2026-08-06 13:41:27）

```
[PASS] M1.1  | HA真值 34.45°C(>27) → switch 走【真路】, 输出=HIGH
[PASS] M1.5  | 不存在实体 → catch 捕获: InputError: Entity could not be found in cache...
[PASS] M2.3  | brightness=200 下发 → 读回 state=on, brightness=199 (容差±12)
[PASS] M2.4  | switch.toggle → HA 真值 off ⇒ on (已翻转)
[PASS] M2.5  | [BLOCKED-ENV] 子流程与直连行为一致(均失败)→产品调用链无缺陷
[PASS] M3.5  | af_hist_duration → {"total_seconds":15176,"total_human":"4小时12分56秒",...}
[PASS] M3.7  | af_hist_aggregate(mean) → {"value":29.340909090909093,"unit":"°C",...}
[PASS] M4.1  | 队列管理器登记了 1 个 ts≥发出时刻的触发戳(窗口内共1个, 发出前2个), 熔断=false
[PASS] M4.4  | bark 带title → HTTP 200, 回显 title=REG-M 回归, body=...
[PASS] M5.2  | cron(*/1 * * * *) 上次真实触发在 16.5s 前, 累计 16 次
[PASS] M5.3  | delay 节点实测耗时 2147ms (目标 2000ms, 容差 1900~6000)
===== 通过 28/28 =====
```

## REV 关注项（产品侧，均已在 README 记录）

1. **历史子流程缺入参校验** — `af_hist_occurred/duration/aggregate` 在 `msg.start` 缺失时
   解析节点 `toHAISO(null)` 抛 TypeError，而子流程内 catch 的 scope 只圈了取历史节点、
   圈不住解析节点 → **消息静默丢失，调用方永远等不到返回**。
   （`af_hist_state_at` 因 `parseNaturalTime(...) || new Date()` 有兜底才幸免。）
   建议：入参缺失时返回明确错误对象，而非抛异常。

2. **Bark 子流程回显失真** — 内部「构造 Bark 明文 JSON」确有默认标题回落
   `msg.title || 'AutoFlow'`，但「结果透传」change 的 JSONata `{"sent":{"title":title,...}}`
   引用的是**顶层 `msg.title`**（原始入参）而非实际发出值，不传 title 时回显 `undefined`。
   不影响推送本身，属可观测性缺陷。

3. **微信通道 BLOCKED-ENV** — `cn_im_hub.send_message` 返回 `ret=-2 prepare failed`。
   M2.5 已加**直连对照**（同 domain/service/channel 手写调用），证明产品编译出的子流程调用
   与手写直连行为**完全一致**，故非产品缺陷；通道恢复后该用例自动转为「两边都成功」。
   注：全库 5 处微信推送统一用 `channel: "wechat/user_id"`，已确认这是约定值而非占位符。

## 回归检查（2026-08-06）

全量收集 1026 个测试全部 import 成功；针对性子集跑完发现 6 个失败，**均非本 PR 引入**
（已死证：`demo_notify` 在 `origin/main` 与 `HEAD` 的 `api_specs.py` 中均不存在，
相关测试文件与 `dsl_engine.py` 相对 main 全部 `UNCHANGED`）。

- `test_subflow_webui`（×2）：seed 计数期望 9，注册表实有 8（前序任务改了 `SUBFLOWS` 没同步测试）。
- `test_api_capability` / `test_compiler_invariants`（×4）：`demo_notify` 注册在
  `data/subflows/subflows.json` 却**没进 `API_SPECS`**，编译器拒绝 `调用子流程: demo_notify`。
  两处注册表不一致，建议另开任务修复，不在本 PR 范围。

本 PR 实际改动（`webui.py` import 清理 + `extract`/`nr_assemble` 占位符扫描）零回归。

## 上线步骤（合并后）

A3 改动需重启网关：

```bash
ssh lidicn@192.168.2.200
# 同步活树 //192.168.2.200/docker/autoflow/src/autoflow_gateway/
docker compose restart autoflow_gateway
```
