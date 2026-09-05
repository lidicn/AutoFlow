# REG-M 能力矩阵回归（M1–M5，28 断言）

在 **1990 测试实例**（容器 `node-red-dev`）上跑通产品全部核心能力，每条断言都必须携带
**真实证据**（HA 真值读回 / 子流程回显 / context 时间戳 / 实测耗时），
「没报错 = 通过」一律不算数。

## 覆盖范围

| 组 | 主题 | 断言数 |
|----|------|--------|
| M1 | 读取与分支（数值真假两路、状态分支、中文字段、缺失实体、msg 变量引用） | 6 |
| M2 | 动作（开/关/亮度/开关 toggle/子流程动作/房间批量），每步读回 HA 真值 | 6 |
| M3 | 历史四件套 `af_hist_state_at / occurred / duration / aggregate` ×2 | 8 |
| M4 | 通知（demo_notify 文本 + 房间级别、Bark 基础 + 带 title） | 4 |
| M5 | 触发与时序（手动 inject、cron 真实触发、delay 实测、多触发 join） | 4 |

## 跑法

```bash
export NR_URL=http://<nas>:1990 NR_USER=<user> NR_PASS=<pass>   # 凭据只走环境变量

python tests/regression/reg_m/build_flow.py      # 1) 生成 + 结构自检
python tests/regression/reg_m/deploy.py          # 2) 增量部署（只动本 tab）

# 3) 等 ≥60s 让 cron(*/1) 至少落一次戳，再触发（只点 regm_run，别手点 M5.4 的 A/B）
python - <<'PY'
import sys; sys.path.insert(0, 'tests/regression/reg_m')
from nr_admin import NRAdmin
a = NRAdmin(); a.login(); a.inject('regm_run')
PY

# 4) 等 ~40s 取证
ssh <nas> 'docker exec node-red-dev cat /tmp/reg_m_result.txt'
```

产物：`/tmp/reg_m_result.txt`（人读总表）、`/tmp/reg_m_result.json`（结构化）。
生成的 flow JSON 与 tab 台账落在 `tests/fixtures_local/`（已 gitignore）。

## 血泪坑（改代码前先读）

1. **change 规则必须带 `t: "set"`** —— 缺了 Node-RED 会**静默跳过整条规则**，
   表现为「赋值看似执行实则没生效」。
2. **`api-call-service` 的 `data` 只能 dumps 一次** —— 对已是 `"{}"` 的字符串再
   `json.dumps` 得到 `"\"{}\""`，节点解析失败直接抛错、不产出任何消息，分支「静默不返回」。
3. **`POST /flow` 会忽略 body 里的 `id` 自行分配** —— 必须以响应返回的真实 id 登记
   `tests/fixtures_local/reg_m_tab.id`；台账失效时按 label 认领并回写。
4. **同设备用例必须串行** —— 并行开/关同一盏灯会互相打架，读回的真值不可信。
   M2.1→2.2→2.3→2.4→2.6 走串行链，链尾复原灯亮度与开关状态。
5. **`TTS_RECENT_TRIGGERS` 是纯时间戳数组 + 惰性过期** —— 队列管理器入队时才
   `filter(t => now - t < 7000)` 再 `push(now)`。所以「长度增量 > 0」是**错的**证据：
   陈旧戳被清掉时长度会不升反降（实测 2→1）。正确做法是判断「存在 ts ≥ 发出时刻的戳」。
6. **断言节点自带幂等闸门** —— 靠 `flow.regm_epoch` 保证每轮每用例只上报一次。
   否则某分支被重复触发（如 inject 既被 fan 驱动又被人工点击）会把 `join(count=28)`
   提前凑满，把真正慢一步的用例误判为「未返回」。
7. **catch 的 scope 圈不住子流程内部节点** —— 见下方已知缺陷 ①。

## 运行中暴露的产品侧问题（提交 REV 关注）

① **历史子流程缺少入参校验**：`af_hist_occurred / duration / aggregate` 在
`msg.start` 缺失时，解析节点执行 `toHAISO(null)` 抛 TypeError；而子流程内 catch 的
scope 只圈了取历史节点，圈不住解析函数节点 → **消息静默丢失，调用方永远等不到返回**。
（`af_hist_state_at` 因 `parseNaturalTime(...) || new Date()` 有兜底才幸免。）
建议：入参缺失时返回明确错误对象，而非抛异常。

② **Bark 子流程结果回显失真**：内部「构造 Bark 明文 JSON」节点确有默认标题回落
（`msg.title || 'AutoFlow'`），但「结果透传」change 的 JSONata
`{"sent":{"title":title,...}}` 引用的是**顶层 `msg.title`**（原始入参）而非实际发出值，
导致不传 title 时回显 `undefined`。不影响推送本身，属可观测性缺陷。

③ **微信通道当前不可用（环境）**：`cn_im_hub.send_message` 返回
`ret=-2 errmsg=prepare failed`。M2.5 已加**直连对照**（同 domain/service/channel 手写
调用）证明产品编译出的子流程调用与手写直连**行为完全一致**，故判定为 BLOCKED-ENV，
不是产品缺陷。通道恢复后 M2.5 会自动变为「两边都成功」。
