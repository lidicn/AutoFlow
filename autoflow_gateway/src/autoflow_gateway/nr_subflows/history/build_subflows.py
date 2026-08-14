"""build_subflows.py — 组装 4 个历史子流程的 NR 定义（声明式 → 扁平条目数组）。

产出：nr_subflows/history/subflows_built.json（可直接喂 nr_client.create_subflow / deploy_all）。
实际部署 + E2E 验证见 Task #272。

设计（每个子流程内部图）：
  in 端口 → n_parse(function: 时间解析) → n_hist(取数) → n_calc(function: 计算) → out 端口
- 前 3 个（state_at/occurred/duration）用 api-get-history 取状态历史；
- aggregate 的 energy 走 statistics REST（http request），其余走 api-get-history。
- 答案对象统一写回 msg.payload 供下游『提取/分支』。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 固定子流程 id（与 subflows.py 的 HISTORY_*_SUBFLOW_ID 对齐；部署后回填该常量）
HIST_IDS = {
    "history_state_at": "af_hist_state_at",
    "history_occurred": "af_hist_occurred",
    "history_duration": "af_hist_duration",
    "history_aggregate": "af_hist_aggregate",
}

# NR1990 中 HA server 节点 id（env NR_HA_SERVER_ID 默认值）
HA_SERVER_ID = "e93e1ad9c034e866"
# docker 版 NR 访问宿主机 HA 的地址（容器内 localhost≠宿主机，见记忆 2026-07-20）
HA_STAT_URL = "http://host.docker.internal:8123"

TIME_PARSE_JS = (HERE / "time_parse.js").read_text(encoding="utf-8")
COMPUTE_JS = (HERE / "compute.js").read_text(encoding="utf-8")

# HA 取数失败（404 无历史 / 网络 / 鉴权）→ 降级为空结果数组，让 compute 函数返回
# found=false / occurred=false / duration=0 等结构化空答案，而非让消息丢失导致下游
# 『分支』中断。由子流程内部 catch 节点在 n_hist 抛错时注入。
ERR_BODY = (
    "// HA 取数失败（404 无历史 / 网络 / 鉴权）→ 降级为空结果数组，\n"
    "// 让 compute 函数返回 found=false / occurred=false / duration=0 等结构化空答案，\n"
    "// 而非让消息丢失导致下游『分支』中断。\n"
    "msg._hist_error = (msg.error && (msg.error.message || String(msg.error))) || 'unknown';\n"
    "msg.payload = [];\n"
    "return msg;\n"
)


def _parse_body(kind: str) -> str:
    """时间解析 function 体：嵌入 time_parse，并设置 msg._hist_* 供计算节点消费；
    同时通过 msg.payload.startDate/endDate/entityId 把窗口/实体透传给 api-get-history
    节点（该节点 0.80.x 原生支持经 msg.payload.* 覆盖配置，见 GetHistoryController 的
    messageProp 声明：startDate→payload.startDate / entityId→payload.entityId）。"""
    head = TIME_PARSE_JS + """
function _feedNode(msg, start, end) {
  // 注入机制（#107 定论，依据 api-get-history 0.80.3 源码实证，勿再改）：
  // dist/nodes/get-history/index.js 的输入声明为
  //   startDate:{messageProp:["payload.startDate"], configProp:"startDate"}
  //   endDate  :{messageProp:["payload.endDate"],   configProp:"endDate"}
  //   entityId :{messageProp:["payload.entityId"],  configProp:"entityId"}
  // 即节点原生支持经 msg.payload.* 覆盖面板配置（messageProp 优先于 configProp）。
  // 故窗口与实体一律走 msg.payload.* 注入，这是官方支持路径，无需任何节点属性改写。
  //
  // ⚠️ 已废弃路径，勿回退（#107 死结）：曾用 NR 运行时节点查找 API 反查 n_hist 实例并
  // 改写其节点属性。但 function 沙箱内该 API 不可用 → 守卫三元取 null → 其后的 fallback
  // 分支又对同一 API 裸调用 → 必然抛错（进入 fallback 的前提恰恰是该 API 不可用），
  // 子流程整体静默失败。该路径已整体删除，本文件不再留任何运行时节点查找引用。
  //
  // G1.5(#107) 修复：入站 payload 可能是数字/字符串时间戳（实测 at/start 落在 payload）。
  // 旧写法 !msg.payload 对数字/非空字符串为 truthy 不触发重置 → 后续 msg.payload.entityId
  // 给原始值赋属性被 JS 静默忽略 → entityId 注入全失 → ValidationError。
  // 改为：payload 非对象（含数字/字符串/数组/null/undefined）一律重置为空对象后再注入。
  if (!msg.payload || typeof msg.payload !== "object" || Array.isArray(msg.payload)) {
    msg.payload = {};
  }
  msg.payload.startDate = toHAISO(start);
  msg.payload.endDate = toHAISO(end);
  msg.payload.entityId = msg.entity || "";
  return msg;
}
"""
    if kind == "history_state_at":
        return head + """
// 某时刻状态：回看 6h 取最近邻采样（覆盖『T 之前最后一次状态』场景）
// ⚠️ api-get-history 节点要求 endDate >= now，否则返回空数组；故 end 用 now（而非 target+1min）。
const now = new Date();
let target = parseNaturalTime(msg.at, now) || new Date(now.getTime());
// G1 future-guard：at 在未来（如凌晨跑「今日08:00」）→ 钳到 now，语义等价于「此刻状态」，
// 避免 start>end 反向窗口 → HA 抛 ValidationError: startDate must be a valid date。
if (target.getTime() > now.getTime()) target = new Date(now.getTime());
const start = new Date(target.getTime() - 6 * 3600 * 1000);
const end = new Date(now.getTime() + 60000);
msg._hist_start = toHAISO(start);
msg._hist_end = toHAISO(end);
msg._hist_target = toHAISO(target);
msg._hist_target_iso = toHAISO(target);
return _feedNode(msg, start, end);
"""
    return head + """
const now = new Date();
let s = parseNaturalTime(msg.start, now);
// end 缺省（未给 / 解析不出）→ now+60s，语义=「查到现在」
let e = parseNaturalTime(msg.end, now) || new Date(now.getTime() + 60000);
// ⚠️ #118 修复，勿回退为「过去 end 一律钳到 now」：
//   旧代码 `if (e < now) e = now+60s` 的立论是「api-get-history 对过去 endDate 返回空数组」，
//   该前提【已实测证伪】——GetHistoryController 只是把 endDate 原样透传给
//   HA /api/history/period 的 end_time，过去时间正常返回数据（2026-08-04 实测：
//   昨天00:00→今天00:00 返回 11 条）。旧钳位会把用户显式指定的过去 end 偷偷改成「现在」，
//   使「昨天00:00→今天00:00」变成「昨天00:00→现在」，把今天的数据混进昨天的统计
//   （实测 mean(temperature) 24.7 被污染成 24.909）。
// 现在只钳【未来】end：未来时刻尚无历史，钳到 now 才有意义。
if (e.getTime() > now.getTime()) e = new Date(now.getTime() + 60000);
// G1 future-guard：未来 start（如凌晨跑「今日08:00」）→ 钳到 now，避免 start>end 反向窗口
// → HA 抛 ValidationError: startDate must be a valid date；并兜底交换防反向窗口。
if (s && s.getTime() > now.getTime()) s = new Date(now.getTime());
if (s && e && s.getTime() > e.getTime()) { const _t = s; s = e; e = _t; }
msg._hist_start = toHAISO(s);
msg._hist_end = toHAISO(e);
return _feedNode(msg, s, e);
"""


def _calc_body(kind: str) -> str:
    common = COMPUTE_JS + "\nconst arr = msg.payload || [];\n"
    if kind == "history_state_at":
        return common + "msg.payload = computeStateAt(msg, arr);\nreturn msg;"
    if kind == "history_occurred":
        return common + "msg.payload = computeOccurred(msg, arr);\nreturn msg;"
    if kind == "history_duration":
        return common + "msg.payload = computeDuration(msg, arr);\nreturn msg;"
    if kind == "history_aggregate":
        # energy 走 statistics（n_hist 为 http 节点，结果已在 msg.payload）；其余走 api-get-history
        return common + (
            "if (msg.metric === 'energy') { msg.payload = computeEnergy(msg, msg.payload); }\n"
            "else { msg.payload = computeAggregate(msg, arr); }\n"
            "return msg;"
        )
    raise ValueError(kind)


def _debug_node(nid: str, label: str, x: int, y: int) -> dict:
    """managed 子流程内部 debug 探针：全量 msg 捕获，经 NR 原生 comms ws 流出供 debug_bridge 旁路采集。

    设计依据（#644 铁律）：采集走网关后台线程订阅 ws://<nr>/comms debug 事件流，绝不往 flow 插采集
    节点（热路径1不碰）；内部节点要能在运行时出事件，必须在 build 期把 debug 节点预置进 managed 子流程。
    """
    return {"id": nid, "type": "debug", "z": "", "name": label,
            "active": True, "tosidebar": True, "console": False, "tostatus": False,
            "complete": "true", "targetType": "full",
            "statusVal": "", "statusType": "auto", "x": x, "y": y, "wires": [[]]}


def _build_one(kind: str, name: str, info: str):
    n_parse = {"id": "n_parse", "type": "function", "z": "", "name": "解析时间",
               "func": _parse_body(kind), "outputs": 1, "wires": [["n_hist", "n_dbg_parse"]]}
    n_calc = {"id": "n_calc", "type": "function", "z": "", "name": "计算答案",
              "func": _calc_body(kind), "outputs": 1, "wires": [["n_dbg_calc"]]}

    # 所有 4 个能力统一用 api-get-history 取状态历史；窗口/实体全部由 n_parse 经
    # msg.payload.{entityId,startDate,endDate} 注入（见 _feedNode 顶部的源码实证注释）。
    # ⚠️ energy 需 HA 长期统计（statistics），history period 取不到 kWh 累计；当前 energy
    # 占位返回 0 kWh（computeEnergy 解析不到 change 字段），#273 后再升级为 statistics REST。
    n_hist = {
        "id": "n_hist", "type": "api-get-history", "z": "", "name": "取历史",
        "server": HA_SERVER_ID, "version": 1,
        # ⚠️ entityIdType 的 schema 白名单只有 equals/regex（见 dist/nodes/get-history/const.js
        # 的 EntityFilterType），写 "msg" 会被部署期校验直接拒绝（#107 实测踩坑，勿回退）。
        # entityId 留空占位：运行时由 msg.payload.entityId 覆盖（messageProp 优先于 configProp）。
        "entityId": "", "entityIdType": "equals",
        "useRelativeTime": False, "relativeTime": "",
        # startDate/endDate 同样留空占位，运行时由 msg.payload.startDate/endDate 覆盖。
        "startDateType": "date", "startDate": "",
        "endDateType": "date", "endDate": "",
        "flatten": True, "outputType": "array",
        "outputLocationType": "msg", "outputLocation": "payload",
        "outputs": 1, "x": 400, "y": 200, "wires": [["n_calc", "n_dbg_hist"]],
    }

    # 子流程内部降级：n_hist 抛错（404 无历史 / 网络 / 鉴权）→ catch 捕获 →
    # 注入空结果数组给 n_calc，使下游『分支』拿到结构化空答案而非消息丢失。
    n_catch = {"id": "n_catch", "type": "catch", "z": "", "name": "HA错误降级",
               "scope": ["n_hist"], "uncaught": False,
               "outputs": 1, "x": 400, "y": 320, "wires": [["n_err"]]}
    n_err = {"id": "n_err", "type": "function", "z": "", "name": "空结果",
             "func": ERR_BODY, "outputs": 1, "x": 400, "y": 400,
             "wires": [["n_calc"]]}

    # G1（#644 铁律）：build 期预置 debug 节点进 managed 子流程，使内部节点在运行时能经 NR 原生
    # ws://<nr>/comms debug 事件流被 debug_bridge 旁路采集（热路径1不插采集节点）。三个探针分别 tap
    # 解析窗口/实体注入(n_dbg_parse)、HA 原始返回(n_dbg_hist)、最终 computed 答案(n_dbg_calc)。
    n_dbg_parse = _debug_node("n_dbg_parse", "DBG 解析", 120, 40)
    n_dbg_hist = _debug_node("n_dbg_hist", "DBG 取数", 560, 40)
    n_dbg_calc = _debug_node("n_dbg_calc", "DBG 答案", 560, 260)
    nodes = [n_parse, n_hist, n_catch, n_err, n_calc, n_dbg_parse, n_dbg_hist, n_dbg_calc]
    in_ports = [{"x": 40, "y": 40, "wires": [{"id": "n_parse"}]}]
    out_ports = [{"x": 40, "y": 120, "wires": [{"id": "n_calc", "port": 0}]}]

    def_entry = {
        "id": HIST_IDS[kind], "type": "subflow", "name": name,
        "info": info, "category": "subflows",
        "in": in_ports, "out": out_ports,
        "status": {"x": 0, "y": 0, "wires": []},
        "env": [], "meta": {},
    }
    internals = []
    for n in nodes:
        nn = dict(n)
        nn["z"] = HIST_IDS[kind]
        internals.append(nn)
    return [def_entry] + internals


def build_all():
    return [
        _build_one("history_state_at", "历史·某时刻状态",
                   "查询实体在某过去时刻的状态/属性值。入参 entity/at/attribute。答案回 msg.payload。"),
        _build_one("history_occurred", "历史·是否发生",
                   "查询区间内是否发生状态变化/达到某态。入参 entity/start/end/state?/attribute?。"),
        _build_one("history_duration", "历史·处于某态时长",
                   "统计区间内处于某态的累计时长。入参 entity/start/end/state。"),
        _build_one("history_aggregate", "历史·聚合统计",
                   "区间聚合 energy/count/mean/min/max/sum。入参 entity/start/end/metric/attribute?。"
                   "⚠️ energy 需 statistics（#272 E2E 验证，可能需改 http+token）。"),
    ]


if __name__ == "__main__":
    entries = build_all()
    out = HERE / "subflows_built.json"
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    n_nodes = sum(len(e) for e in entries)
    print(f"✅ 已生成 4 个子流程定义（共 {n_nodes} 条目 = 4 组 × 9 条目）→ {out}")
