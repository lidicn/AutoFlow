"""AutoFlow 静态 Flow Linter（A1 — 编译期/部署前抓「静态合法、运行必错」反模式）。

设计目标：在 `propose_dsl` / `deploy_raw` 部署前，对**已编译或白盒产出的 Node-RED flow JSON**
做纯静态分析，拦住本次 ArcFace 排障暴露的两类坑：
  - NR switch 语义陷阱：`otherwise`(else) 分支排在正例之前 + checkall=false → 正例成死代码。
  - function / change 读取的 `msg.payload.X.Y` 路径，与上游 API 实际 emit 的结构不一致
    （如真实 ArcFace 返回扁平 `payload.faces`，函数却读 `payload.object.faces`）。
   - 同一 flow 内节点 id 撞车（NR 导入静默丢节点）→ R16。
   - wires 指向不存在的节点 id（悬空连线）→ R17。
   - 子流程定义 in/out 端口声明却无连线（死端口）→ R18。
   - switch 某条分支无输出连线（静默死分支）→ R21（warning，不拦）。
   - 节点必填字段缺失（api-call-service/service、switch/rules 等）→ R22（error 硬拦 / warning 软提示）。
  - 节点关键空参（空 func 的 function / 空 domain 的 api-call-service）→ R32（error 硬拦，补 R22 未覆盖的死角）。
  - 整条流无任何 effectful 节点（纯 stub / pass-through）→ R33（warning，fail-open 不拦）。
  - switch 直接读 `msg.<实体ID>` 路径（如 `sun.sun` / `weather.xxx`，首段为已知 HA 域）→ R34（error，
    提示改用 api-current-state + switch(payload)。HA websocket 不注入 `msg.<entity_id>`，条件恒假静默失败）。
  - http request bodyType=json 但上游没有 change/function 构造 msg.payload → body 可能为空/错。
  - switch/change 的 JSONata 表达式语法预校验（启发式，NR 同款引擎才 100% 准）。

本模块零依赖（仅标准库），不触真实 HA/NR，纯静态。lint 结果为 warning/error/info 列表，
非阻塞（部署仍继续，但 agent 能在提案/部署回执里立刻看到并自我修正）。

落点：gateway.propose_dsl / gateway.deploy_raw 部署前调用，结果随回执返回。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ── 公共类型 ──
# lint issue: {"level": "error"|"warning"|"info", "rule": "R1".."R4",
#              "node_id": str, "node_type": str, "message": str}


# ── R34：switch 直接读 `msg.<实体ID>` 路径检测所需 ──
# HA 已知域集合（首段）。用于判断 switch.property 的「首段」是否像实体 ID（如 sun/weather/sensor…）。
# 注意：payload/data/topic/headers/name/req/res/error 等是 NR 合法 msg 根，绝不可误判。
_HA_DOMAINS = frozenset({
    "sun", "weather", "sensor", "binary_sensor", "light", "switch", "climate",
    "cover", "fan", "lock", "media_player", "device_tracker", "person", "zone",
    "input_boolean", "input_number", "input_select", "input_text", "input_datetime",
    "number", "select", "button", "event", "update", "vacuum", "humidifier",
    "water_heater", "alarm_control_panel", "camera", "image", "scene", "automation",
    "group", "script", "timer", "plant", "valve", "notify", "geo_location", "tag",
})
# 实体 ID 形状：<domain>.<object_id>[.<sub>]*，全小写字母数字下划线，≥2 段、首段为已知域才判。
_ENTITY_ID_PATH_RE = re.compile(r"^([a-z][a-z0-9_]*)\.[a-z0-9_]+(\.[a-z0-9_]+)*$")


def _looks_like_entity_path(prop: str) -> bool:
    """switch.property 是否形如 HA 实体 ID 路径（首段为已知 HA 域）。"""
    if not isinstance(prop, str):
        return False
    m = _ENTITY_ID_PATH_RE.match(prop)
    if not m:
        return False
    return m.group(1) in _HA_DOMAINS


# ── 节点关系工具 ──
def _flat_wire_targets(wire_list) -> List[str]:
    """把一个 output 的 wires 列表拍平成「字符串 target id」列表。

    兼容畸形嵌套（target 自身是 list，如 [['a']]）与非字符串元素，
    遍历时丢弃非法项。用于下游建图 / lint 遍历，避免 `unhashable type: 'list'` 崩溃。
    """
    result: List[str] = []
    if not isinstance(wire_list, list):
        return result
    for t in wire_list:
        if isinstance(t, list):
            result.extend(_flat_wire_targets(t))
        elif isinstance(t, str):
            result.append(t)
        # 其他类型（None/数字/dict）丢弃
    return result


def _build_reverse_graph(nodes: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """返回 {node_id: [上游 node_id, ...]}，按 wires 反向建图。"""
    rev: Dict[str, List[str]] = {n.get("id"): [] for n in nodes}
    idset = set(rev)
    for n in nodes:
        nid = n.get("id")
        for wire_list in (n.get("wires") or []):
            for tgt in _flat_wire_targets(wire_list):
                if tgt in idset:
                    rev.setdefault(tgt, []).append(nid)
    return rev


def _build_forward_graph(nodes: List[Dict[str, Any]], idset: set) -> Dict[str, List[str]]:
    """返回 {node_id: [下游 node_id, ...]}，按 wires + link 链正向建图。

    link 链：link out 节点的 `links` 指向 link in 节点 id，link in 再经 wires 续流。
    仅收录 idset 内（同一 flow / 同一 lint 集合）的目标，跨 tab 的 link 不在图里，
    不会凭空制造环或可达性误判。
    """
    fwd: Dict[str, List[str]] = {n.get("id"): [] for n in nodes}
    for n in nodes:
        nid = n.get("id")
        for wire_list in (n.get("wires") or []):
            for tgt in _flat_wire_targets(wire_list):
                if tgt in idset:
                    fwd.setdefault(nid, []).append(tgt)
        if n.get("type") == "link out":
            for lin in (n.get("links") or []):
                if lin in idset:
                    fwd.setdefault(nid, []).append(lin)
    return fwd


# NR 里「消息源头」节点：它们产出 msg，但不算「构造了 http body」的 setter
_ORIGIN_TYPES = {
    "inject", "tab", "comment", "catch", "status", "websocket in",
    "server", "server-state-changed", "events: all", "api-call-service",
    "ha-entity", "mqtt in", "tcp in", "udp in", "file in", "zwave",
    "debug",
}

# ── B1（R14）正向可达性分析所需 ──
# 消息「根/触发源」节点：它们自己产生 msg，是正向可达图的起点；
# 即便没有任何连线指向它们，也不应被判定为「死节点」。
# 列全常见 NR/HA 触发类型，避免漏列导致下游整链误判为不可达。
_ENTRY_TYPES = {
    "inject", "server-state-changed", "server-events", "link in", "catch",
    "status", "complete", "mqtt in", "websocket in", "tcp in", "udp in",
    "email", "imap", "pop3", "events: all", "api-current-state",
    "ha-entity", "ha-time", "ha-wait-until", "trigger-state", "poll-state",
    "trigger", "time", "http in",
    "time-range-switch",
}

# 非消息流节点（配置 / 全局 / UI 布局 / 注释）：它们本就没有 wires，
# 也不该被可达性分析判定为「死节点」。漏列会导致误报，故宁可多列。
_CONFIG_TYPES = {
    "server", "mqtt-broker", "tls-config", "global-config", "comment",
    "tab", "subflow", "websocket-listener", "group", "junction",
    "credentials", "crypto-config", "history", "influxdb", "mysql",
    "postgresql", "mongodb", "mongodb2", "redis-config", "twilio-config",
    "email", "feedparse", "watch",
    # Node-RED dashboard / 各类 UI 配置节点
    "ui_group", "ui_tab", "ui_base", "ui_page", "ui_button", "ui_switch",
    "ui_dropdown", "ui_text", "ui_gauge", "ui_chart", "ui_template",
    "ui_led", "ui_slider", "ui_form", "ui_table", "ui_audio",
}
# 以这些前缀开头的类型也视为配置/布局节点（dashboard 系常扩展新类型）
_CONFIG_PREFIXES = ("ui_", "dashboard", "config-", "group-")

# 明确「几乎从不是消息源头」的下游处理/动作类型：零入度且属此类 → 视为孤立候选
# （用于 B1 根播种：未知触发类型零入度会被当根避免误报；而这类零入度节点是真正漏连）。
_DOWNSTREAM_TYPES = {
    "function", "change", "switch", "template", "http request", "delay",
    "link out", "debug", "api-call-service", "ha-call-service",
    "axios-request", "ha-api", "ha-get-entities", "range", "csv", "json",
    "xml", "html", "markdown", "join", "split", "sort", "batch", "change",
}

# R13 已独占处理 api-call-service / ha-call-service 的孤立问题，R14 不再重复报。
_R14_SKIP_TYPES = {"api-call-service", "ha-call-service"}

# ── B2（R15）环检测所需 ──
# 受控循环节点：含这些节点的环通常是「有意为之」的节流/自触发循环
# （如 TTS 队列 function→delay→function、调度器 link out→link in 自触发），
# 不应报为错误。不含这些节点的紧致环才是 accidental 死循环。
_THROTTLE_TYPES = {
    "delay", "trigger", "link out", "link in", "time", "ha-time",
    "trigger-state", "poll-state", "server-events",
}

# ── 安装即对账：子流程 hash 漂移检测（R29, warning）──
def detect_subflow_drift(nodes: List[Dict[str, Any]],
                         current_subflows) -> List[Dict[str, str]]:
    """检测已部署 flow 引用的子流程是否已升级（hash 漂移）→ stale warning。

    对应网关需求报告「方案 b」：子流程按裸 hash 引用，升级后 hash 变、旧引用
    静默断链。本函数把这种漂移变成可见信号（warning，不硬拦——已上线的旧流
    仍能跑，只是提示重新部署刷新引用）。

    current_subflows: NR 当前已定义子流程清单，元素为 {"id": <hex>, "name": <str>}
                      或 name->id 的 dict。由调用方（gateway，持有 NR 上下文）传入；
                      flow_linter 自身不连 NR，离线/无上下文时 caller 传 [] 即可跳过。
    返回 warning 级 issue（rule=R29）。仅针对「类型已注册但 hash 漂移」的情形：
      - 引用的 hash 已不在当前子流程清单 → 若按 name 能匹配到同名的其它 hash → 升级漂移，告警
      - 若按 name 也找不到 → 属「子流程真缺失」，由 node_gate 硬拦，这里不重复报
    """
    issues: List[Dict[str, str]] = []
    if not current_subflows:
        return issues
    # 归一为 name->id 与 current_ids 集合
    name_to_id: Dict[str, str] = {}
    current_ids = set()
    if isinstance(current_subflows, dict):
        for k, v in current_subflows.items():
            name_to_id[str(k)] = str(v)
            current_ids.add(str(v))
    else:
        for s in current_subflows:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            nm = s.get("name")
            if sid:
                current_ids.add(str(sid))
            if nm and sid:
                name_to_id[str(nm)] = str(sid)
    if not current_ids:
        return issues
    HEX = re.compile(r"^[0-9a-fA-F]{24}$")
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type", "")
        # 解析引用的子流程 id（支持 "subflow:<id>" 与裸 24 位 hex 两种写法）
        old_id = None
        if isinstance(t, str) and t.startswith("subflow:"):
            old_id = t[len("subflow:"):]
        elif isinstance(t, str) and HEX.match(t):
            old_id = t
        if not old_id:
            continue
        if old_id in current_ids:
            continue  # 引用的是当前 hash，正常
        nm = n.get("name")
        if nm and str(nm) in name_to_id and name_to_id[str(nm)] != old_id:
            issues.append({
                "level": "warning", "rule": "R29",
                "node_id": n.get("id", "?"),
                "node_type": t,
                "message": f"子流程『{nm}』已升级（hash 漂移）：flow 仍引用旧 hash "
                           f"{old_id}，当前为 {name_to_id[str(nm)]}。"
                           f"建议重新部署该 flow 以刷新子流程引用，否则可能静默断链。",
            })
    return issues



def _is_payload_setter(n: Dict[str, Any]) -> bool:
    """节点是否会把 msg.payload 重新构造（=能成为 http 请求体来源）。"""
    t = n.get("type", "")
    if t == "function":
        return True
    if t == "template":
        # template 节点通过 field 指定写入目标
        return (n.get("field") or "payload") == "payload"
    if t == "change":
        for r in (n.get("rules") or []):
            p = r.get("p") or ""
            if r.get("t") in ("set", "change", "move") and (
                p == "payload" or p.startswith("payload.")
            ):
                return True
    return False


def _upstream_has_payload_setter(
    start_id: str,
    rev: Dict[str, List[str]],
    nodes_by_id: Dict[str, Dict[str, Any]],
    depth_limit: int = 50,
) -> bool:
    """从 http 节点反向 BFS，任一上游链出现 payload setter 即返回 True。
    若所有反向路径都只到 origin 节点而无 setter，则 False（body 未构造）。"""
    if start_id not in rev:
        return False
    visited = set()
    stack = [(start_id, 0)]
    while stack:
        nid, depth = stack.pop()
        if nid in visited or depth > depth_limit:
            continue
        visited.add(nid)
        for up in rev.get(nid, []):
            un = nodes_by_id.get(up)
            if not un:
                continue
            if _is_payload_setter(un):
                return True
            if un.get("type") in _ORIGIN_TYPES:
                # 抵达消息源仍未遇 setter：这条链未构造 body，继续找其它链
                continue
            stack.append((up, depth + 1))
    return False


# ── JSONata 启发式语法检查（NR 同款引擎才 100% 准；这里做宽松预检）──
def _check_jsonata(expr: str) -> Tuple[bool, str]:
    """返回 (ok, 说明)。启发式：括号/方括号/花括号平衡、引号平衡、无 .. 双点。
    允许 NR 扩展语法 `:=` 绑定（standalone jsonata 不支持，但 NR 内置支持）。"""
    if not isinstance(expr, str) or not expr.strip():
        return True, ""
    s = expr
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set(pairs.values())
    stack = []
    i = 0
    in_str = False
    while i < len(s):
        ch = s[i]
        if ch == '"' or ch == "'":
            # 处理转义引号
            if in_str:
                if s[i - 1] != "\\":
                    in_str = False
            else:
                in_str = True
        elif in_str:
            pass
        elif ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False, f"括号不匹配：'{ch}' 无对应左侧 '{pairs[ch]}'"
            stack.pop()
        i += 1
    if in_str:
        return False, "字符串引号未闭合"
    if stack:
        return False, f"括号未闭合：多余 '{stack[-1]}'"
    if ".." in s:
        return False, "出现 '..' 连续点（JSONata 路径不允许）"
    return True, ""


# ── R37（round4 R7）：link-out 请求侧错挂「取返回值/提取」节点 ────────────
def _lint_link_out_request_side(nodes: List[Dict[str, Any]],
                                nodes_by_id: Dict[str, Any]) -> List[Dict[str, str]]:
    """R37(#round4 R7) iss_516bc5d816（报告 A16）：异步 link-out 的**请求侧**挂了取值节点。

    `link out` 是 fire-and-forget：消息投递到目标 `link in` 就结束，Node-RED
    **没有**把结果送回调用点的通道（回送必须用 `link call` + `link out(mode=return)`）。
    所以下面这种接线是断链：

        change(设 msg.payload = 入参) ──┬─→ link out(→ 子流程入口)
                                        └─→ change(取返回值 / 提取字段)

    右下那个节点并不在回执侧：它和 link out 同时收到**刚被入参覆写的那条 msg**，
    读 `payload.*` 只会拿到自己刚写进去的入参（或 undefined）。运行期不报错、
    不中断，纯静默取错值——正是最难排查的一类。

    判据收紧到能确证「请求侧」的形状：上游 change 写了 msg.payload（= 设入参），
    且同一输出口既连 link out 又连另一个读 payload 的 change。warning 级不阻断
    ——手搓流里「发出去顺带记一笔」是合法写法，不能硬拦。

    注：编译器侧已在 `_emit_body` 修正为副链编排，不再产出该形状；本规则是给
    手搓 / 白箱 deploy_raw / 历史提案兜底。
    """
    out: List[Dict[str, str]] = []
    for n in nodes:
        if n.get("type") != "change":
            continue
        # 上游必须是「设入参」：写了 msg.payload 或 msg.payload.<k>
        if not any(r.get("t") == "set" and r.get("pt", "msg") == "msg"
                   and str(r.get("p") or "").split(".")[0] == "payload"
                   for r in (n.get("rules") or [])):
            continue
        for port in (n.get("wires") or []):
            targets = [t for t in (port or []) if t]
            link_outs = [t for t in targets
                         if (nodes_by_id.get(t) or {}).get("type") == "link out"]
            if not link_outs:
                continue
            for t in targets:
                if t in link_outs:
                    continue
                d = nodes_by_id.get(t) or {}
                if d.get("type") != "change":
                    continue
                reads = [r for r in (d.get("rules") or [])
                         if r.get("t") == "set"
                         and r.get("tot") in ("jsonata", "msg")
                         and "payload" in str(r.get("to") or "")]
                if not reads:
                    continue
                lo_name = (nodes_by_id.get(link_outs[0]) or {}).get("name") or link_outs[0]
                out.append({
                    "level": "warning", "rule": "R37", "node_id": t,
                    "node_type": "change",
                    "message": (
                        f"change 节点『{d.get('name') or t}』挂在 link-out 调用的"
                        f"**请求侧**（与 `link out`「{lo_name}」同挂在设入参节点 "
                        f"`{n.get('name') or n.get('id')}` 的同一输出口）。"
                        f"link out 是 fire-and-forget，Node-RED 没有把结果送回调用点的"
                        f"回执通道，本节点收到的是**刚被入参覆写的那条 msg**，"
                        f"读 `payload.*` 只会拿到自己刚写进去的入参或 undefined —— "
                        f"运行期不报错，纯静默取错值。"
                        f"若需要返回值，请改用请求/响应型调用（`link call` + 目标流以 "
                        f"`link out(mode=return)` 回送，或直接用 http request / 子流程实例）；"
                        f"若这里本就只是发完顺带记一笔，请把它挪到 link out 之前、"
                        f"或改读与返回值无关的字段以消除歧义。"
                        f" —— 取值表达式 `{reads[0].get('to')}`"
                    ),
                })
    return out


# ── 主 lint ──
def lint_flow(flow: Dict[str, Any], b1_unreachable: bool = False) -> List[Dict[str, str]]:
    """对 flow JSON（{nodes:[...]}）做静态 lint，返回 issue 列表。

    b1_unreachable: 是否启用 R14 不可达节点检测（默认关闭）。
      原因：R14 基于单实例可达性，对「跨实例 link」（另一 NR 实例的 link out → 本实例）
      会产生大量误报（本实例看不到上游 → 误判死节点）。故仅在对**自包含白盒 flow**
      （agent 手搓、无跨实例依赖）做部署前检查时开启；对整实例/跨实例流保持关闭以免刷屏。
    """
    issues: List[Dict[str, str]] = []
    nodes = flow.get("nodes")
    if not isinstance(nodes, list):
        return issues

    nodes_by_id = {n.get("id"): n for n in nodes if n.get("id")}
    idset = set(nodes_by_id)
    rev = _build_reverse_graph(nodes)
    fwd = _build_forward_graph(nodes, idset)

    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        if ntype == "switch":
            issues.extend(_lint_switch(n, nid))
        elif ntype == "function":
            issues.extend(_lint_function(n, nid))
        elif ntype == "change":
            issues.extend(_lint_change(n, nid))
        elif ntype == "subflow":
            issues.extend(_lint_subflow_def(n, nid))
        elif ntype == "http request":
            issues.extend(_lint_http_request(n, nid, rev, nodes_by_id))

    # R10-R12: 全局结构检查（需遍历所有节点）
    issues.extend(_lint_wire_structure(nodes, idset))
    issues.extend(_lint_entity_id_format(nodes))
    issues.extend(_lint_ha_node_field_typos(nodes))
    issues.extend(_lint_ha_node_missing_entity(nodes))
    # R24：server-state-changed 的 ifState 混入时长词 → error（持久等待应拆进 for）
    issues.extend(_lint_trigger_duration(nodes))
    issues.extend(_lint_missing_z(nodes))
    # R25: comment 节点被当作消息中转（带 wires 或被接入主链）→ warning（对齐压测报告 Bug-3）
    issues.extend(_lint_comment_relay(nodes))
    # R26: 变量↔分支作用域一致性（白箱 raw 路径的 C2 守护）→ warning
    issues.extend(_lint_var_branch_scope(nodes))
    # R27: 动作参数字面数值越界（WB4 #3）→ warning（白名单制，仅字面数字、不硬拦）
    issues.extend(_lint_numeric_range(nodes))
    # R_SERVICE_PARAM：HA 服务非法参数（WB22 T1 / B1）→ error 硬拦（fail-open 黑名单）
    issues.extend(_lint_service_param(nodes))
    # R16: 重复节点 id（同一 flow 内 id 撞车）
    issues.extend(_lint_duplicate_ids(nodes))
    # R13: 孤立动作节点（无入边且为已知动作/处理类型）→ 几乎必为漏连
    issues.extend(_lint_orphan_action_nodes(nodes))
    # R18: 子流程定义 in/out 端口死端口（声明却无连线）
    issues.extend(_lint_subflow_dead_ports(nodes))
    # R21: switch 死分支（某条规则无输出连线）→ warning（不硬拦，避免误伤丢弃分支写法）
    issues.extend(_lint_switch_dead_branches(nodes))
    # R31：分支/条件 引用未定义字段（取值/变量 未声明）→ warning（WB16 iss_0a831a1760）
    issues.extend(_lint_undefined_field_ref(nodes))
    # R22: 节点必填字段表（A 静态预检核心）→ error(不可运行) 硬拦 / warning(软提示)
    issues.extend(_lint_node_required_fields(nodes))
    # R32：关键空参（编译过但必废：空 func 的 function / 空 domain 的 api-call-service）→ error 硬拦
    issues.extend(_lint_key_empty_params(nodes))
    # R33：整条流无 effectful 节点（纯 stub / pass-through）→ warning（fail-open，不阻塞）
    issues.extend(_lint_noop_flow(nodes))
    # R37（round4 R7）：link-out 请求侧错挂「取返回值/提取」节点（无回执 → 静默取错值）
    issues.extend(_lint_link_out_request_side(nodes, nodes_by_id))
    # B1（R14）：不可达节点 / 死代码（正向全图可达性）。默认关闭（见 b1_unreachable 说明）。
    if b1_unreachable:
        issues.extend(_lint_unreachable_nodes(nodes, fwd, idset))
    # B2（R15）：环检测（避免白盒手搓出死循环链）
    issues.extend(_lint_cycles(nodes, fwd, idset))
    # R23：事件环检测（触发器监听实体被其下游动作改回 → 经 HA 状态重入的死循环）
    issues.extend(_lint_event_loops(nodes, fwd, idset))
    return issues


# ── R1：switch otherwise 前置 → 死代码 ──
def _r34_issue(nid: str, prop: str, ptype: str, rule_idx: int = 0) -> Dict[str, str]:
    """构造一条 R34 error issue：switch 直接读实体 ID 路径。"""
    where = f"第 {rule_idx} 条分支的" if rule_idx else "节点级"
    return {
        "level": "error", "rule": "R34", "node_id": nid, "node_type": "switch",
        "message": (
            f"switch 的{where}`property` 为 `{prop}`（propertyType={ptype}），"
            f"形如 HA 实体 ID 路径。HA websocket 节点**不会**把实体状态注入 `msg.{prop}`，"
            f"该路径运行态恒为 undefined → 条件永远为假、动作永不执行"
            f"（典型黑箱静默 bug，灯永不响应）。"
            f"请先在 flow 内放 `api-current-state` 节点读取该实体（输出落到 `msg.payload`/`msg.data`），"
            f"再把 switch 的 property 改为 `payload`（或 `payload.state`）。"
            f"编译器 DSL 用 `取值: {prop}  分支: payload == ...` 会自动生成正确链路。"
        ),
    }


def _lint_switch(n: Dict[str, Any], nid: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    rules = n.get("rules") or []
    checkall = n.get("checkall", True)
    # R34：switch 直接读 `msg.<实体ID>` 路径（如 sun.sun / weather.xxx）→ 条件恒假静默 bug。
    # HA websocket 不会把实体状态注入 msg.<entity_id>，必须先用 api-current-state 读到 msg.payload。
    # 仅对 path 型 propertyType（msg/flow/global）生效；jsonata 走 R30 另查。
    ptype = n.get("propertyType") or "msg"
    if ptype in ("msg", "flow", "global"):
        if _looks_like_entity_path(n.get("property")):
            out.append(_r34_issue(nid, n.get("property"), ptype))
        for ri, r in enumerate(rules):
            # 规则级 property 覆盖（非 jsonata 比较规则才有「路径」语义）
            if (r.get("t") not in ("jsonata", "jsonata_exp")) and _looks_like_entity_path(r.get("property")):
                out.append(_r34_issue(nid, r.get("property"), ptype, rule_idx=ri + 1))
    # R30：switch 规则的 JSONata 语法预检（括号/引号配平）。WB16 iss_d184f78b7c。
    # 必须在「有无 else 分支」判断之前执行——否则无 else 的纯 jsonata 分支 switch
    # 会整段跳过本检查（早期 `if not else_indices: return out` 漏掉），导致非法表达式
    # 部署后运行态崩溃、整条链路中断。
    for i, r in enumerate(rules):
        vt = r.get("vt") or r.get("t")
        if vt in ("jsonata", "jsonata_exp"):
            expr = r.get("v") or ""
            ok, msg = _check_jsonata(expr)
            if not ok:
                out.append({
                    "level": "error", "rule": "R30", "node_id": nid,
                    "node_type": "switch",
                    "message": (
                        f"switch 第 {i + 1} 条分支的 JSONata 表达式语法疑似有误：{msg}"
                        f" —— 表达式 `{expr}`。该表达式运行态会被 JSONata 引擎拒绝并抛错，"
                        f"导致整条链路中断。请修正括号配平 / 引号闭合。"
                    ),
                })
    # R35（round4 R6）：常量字面条件分支（分支: true / 分支: false）。
    # 裸布尔字面量作为分支条件 → 该分支恒真/恒假，动作永不可达（或恒执行、冗余），
    # 作者无提示。warning 级（与 R30 区分：R30 是语法错误 error）。
    for i, r in enumerate(rules):
        if r.get("t") in ("jsonata", "jsonata_exp") and r.get("v", "").strip() in ("true", "false"):
            const = r["v"].strip()
            out.append({
                "level": "warning",
                "rule": "R35",
                "node_id": nid,
                "node_type": "switch",
                "message": (
                    f"switch 第 {i + 1} 条分支的条件是常量字面量 `{const}`，"
                    f"{'该分支恒为真（其后分支永不触发，逻辑冗余）' if const == 'true' else '该分支恒为假（其中动作永不可达）'}。"
                    f"请确认是否误写；若需无条件执行请用「动作」直接写，删掉该空分支。"
                ),
            })
    # 找到 otherwise(else) 规则的位置
    else_indices = [i for i, r in enumerate(rules) if (r.get("t") == "else")]
    if not else_indices:
        return out
    for ei in else_indices:
        is_last = (ei == len(rules) - 1)
        if not is_last and not checkall:
            # 最危险：otherwise 之后还有正例分支且 checkall=false → 后续分支必为死代码
            out.append({
                "level": "error",
                "rule": "R1",
                "node_id": nid,
                "node_type": "switch",
                "message": (
                    "switch 的 otherwise(否则) 分支位于正例分支之前，且 checkall=false → "
                    "消息一旦命中 otherwise 即停止路由，其后的正例分支（如 'matched=true'）"
                    "永远不触发，成为死代码。这正是本次 ArcFace 排障的坑。"
                    "请保证 otherwise 是最后一条规则，或置 checkall=true。"
                ),
            })
        elif not is_last and checkall:
            out.append({
                "level": "warning",
                "rule": "R1",
                "node_id": nid,
                "node_type": "switch",
                "message": (
                    "switch 的 otherwise(否则) 分支不在最后一条。checkall=true 时后续分支仍可触发，"
                    "但顺序反直觉、易引发维护错误。建议把 otherwise 移到规则列表末尾。"
                ),
            })
        elif is_last and not checkall:
            # 正确顺序，但提醒：otherwise 在最后 + checkall=false 是 OK 的
            pass
    return out


# ── R31：分支/条件 引用未定义字段（静默逻辑 bug）──
# 取值: <entity> <字段> 把状态写到 msg.payload.<字段>；变量: 把值写到 flow.<名>。
# 分支/条件 的 JSONata 表达式若引用了从未声明的字段名（如 取值 只定义 光照度，
# 分支 却用 $number(温度)），运行态该字段为 undefined → 条件恒假 → 真分支永不触发、
# 动作永不执行，且编译/gate 均不报错（典型"静默逻辑 bug"，WB16 iss_0a831a1760）。
# 本规则扫 switch 的 jsonata 规则，抽取裸字段引用（排除 $-函数、payload./flow./msg. 限定、
# JSONata 关键字、字面量），凡不在「已声明字段集」内即 warning 提示（不硬拦，避免误伤合法表达式）。
_JSONATA_KEYWORDS = {
    "true", "false", "and", "or", "not", "null", "if", "then", "else", "in",
    "function", "payload", "msg", "flow", "global",
}
_FIELD_TOKEN_RE = re.compile(r"(?<![.\w$一-鿿])([A-Za-z_一-鿿][\w一-鿿]*)(?![一-鿿\w])")


def _collect_defined_fields(nodes: List[Dict[str, Any]]) -> set:
    """收集编译产物中「已声明可用」的字段名：取值字段(payload.<x>) + 变量(flow.<x>)。"""
    defined: set = {"state"}  # payload.state 由取值节点恒提供
    for n in nodes:
        if n.get("type") == "api-current-state":
            for op in (n.get("outputProperties") or []):
                p = op.get("property") or ""
                if p.startswith("payload."):
                    if p != "payload.state":
                        defined.add(p[len("payload."):])
                elif p and p != "payload" and "." not in p:
                    # 手写/白箱 flow 常直接写 msg.<字段>（不带 payload. 前缀），
                    # 同样是**已声明**。漏收会让 R31 误报——而闸门(G3)现在会据此
                    # 把分支判为恒假，误报代价从「多一条 warning」升级成
                    # 「本来正确的分支被判永不执行」，故必须收全。
                    defined.add(p)
        elif n.get("type") == "change":
            for r in (n.get("rules") or []):
                p = r.get("p")
                if not p:
                    continue
                if r.get("pt") == "flow":
                    defined.add(p)
                elif r.get("pt") in (None, "", "msg"):
                    if p.startswith("payload."):
                        defined.add(p[len("payload."):])
                    elif p != "payload" and "." not in p:
                        defined.add(p)
    return defined


def collect_undefined_field_refs(
        nodes: List[Dict[str, Any]]) -> Dict[str, Dict[int, List[str]]]:
    """R31 的**结构化**索引：`{switch节点id: {规则下标: [未定义字段名, ...]}}`。

    与 `_lint_undefined_field_ref`（每节点只报一条、面向人读）不同，本函数**逐规则**
    列全，供**闸门**消费：闸门需要知道「具体是第几条规则恒假」，才能在重放时把该
    分支判为**不命中**（而不是对无法本地求值的 JSONata 一律保守视为命中）。

    语义依据：`分支: 状态.光照 < 22` 里 `状态.光照` 从未被 `取值:`/`变量:` 声明 →
    运行态 JSONata 求值得 undefined → NR switch 该规则**不命中** → THEN 体永不执行。
    这是**静态可判定**的恒假，闸门必须与编译器结论一致（G3 / 报告 A30 闸门侧）。
    """
    defined = _collect_defined_fields(nodes)
    idx: Dict[str, Dict[int, List[str]]] = {}
    for n in nodes:
        if n.get("type") != "switch":
            continue
        nid = n.get("id") or "?"
        for i, r in enumerate(n.get("rules") or []):
            if (r.get("t") or "") in ("else", "otherwise"):
                continue  # else 规则不是条件，编译器也给它带 vt=jsonata
            vt = r.get("vt") or r.get("t")
            if vt not in ("jsonata", "jsonata_exp"):
                continue
            expr = r.get("v") or ""
            toks: List[str] = []
            for m in _FIELD_TOKEN_RE.finditer(expr):
                tok = m.group(1)
                if tok in _JSONATA_KEYWORDS or tok in defined or tok in toks:
                    continue
                toks.append(tok)
            if toks:
                idx.setdefault(nid, {})[i] = toks
    return idx


def _lint_undefined_field_ref(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for nid, rules in collect_undefined_field_refs(nodes).items():
        i = min(rules)          # 单节点报一次足够：取下标最小的那条
        tok = rules[i][0]
        out.append({
            "level": "warning", "rule": "R31", "node_id": nid,
            "node_type": "switch",
            "message": (
                f"switch 第 {i + 1} 条分支引用了未定义的字段 `{tok}`"
                f"（取值/变量 均未声明该名）。运行态该字段为 undefined → 条件恒假、"
                f"动作永不执行。请确认 取值: 字段名 与 分支 引用一致，"
                f"或显式写成 payload.{tok}。"
            ),
        })
    return out


# ── R2：function 取值路径黑箱 + 扁平结构反模式 ──
_FUNCTION_PATH_RE = re.compile(r"msg\.payload(?:\.|\b)([A-Za-z_][\w]*)(?:\.([A-Za-z_][\w]*))?")


def _lint_function(n: Dict[str, Any], nid: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    func = n.get("func", "") or ""
    # function 是黑箱：引擎禁止生成它（黑箱 DSL），但白盒允许 → 仅提示无法静态校验
    out.append({
        "level": "info",
        "rule": "R2",
        "node_id": nid,
        "node_type": "function",
        "message": (
            "function 节点为黑箱，Linter 无法静态校验其读取的 payload 字段是否与上游 API "
            "真实返回结构一致。请确认取值路径正确——常见错误：真实 API 返回扁平结构"
            "（如 payload.faces[0].matched），函数却按嵌套结构读取（payload.object.faces...）。"
            "建议配合 A3 Schema Probe 固化真实响应结构。"
        ),
    })
    # 针对性反模式：读 payload.object.* （多数本地 API 是扁平的）
    for m in _FUNCTION_PATH_RE.finditer(func):
        seg1 = m.group(1)
        seg2 = m.group(2)
        if seg1 == "object":
            out.append({
                "level": "warning",
                "rule": "R2",
                "node_id": nid,
                "node_type": "function",
                "message": (
                    f"function 读取了 `msg.payload.object...`（命中于 `{m.group(0)}`）。"
                    "多数本地 API（ArcFace 等）返回**扁平**结构（faces 直接在 payload 下，"
                    "非 payload.object.faces）。请核对真实响应：字段路径错误会导致始终走异常/陌生人分支。"
                ),
            })
            break
    return out


# ── R4：change 节点 JSONata 语法预检 ──
def _lint_change(n: Dict[str, Any], nid: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in (n.get("rules") or []):
        # JSONata 取值/赋值
        if r.get("pt") == "jsonata" and r.get("p"):
            ok, msg = _check_jsonata(r["p"])
            if not ok:
                out.append({
                    "level": "warning", "rule": "R4", "node_id": nid,
                    "node_type": "change",
                    "message": f"change 节点属性 JSONata 语法疑似有误：{msg} —— 表达式 `{r['p']}`",
                })
        if r.get("tot") == "jsonata" and r.get("to"):
            ok, msg = _check_jsonata(r["to"])
            if not ok:
                out.append({
                    "level": "warning", "rule": "R4", "node_id": nid,
                    "node_type": "change",
                    "message": f"change 节点赋值 JSONata 语法疑似有误：{msg} —— 表达式 `{r['to']}`",
                })
    out.extend(_lint_change_jsonata_pitfalls(n, nid))
    return out


def _strip_single_quotes(s: str) -> str:
    """去掉单引号字符串内容（含 NR 的 '' 转义），保留裸表达式，便于检测危险结构。"""
    out = []
    in_s = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'":
            if in_s:
                if i + 1 < len(s) and s[i + 1] == "'":  # 转义的单引号 ''
                    i += 2
                    continue
                in_s = False
                i += 1
                continue
            in_s = True
            i += 1
            continue
        if in_s:
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ── R5/R6/R7/R9：change 节点 JSONata 反模式（2026-07-13 Bark 子流程排障沉淀）──
def _lint_change_jsonata_pitfalls(n: Dict[str, Any], nid: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in (n.get("rules") or []):
        # R36（round4 R8 / A28）：自赋值空规则 `msg.X = msg.X`。
        # 典型来源：http_api 子流程的 extract 恰好等于落点（payload.reply=payload.reply），
        # 或手写/白箱 deploy_raw 复制粘贴留下的空转规则。它不改变任何数据，
        # 却让链路看着「有处理」，排障时极具误导性。
        if (r.get("t") == "set" and r.get("pt", "msg") == "msg"
                and str(r.get("p") or "").strip()
                and str(r.get("p") or "").strip() == str(r.get("to") or "").strip()
                and r.get("tot") in ("jsonata", "msg")):
            out.append({
                "level": "warning", "rule": "R36", "node_id": nid,
                "node_type": "change",
                "message": (
                    f"change 节点存在**自赋值空规则** `msg.{r.get('p')} = msg.{r.get('to')}`："
                    f"目标属性与赋值表达式完全相同，运行时不改变任何数据（纯空转节点）。"
                    f"这类节点常来自「取返回值」模板与实际落点重合，或复制粘贴残留。"
                    f"请删除该规则；若本意是改名/搬移字段，请把 to 改成真正的源路径。"
                ),
            })
        if r.get("tot") != "jsonata":
            continue
        # p（目标字段）与 to（赋值表达式）都检查
        for field, label in (("p", "属性"), ("to", "赋值")):
            expr = r.get(field) or ""
            if not expr:
                continue
            # R5：$defined 在 Node-RED 内置 JSONata 中不存在
            if "$defined(" in expr:
                out.append({
                    "level": "error", "rule": "R5", "node_id": nid,
                    "node_type": "change",
                    "message": (
                        f"change 节点{label} JSONata 使用了 `$defined(...)`，但 Node-RED 内置 JSONata "
                        f"**没有** $defined 函数 → 运行期抛 'Attempted to invoke a non-function'。"
                        f"改用三元短路判断字段是否存在：`(field ? a : b)`（字段不存在=undefined=假值，走 else）。"
                        f"—— 表达式 `{expr}`"
                    ),
                })
            # R6：$flowContext('x') or ... 空对象毒化
            if "$flowContext(" in expr:
                out.append({
                    "level": "warning", "rule": "R6", "node_id": nid,
                    "node_type": "change",
                    "message": (
                        f"change 节点{label} JSONata 使用了 `$flowContext('键')`。实测当键不存在时它返回"
                        f"**空对象 {{}}**（JSONata 中算「真值」），会毒化 `or` 链把 {{}} 传下去，"
                        f"后续 `$encodeUrlComponent({{}})` 之类调用抛类型错。子流程默认值兜底应直接用 "
                        f"`x or '默认'`（必要时加 `& ''` 保底转字符串），不要用 $flowContext。"
                        f"—— 表达式 `{expr}`"
                    ),
                })
            # R7：裸全角括号（）→ Syntax error（须留在单引号字符串内）
            bare = _strip_single_quotes(expr)
            if "（" in bare or "）" in bare:
                out.append({
                    "level": "error", "rule": "R7", "node_id": nid,
                    "node_type": "change",
                    "message": (
                        f"change 节点{label} JSONata 的**裸表达式**里出现全角括号 `（）`，"
                        f"会被 JSONata 当成函数调用括号 → 'Syntax error'。要么把含全角括号的文本放进"
                        f"单引号字符串 `'...'` 内，要么改用半角括号。注意：字符串字面量整体要用单引号包裹。"
                        f"—— 表达式 `{expr}`"
                    ),
                })
            # R9：双引号字符串字面量（NR JSONata 应单引号）
            if '"' in bare:
                out.append({
                    "level": "warning", "rule": "R9", "node_id": nid,
                    "node_type": "change",
                    "message": (
                        f"change 节点{label} JSONata 里出现双引号 `\"...\"`。Node-RED 中 JSONata 字符串字面量"
                        f"**必须用单引号** `'...'`（双引号在 NR 的 JSON `to` 字段里需转义 `\\\"` 且极易出错）。"
                        f"请改为单引号。 —— 表达式 `{expr}`"
                    ),
                })
    return out


# ── R8：子流程定义 out/in 端口数组格式（防编辑器加载崩溃）──
def _lint_subflow_def(n: Dict[str, Any], nid: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for key in ("out", "in"):
        v = n.get(key)
        if v is None:
            continue
        if not isinstance(v, list):
            out.append({
                "level": "error", "rule": "R8", "node_id": nid,
                "node_type": "subflow",
                "message": (
                    f"子流程定义的 `{key}` 必须是端口对象数组（如 `[{{\"x\":0,\"y\":0,\"wires\":[[\"...\"]]}}]`），"
                    f"无端口时写 `[]`。实得类型 {type(v).__name__}。写成数字（如 `[1]`）会导致编辑器加载时"
                    f"`out[0].wires.forEach` 命中 undefined → 整页 'Cannot read properties of undefined "
                    f"(reading 'forEach')' 崩溃。"
                ),
            })
            continue
        for idx, port in enumerate(v):
            if not isinstance(port, dict) or "wires" not in port:
                out.append({
                    "level": "error", "rule": "R8", "node_id": nid,
                    "node_type": "subflow",
                    "message": (
                        f"子流程 `{key}` 的第 {idx} 个端口必须是含 `wires` 的对象，实得 `{port}`。"
                        f"写成 `[1]` 之类的数字会被编辑器在加载时抛 'Cannot read properties of undefined"
                        f" (reading 'forEach')'。"
                    ),
                })
    return out


# ── R3：http request bodyType=json 但上游无 payload setter ──
def _lint_http_request(
    n: Dict[str, Any], nid: str,
    rev: Dict[str, List[str]],
    nodes_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    method = (n.get("method") or "GET").upper()
    if method != "POST":
        return out
    body = n.get("body")
    bt = n.get("bodyType")
    # bodyType=json/urlencoded 且无字面 body → 请求体取自 msg.payload，需上游构造
    needs_payload = (bt in ("json", "urlencoded")) or (bt is None and not body)
    if not needs_payload:
        return out
    if body:  # 已有字面请求体（如 {} 或 JSON 字符串），视为已构造
        return out
    constructed = _upstream_has_payload_setter(nid, rev, nodes_by_id)
    if not constructed:
        out.append({
            "level": "warning",
            "rule": "R3",
            "node_id": nid,
            "node_type": "http request",
            "message": (
                "http request 节点 method=POST 且 bodyType=json，但上游没有任何 change/function/"
                "template 节点构造 msg.payload（请求体来源）。部署后该请求会以原始/空的 msg.payload "
                "作为请求体发出，极易触发 API 422/400。请在 http 前用「构建」/change 设置 msg.payload，"
                "或在该节点直接填写 body (JSON 对象)。"
            ),
        })
    return out


# ── R10：wire 结构校验（单 output 节点误用双数组）──
# 单 output 节点的 wires 必须是 [[...]]（一对多），写成 [["a"],["b"]] 会被 NR 当 2 output，
# output 1 永不触发（经典"只开灯不播报"根因）。仅真 2+ output 节点才用多数组。
_TRUE_MULTI_OUTPUT_TYPES = {
    "switch", "api-current-state", "server-state-changed",
    "catch", "status", "complete",
    "rbe", "delay", "trigger", "filter",
    # function 节点支持多 output（outputs:N 配置），多数组 wires 是合法分支路由，
    # 非"单 output 误用多数组"反模式。1880 prod 大量使用，漏列会误杀已知-good 流。
    "function",
}


def _lint_wire_structure(
    nodes: List[Dict[str, Any]],
    idset: set,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        wires = n.get("wires")
        if not isinstance(wires, list):
            continue
        # R17: 悬空 wire 引用（指向不存在的节点 id）
        for wi, wl in enumerate(wires):
            for tgt in _flat_wire_targets(wl):
                if tgt not in idset:
                    out.append({
                        "level": "error", "rule": "R17", "node_id": nid,
                        "node_type": ntype,
                        "message": (
                            f"节点 wires[{wi}] 引用了不存在的节点 id `{tgt}`。"
                            f"NR 会静默丢弃此连线，下游节点永不触发。"
                            f"常见原因：节点被删除但连线未清理、或 id 拼写错误。"
                        ),
                    })
        # R10b: 单 output 节点误用多数组 wires（R10 现专指此反模式；悬空引用已拆为 R17）
        # 用节点声明的 outputs 字段判断（function 可配置 outputs:N 合法多输出），
        # 而非只靠类型白名单——更准：声明 outputs>=2 的多数组是合法分支路由。
        declared = n.get("outputs", 1) or 1
        if declared < 2 and ntype not in _TRUE_MULTI_OUTPUT_TYPES and len(wires) > 1:
            non_empty = sum(1 for w in wires if w)
            if non_empty > 1:
                out.append({
                    "level": "error", "rule": "R10", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        f"单 output 节点 `{ntype}` 的 wires 有 {len(wires)} 个数组，但该节点声明 outputs={declared}。"
                        f"NR 会把多出来的数组当作第 2/3 output 的连线 → 第 2+ 数组里的目标节点"
                        f"**永不触发**（经典'只开灯不播报'坑）。"
                        f"单 output 一对多连线应写 `[['a','b']]`（一个数组多个目标），"
                        f"而非 `[['a'],['b']]`（两个数组各一个目标）。"
                        f"若确实要多输出，请显式设 outputs>=2。"
                    ),
                })
    return out


# ── R11：entity_id 格式校验 ──
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
# 含占位符/模板变量（{{ ... }} 或 REPLACE_WITH）的实体不校验——降噪，
# 避免误伤白箱模板实体（如 `light.{{room}}`、`{{payload.entityId}}`）。
_ENTITY_PLACEHOLDER = re.compile(r"(REPLACE_WITH|\{\{)")


def _lint_entity_id_format(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        # api-call-service: entityId 数组
        eid = n.get("entityId")
        if ntype in ("api-call-service", "api-current-state") and eid:
            targets = eid if isinstance(eid, list) else [eid]
            for t in targets:
                if not isinstance(t, str) or not t.strip():
                    continue
                if _ENTITY_PLACEHOLDER.search(t):
                    continue
                if not _ENTITY_ID_RE.match(t):
                    out.append({
                        "level": "warning", "rule": "R11", "node_id": nid,
                        "node_type": ntype,
                        "message": (
                            f"entity_id `{t}` 不符合 `domain.entity_name` 格式。"
                            f"HA 实体 id 必须是小写字母/数字/下划线，用点分隔 domain 和实体名"
                            f"（如 `light.living_room`）。格式错误会导致 HA 静默拒绝服务调用。"
                        ),
                    })
        # server-state-changed: entities.entity 数组
        if ntype == "server-state-changed":
            ents = n.get("entities") or {}
            for t in (ents.get("entity") or []):
                if not isinstance(t, str) or not t.strip():
                    continue
                if _ENTITY_PLACEHOLDER.search(t):
                    continue
                if not _ENTITY_ID_RE.match(t):
                    out.append({
                        "level": "warning", "rule": "R11", "node_id": nid,
                        "node_type": ntype,
                        "message": (
                            f"触发器 entity `{t}` 不符合 `domain.entity_name` 格式。"
                            f"格式错误会导致 NR 的 HA 节点永远不匹配任何状态变化。"
                        ),
                    })
    return out


# ── R19：HA 节点字段拼写守护（entityId vs entity_id / switch jsonata 运算符）──
# 针对 NR5 + node-red-contrib-home-assistant-websocket 0.80.x 的字段契约：
#   - api-current-state / api-call-service 的目标实体字段是 `entityId`（camelCase），
#     不是 `entity_id`（snake_case）。网关旧编译器曾误产 `entity_id` →
#     ha-websocket Joi schema 强制 `entityId` 缺失 → "ValidationError: entityId is required"，
#     节点直接变红报错、整条流瘫痪。
#   - switch 节点的 JSONata 规则运算符键是 `jsonata_exp`（注意带 _exp 后缀），
#     不是 `jsonata`。旧编译器误产 `jsonata` → `operators[rule.t] is not a function`，
#     switch 节点崩溃、后续分支全断。
# 这两条是网关编译器在 NR5 下的真实字段契约，静态 lint 即可在部署前拦下，
# 不必等到 NR 运行态才暴露。（仅查节点**顶层**字段；服务 data 内嵌的 entity_id 不误伤。）
def _lint_ha_node_field_typos(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        # 1) HA 实体字段：api-current-state / api-call-service 误用 entity_id
        if ntype in ("api-current-state", "api-call-service"):
            has_correct = "entityId" in n
            has_wrong = "entity_id" in n
            if has_wrong and not has_correct:
                out.append({
                    "level": "error", "rule": "R19", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        f"`{ntype}` 节点使用了 `entity_id` 字段，但 ha-websocket(NR5) 的契约字段是"
                        f"`entityId`（camelCase）。缺失 `entityId` 会触发 Joi 校验"
                        f"`ValidationError: \"entityId\" is required`，节点部署即报错。"
                        f"请改为 `entityId`。"
                    ),
                })
            elif has_wrong and has_correct:
                out.append({
                    "level": "warning", "rule": "R19", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        f"`{ntype}` 节点同时含 `entity_id`(snake_case) 与 `entityId`(camelCase)，"
                        f"前者是冗余/残留字段，NR5 只用 `entityId`。建议删掉 `entity_id`。"
                    ),
                })
        # 2) switch 节点的 JSONata 规则误用 `jsonata`（应为 jsonata_exp）
        if ntype == "switch":
            for i, r in enumerate(n.get("rules") or []):
                if isinstance(r, dict) and r.get("t") == "jsonata":
                    out.append({
                        "level": "error", "rule": "R19", "node_id": nid,
                        "node_type": ntype,
                        "message": (
                            f"switch 节点第 {i} 条规则使用了 `t: \"jsonata\"`，但 NR5 的运算符表键是"
                            f"`jsonata_exp`（带 _exp 后缀）。误用 `jsonata` 会在运行态抛"
                            f"`TypeError: operators[rule.t] is not a function`，导致分支崩溃。"
                            f"请改为 `jsonata_exp`。"
                        ),
                    })
    return out


# ── R20：HA 实体节点缺失/空白 entityId（白盒 escape hatch 盲区补丁）──
# 背景：R19 只拦「字段名写错」（`entity_id` snake_case 而非 `entityId`），
#   但白盒 agent 漏填整段 entityId（键不存在或值为空字符串）时，R19 抓不到，
#   而旧网关白盒 lint 仅阻塞 R13/R15，其余 error 照常放行 → 节点部署到 NR 后才在
#   运行态变红报错。R20 专门堵这个盲区。
# 判定：
#   - api-current-state：必须指定实体（否则该节点无意义），缺/空 entityId 且无 entity_id 兜底 → error（硬拦）
#   - api-get-history：必须指定实体（否则取不到任何历史、部署后静默失效），缺/空 entityId → error（硬拦，iss_d311744392）
#   - api-call-service：可为域级服务（如 homeassistant.turn_off 不带实体），缺/空 entityId → warning（仅告警，不阻塞）
# 与 R19 分工：R19 管「用了错误字段名 entity_id」，R20 管「整段实体缺失/空白」。
#   为避免重复报告，只要节点带 `entity_id` 键（无论值如何）一律交给 R19 处理，R20 跳过。
# homeassistant 域中「无需实体」的服务（其余 homeassistant.* 仍属实体级，缺实体须硬拦）。
# 注意：homeassistant.reload* 是前缀通配（reload_core_config / reload_yaml / reload...），
# 全部无需实体，在分支里用 startswith("reload") 处理。
_HA_NO_ENTITY_SERVICES = {
    "homeassistant.restart",
    "homeassistant.check_config",
    "homeassistant.stop",
}


def _entity_ref_valid(eid) -> bool:
    """entityId 是否有效引用。NR5 ha-websocket 中：
    - api-current-state 用单字符串 entityId；
    - api-call-service 用字符串数组 entityId（如 ["light.x"]）。
    两者皆有效；缺失/空白/空数组视为无效。"""
    if isinstance(eid, str):
        return eid.strip() != ""
    if isinstance(eid, list):
        return len(eid) > 0 and any(isinstance(x, str) and x.strip() for x in eid)
    return False


def _lint_ha_node_missing_entity(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        # server-state-changed 实体在 entities.entity（数组），其余在顶层 entityId
        if ntype == "server-state-changed":
            ents = n.get("entities") or {}
            raw_list = ents.get("entity") or []
            entityId = raw_list if isinstance(raw_list, list) else [raw_list]
            # 带 entity_id（错误字段名）的情况归 R19 处理，这里跳过避免刷屏
            if "entity_id" in n:
                continue
            has_valid = any(isinstance(x, str) and x.strip() for x in entityId)
            if has_valid:
                continue
            level = "error"
            tail = "（server-state-changed 必须指定监听实体，否则节点变红报错，已阻断部署）"
            out.append({
                "level": level, "rule": "R20", "node_id": nid,
                "node_type": ntype,
                "message": (
                    f"`{ntype}` 节点缺少有效的监听实体（entities.entity 为空）。"
                    f"该节点部署后会因 ConfigError: An entity is required 而报错。{tail}"
                ),
            })
            continue
        if ntype not in ("api-current-state", "api-call-service", "api-get-history"):
            continue
        # 带 entity_id（错误字段名）的情况归 R19 处理，这里跳过避免刷屏
        if "entity_id" in n:
            continue
        entityId = n.get("entityId")
        has_valid = _entity_ref_valid(entityId)
        if has_valid:
            continue
        # 到此处：entityId 缺失或空白，且未发现 entity_id 兜底
        if ntype == "api-get-history":
            level = "error"
            tail = ("（api-get-history 必须指定实体，否则取不到任何历史、"
                    "部署后静默失效，已阻断部署）")
        elif ntype == "api-current-state":
            level = "error"
            tail = "（api-current-state 必须指定实体，已阻断部署）"
        else:  # api-call-service
            # Bug2 修复：区分实体级/域级。实体级服务（light/switch/cover/climate/...）
            # 缺实体必须硬拦；域级服务（notify 目标在 params、homeassistant 域级
            # restart/reload 等无需实体）豁免，避免误伤正常流。
            r20_domain = n.get("domain")
            r20_service = n.get("service") or ""
            if r20_domain == "notify":
                continue  # notify 目标在 params，entityId 留空是设计内，不告警
            if r20_domain == "homeassistant" and (
                f"{r20_domain}.{r20_service}" in _HA_NO_ENTITY_SERVICES
                or r20_service.startswith("reload")
            ):
                continue  # homeassistant 域级服务无需实体，豁免
            level = "error"
            tail = ("（该 api-call-service 属实体级服务，必须指定 entityId，已阻断部署；"
                    "若确为域级服务请改用 notify 或 homeassistant 域级服务）")
        out.append({
            "level": level, "rule": "R20", "node_type": ntype, "node_id": nid,
            "message": (
                f"`{ntype}` 节点缺少有效的 `entityId`（NR5 ha-websocket 契约字段）。"
                f"当前 entityId={entityId!r} 无效，且未发现 `entity_id`(snake_case) 兜底。"
                f"该节点部署后将因无法定位实体而报错或失控。{tail}"
            ),
        })
    return out


# ── R24：server-state-changed 的 ifState 混入时长词（应拆进 for 等待）──
_DURATION_WORDS = ("持续", "分钟", "小时", "秒", "min", "mins", "sec", "secs", "hour", "hours")


def _lint_trigger_duration(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        if n.get("type") != "server-state-changed":
            continue
        ifstate = n.get("ifState")
        if not isinstance(ifstate, str) or not ifstate.strip():
            continue
        if any(w in ifstate for w in _DURATION_WORDS):
            for_val = n.get("for", "0")
            out.append({
                "level": "error", "rule": "R24", "node_id": nid,
                "node_type": "server-state-changed",
                "message": (
                    f"`server-state-changed` 节点的 `ifState`={ifstate!r} 混入了时长词"
                    f"（持续/分钟/小时/秒…）。持久等待应写成『触发: <实体> <状态> 持续N分钟』"
                    f"由编译器拆进 `for`（当前 for={for_val!r}），而非塞进 ifState——"
                    f"否则实体永远不会『变到带时长词的奇怪状态』，触发器永不触发。"
                    f"请改用 DSL『持续N分钟』原语或手动把时长移入 for。"
                ),
            })
    return out


# ── R12：缺少 z 字段（tab 引用）──
def _lint_missing_z(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    # 全局节点类型：不需要 z（它们不属于任何 tab）
    _GLOBAL_TYPES = {
        "tab", "subflow", "server", "global-config",
        "comment",  # comment 可以在 tab 内也可以全局
    }
    for n in nodes:
        ntype = n.get("type", "?")
        if ntype in _GLOBAL_TYPES:
            continue
        if ntype == "subflow":
            continue
        z = n.get("z")
        if not z:
            out.append({
                "level": "warning", "rule": "R12", "node_id": n.get("id") or "?",
                "node_type": ntype,
                "message": (
                    f"节点缺少 `z` 字段（tab 引用）。NR 会把无 `z` 的节点视为全局配置节点，"
                    f"它们不会出现在任何 flow tab 里 → 部署后'消失'。"
                    f"白盒 agent 产出的 flow 常漏写 z（NR 静默丢节点坑）。"
                ),
            })
    return out


# ── R25：comment 节点被当作消息中转（带 wires 或被接入主链）──
# 白盒原生手写的常见坑：把 comment 节点接进消息主链（inject→comment→debug）。
# Node-RED 的 comment 节点不转发消息，下游（debug）永不触发 → 静默逻辑断裂且零告警。
# 对应压测报告 Bug-3（iss_649c9aae1a, low）；报告建议 warning 级覆盖。
# 编译器侧（DSL→NR）已在 _emit_body/_Emitter 跳过 comment，故本规则只补齐「白盒原生手写」
# 这条无人值守的路径。检测两类：
#   (a) comment 节点自身 wires 非空（它试图向下游转发）
#   (b) 任何功能节点的 wires 把消息送入 comment 节点 id（它作为某条连线的目标）
def _flat_wire_ids(wires) -> List[str]:
    ids: List[str] = []
    for out_list in wires or []:
        if isinstance(out_list, list):
            ids.extend(t for t in out_list if isinstance(t, str))
        elif isinstance(out_list, str):
            ids.append(out_list)
    return ids


def _lint_comment_relay(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    comment_ids = {n.get("id") for n in nodes
                   if n.get("type") == "comment" and n.get("id")}
    if not comment_ids:
        return out
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        if ntype != "comment":
            # (b) 功能节点把消息送入 comment —— 消息到达 comment 后直接丢弃
            for target in _flat_wire_ids(n.get("wires")):
                if target in comment_ids:
                    out.append({
                        "level": "warning", "rule": "R25", "node_id": nid,
                        "node_type": ntype,
                        "message": (
                            f"节点 {nid}({ntype}) 的 wires 指向 comment 节点 {target}。"
                            f"comment 节点不转发消息，消息到达后直接丢弃，下游节点永不触发"
                            f"→ 静默逻辑断裂。请从主链移除该 comment 连线（comment 仅作可视化说明）。"
                        ),
                    })
            continue
        # (a) comment 节点自身带 outgoing wires —— 被当作中转节点
        # 注意：Node-RED 的 comment 节点 wires 常写成 [[]]（内层为空列表），外层长度为 1，
        # 但真实目标数为 0。必须用内层真实 target 数判断，否则会把 [[]] 误报成「带 1 条 outgoing wires」。
        wires = n.get("wires", []) or []
        targets = [t for w in wires if isinstance(w, list)
                   for t in w if isinstance(t, str) and t]
        if targets:
            out.append({
                "level": "warning", "rule": "R25", "node_id": nid,
                "node_type": "comment",
                "message": (
                    f"comment 节点 {nid} 带有 {len(targets)} 条 outgoing wires，"
                    f"被当作消息中转接入主链。Node-RED 的 comment 节点不转发消息，"
                    f"下游节点永不触发 → 静默逻辑断裂。请删除 comment 的连线，"
                    f"或改用 inject/link 等真实节点承载流程。"
                ),
            })
    return out


# ── R27：动作参数字面数值越界（WB4 #3, iss_50828738bb）──
# 编译器不拦截业务值域。此处做一层轻量、warning 级、白名单制的「字面数值」校验：
# 仅对 api-call-service 节点、dataType=="json"（字面 JSON，非 jsonata 表达式）、
# 参数是「字面数字」且命中已知 HA 值域表时检查。变量/表达式参数无法静态判定，跳过。
# 不硬拦——HA 在部署/e2e 阶段自己会校验，此处仅为提前预警明显手误（如 brightness_pct=99999）。
# 值域表刻意保持小且为 HA core 文档化范围，避免误伤合法 exotic 参数。
_NUMERIC_RANGE = {
    "brightness_pct": (0, 100),
    "brightness": (0, 255),
    "brightness_step": (0, 255),
    "brightness_step_ct": (0, 255),
    "volume_level": (0.0, 1.0),
    "percentage": (0, 100),
    "white_value": (0, 255),
    "hue": (0, 360),
    "saturation": (0, 100),
}


def _lint_numeric_range(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        if n.get("type") != "api-call-service":
            continue
        if n.get("dataType") != "json":
            continue  # jsonata 表达式为动态值，无法静态判定，跳过
        raw = n.get("data")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, (lo, hi) in _NUMERIC_RANGE.items():
            if key not in data:
                continue
            v = data[key]
            if isinstance(v, bool):
                continue  # bool 不算数值范围
            if isinstance(v, (int, float)) and not (lo <= v <= hi):
                out.append({
                    "level": "warning", "rule": "R27", "node_id": n.get("id", "?"),
                    "node_type": "api-call-service",
                    "message": (
                        f"api-call-service {n.get('action', '?')} 的参数 {key}={v} 超出常见值域 [{lo}, {hi}]。"
                        f"这通常是手误（如 brightness_pct 应在 0-100、volume_level 应在 0-1）。"
                        f"HA 在部署/e2e 阶段也会校验；若确为合法 exotic 值，可忽略此 warning。"
                    ),
                })
    return out


# ── R_SERVICE_PARAM：HA 服务「非法参数」静态校验（WB22 T1 / B1）──
# HA 的 api-call-service 节点在部署时不校验服务签名：若调用 climate.turn_on 却传 hvac_mode，
# HA 端**不报错**但**静默忽略该参数**——自动化「看似成功、实则从未按预期生效」，极难排查。
# 此处做精准、fail-open 的黑名单校验：仅命中「已知非法 (service,param) 组合」才报 error。
# 不接 HA /services 远程注册表（需异步拉取+缓存+爆炸半径大、且会误伤 exotic 集成）；
# 未知 action 或未知 param 一律跳过，绝不误伤合法动态写法。
# 黑名单刻意保持小、且均为 HA core 文档明确的服务签名（同 domain 错服务传参）。
_SERVICE_ILLEGAL_PARAMS = {
    # climate.turn_on/off 仅接受 entity_id；任何温控参数都属「错服务」
    "climate.turn_on": {
        "hvac_mode", "hvac_modes", "temperature", "target_temp_high",
        "target_temp_low", "current_temperature", "set_point",
    },
    "climate.turn_off": {
        "hvac_mode", "hvac_modes", "temperature", "target_temp_high",
        "target_temp_low", "current_temperature", "set_point",
    },
    # cover 开/关/停不含 position/tilt；那些属于 set_cover_position / set_cover_tilt_position
    "cover.open_cover": {"position", "percentage", "tilt_position", "tilt"},
    "cover.close_cover": {"position", "percentage", "tilt_position", "tilt"},
    "cover.stop_cover": {"position", "percentage", "tilt_position", "tilt"},
    # lock 的 lock/unlock 不含温控/亮度类参数
    "lock.lock": {"hvac_mode", "temperature", "brightness", "percentage"},
    "lock.unlock": {"hvac_mode", "temperature", "brightness", "percentage"},
    # media_player 播放控制不含温控/亮度
    "media_player.media_play": {"hvac_mode", "temperature", "brightness"},
    "media_player.media_pause": {"hvac_mode", "temperature", "brightness"},
    "media_player.media_stop": {"hvac_mode", "temperature", "brightness"},
}


def _lint_service_param(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        if n.get("type") != "api-call-service":
            continue
        action = n.get("action") or ""
        if "." not in action:
            continue  # 无 domain.service 形态（变量/表达式）→ 无法静态判定，跳过
        if n.get("dataType") == "jsonata":
            continue  # 动态表达式参数，无法静态判定，跳过（避免误伤）
        raw = n.get("data")
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except Exception:
                continue
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {}
        if not isinstance(data, dict):
            continue
        illegal = _SERVICE_ILLEGAL_PARAMS.get(action)
        if not illegal:
            continue
        bad = sorted(k for k in data if k in illegal)
        if bad:
            out.append({
                "level": "error", "rule": "R_SERVICE_PARAM",
                "node_id": n.get("id", "?"), "node_type": "api-call-service",
                "message": (
                    f"api-call-service 调用 {action} 传入了非法参数 {bad}。"
                    f"该服务签名不接受这些参数（HA 端会静默忽略，导致自动化形同虚设）。"
                    f"常见纠错：climate 的温控参数应走 climate.set_hvac_mode / climate.set_temperature；"
                    f"cover 的 position/tilt 应走 cover.set_cover_position / set_cover_tilt_position。"
                ),
            })
    return out


# ── R26：变量↔分支作用域一致性（白箱 raw 路径的 C2 守护）──
# 压测报告 C4 (iss_bbf90c6afa) 的剩余结构 lint：白盒手写 flow 里，change 节点把变量写到
# flow/global 作用域（pt=="flow"/"global"），但下游 switch 却从 msg 作用域读同名变量
# （propertyType=="msg" 且 property 为裸名，无 "." 嵌套）→ 变量永远 undefined → 死分支。
# 这是 C2 编译器缺陷在白盒路径的等价形态（C2 已在编译器层修复，此处补白盒闸门）。
# 仅 warning（不硬拦）：避免误伤合法「msg 裸字段」写法；且同名巧合罕见。
# 用「裸字段（无 "." 嵌套）」约束精确命中 C2 形态——真实 HA 读取几乎都是 msg.payload.x 嵌套，
# 不会误伤；只有 switch 直接读 msg.<var>（与 flow 变量同名）才告警。
def _leaf(name: str) -> str:
    return name.rsplit(".", 1)[-1] if name else ""


def _lint_var_branch_scope(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    # 1) 收集 change 写到 flow/global 作用域的变量裸名
    flow_vars: set = set()
    for n in nodes:
        if n.get("type") != "change":
            continue
        for r in (n.get("rules") or []):
            if r.get("pt") in ("flow", "global") and r.get("t") in ("set", "change", "move"):
                p = r.get("p")
                if isinstance(p, str) and p:
                    flow_vars.add(_leaf(p))
    if not flow_vars:
        return out
    # 2) 检查 switch 是否从 msg 作用域裸读同名变量
    for n in nodes:
        if n.get("type") != "switch":
            continue
        nid = n.get("id") or "?"
        reads = []
        # v1: 顶层 property / propertyType
        if n.get("propertyType") == "msg":
            prop = n.get("property")
            if isinstance(prop, str) and "." not in prop:
                reads.append(prop)
        # v2: 每条规则的 property / propertyType
        for r in (n.get("rules") or []):
            if r.get("propertyType") == "msg":
                rprop = r.get("property")
                if isinstance(rprop, str) and "." not in rprop:
                    reads.append(rprop)
        mismatch = sorted({name for name in reads if name in flow_vars})
        if mismatch:
            out.append({
                "level": "warning", "rule": "R26", "node_id": nid,
                "node_type": "switch",
                "message": (
                    f"switch 节点 {nid} 从 msg 作用域读取变量 {mismatch}，"
                    f"但同名变量仅由 change 节点写入 flow/global 作用域（跨触发持久）。"
                    f"switch 从 msg 永远读不到该变量 → 对应分支恒为死分支"
                    f"（C2 作用域错配的白盒形态）。请统一作用域：要么 change 把变量写到 msg 作用域，"
                    f"要么 switch 改为从 flow/global 作用域读取（propertyType 改 flow/global）。"
                ),
            })
    return out


# ── R13：孤立动作节点（无入边且非触发源）──
# 白盒 agent 漏连的典型坑：写了 api-call-service 却忘了接进主链（无任何连线指向它）。
# 部署后该服务调用永不执行，但 NR 静态合法、gate 虚拟重放也未必抓得出
# （实体最终状态可能"恰好正确"或分支没覆盖到）。这里做结构层兜底。
#
# 为什么只查 `api-call-service`：
#   在真实生产流（1880 导出 969 节点）上实测，function / debug / change / switch / link out
#   大量"无 wires 入边"却完全合法——它们由 `link in`/`link out` 的 `links` 字段或子流程
#   端口跨流触达，根本不走 `wires` 数组。把这些类型纳入会疯狂误报。
#   而 `api-call-service`（HA 服务调用）几乎从不是合法的"无输入起点"——它必须被某条
#   主链触发，漏连即 bug。1880 实测 0 个孤立 api-call-service，无假阳性。
#   同时做 link 感知：若某 api-call-service 由 `link out → link in → wires` 链触达，
#   视为有入边，不误报。
_ORPHAN_CHECK_TYPES = {
    "api-call-service",
    "ha-call-service",  # 历史别名/兼容
}


def _build_incoming(nodes: List[Dict[str, Any]]) -> set:
    """收集所有"存在入边"的节点 id：含 wires 入边 + link 链（link out → link in → wires）。"""
    incoming: set = set()
    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    for n in nodes:
        for out_list in n.get("wires", []) or []:
            for target in _flat_wire_targets(out_list):
                if target:
                    incoming.add(target)
    # link 链：link out 的 links 指向 link in 节点，link in 的 wires 再指向下游
    link_ins = {n.get("id"): n for n in nodes if n.get("type") == "link in"}
    for n in nodes:
        if n.get("type") != "link out":
            continue
        for lin_id in (n.get("links") or []):
            lin = link_ins.get(lin_id)
            if not lin:
                continue
            for out_list in lin.get("wires", []) or []:
                for target in _flat_wire_targets(out_list):
                    if target:
                        incoming.add(target)
    return incoming


def _lint_orphan_action_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    incoming = _build_incoming(nodes)

    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        if ntype.startswith("subflow:"):
            continue
        if ntype not in _ORPHAN_CHECK_TYPES:
            continue
        if nid in incoming:
            continue
        out.append({
            "level": "error",
            "rule": "R13",
            "node_id": nid,
            "node_type": ntype,
            "message": (
                f"孤立动作节点：类型 `{ntype}` 的节点没有任何入边（无连线指向它，"
                f"也不由 link 节点链触达），部署后该服务调用**永远不会执行**。"
                f"这通常是白盒 agent 漏连导致（如场景里写了开灯/开空调，"
                f"却只把分支连到了播报节点）。请将该节点接入主链。"
            ),
        })
    return out


# ── B1（R14）：不可达节点 / 死代码检测 ──
# 白盒 agent 手搓 flow 时，最典型的 bug 是「写了节点却忘了接进主链」——
# 不仅限于 api-call-service（R13 已覆盖），也可能漏连 function/change/switch/
# http request/link out 等，甚至整段子图都悬空。R14 做**正向全图可达性**：
# 从所有触发源（inject/server-state-changed/link in/...）出发 BFS，
# 任何不可达、且自身不是触发源/配置节点的消息流节点 = 死节点（dead code）。
def _lint_unreachable_nodes(
    nodes: List[Dict[str, Any]],
    fwd: Dict[str, List[str]],
    idset: set,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    # 入度
    indeg: Dict[str, int] = {nid: 0 for nid in idset}
    for src, nxts in fwd.items():
        for t in nxts:
            if t in idset:
                indeg[t] = indeg.get(t, 0) + 1
    # 根播种：已知触发源，或「零入度且非下游处理类型」（未知触发类型也当根，避免误报）
    reachable: set = set()
    queue: List[str] = []
    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        t = n.get("type", "?")
        if t in _ENTRY_TYPES:
            if nid not in reachable:
                reachable.add(nid)
                queue.append(nid)
        elif indeg.get(nid, 0) == 0 and t not in _DOWNSTREAM_TYPES:
            # 零入度的非下游类型（多为某种触发/源，未必都在 _ENTRY_TYPES 里枚举得到）
            if nid not in reachable:
                reachable.add(nid)
                queue.append(nid)
    while queue:
        cur = queue.pop()
        for nxt in (fwd.get(cur) or []):
            if nxt in idset and nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")
        # 跳过：触发源（自身为根）、配置/布局节点、R13 已独占的孤立动作类型
        if ntype in _ENTRY_TYPES:
            continue
        if ntype in _CONFIG_TYPES:
            continue
        if any(ntype.startswith(p) for p in _CONFIG_PREFIXES):
            continue
        if ntype in _R14_SKIP_TYPES:
            continue
        if nid in reachable:
            continue
        out.append({
            "level": "warning",
            "rule": "R14",
            "node_id": nid,
            "node_type": ntype,
            "message": (
                f"不可达节点（死代码）：类型 `{ntype}` 的节点从任何触发源（inject/触发器/"
                f"link in 等）出发都**无法被触达**，部署后该节点永远不执行。"
                f"白盒 agent 手搓 flow 时常见原因是「节点写好了但忘了接入主链」"
                f"（甚至可能整段子图都悬空）。请检查连线，或若该节点确为冗余请删除。"
            ),
        })
    return out


# ── B2（R15）：环检测（避免白盒手搓出死循环链）──
# msg 在某条 wire 链上绕回上游节点 → 消息会无限循环重入，NR 表现为卡死/高负载。
# 用强连通分量（SCC）判定：size≥2 的 SCC，或自环（节点 wire 指向自己）= 含环。
# 仅在本 flow 内部图（wires + link 链）上判定；跨 tab 的 link 不在图里，不误报。
def _find_cycle_nodes(fwd: Dict[str, List[str]], idset: set) -> set:
    """返回所有处于环中的节点 id（迭代式三色 DFS 找回边）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in idset}
    in_cycle: set = set()
    for start in idset:
        if color[start] != WHITE:
            continue
        # 迭代 DFS：栈帧 = (node, 邻居迭代器)；path 跟踪当前递归栈用于标记环成员
        stack = [(start, iter(fwd.get(start, [])))]
        color[start] = GRAY
        path = [start]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in idset:
                    continue
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(fwd.get(nxt, []))))
                    path.append(nxt)
                    advanced = True
                    break
                if color[nxt] == GRAY:
                    # 回边 → node..nxt 形成环，标记 path 上从 nxt 起的成员
                    if nxt in path:
                        idx = path.index(nxt)
                        for p in path[idx:]:
                            in_cycle.add(p)
                    advanced = True
                    break
                # 否则为 BLACK（已完结）跨边，忽略
            if not advanced:
                color[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()
    return in_cycle

def _lint_cycles(
    nodes: List[Dict[str, Any]],
    fwd: Dict[str, List[str]],
    idset: set,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    cycle = _find_cycle_nodes(fwd, idset)
    if not cycle:
        return out
    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    # 受控循环（含 delay/trigger/link 等节流节点）通常是「有意为之」的自触发队列/调度器，
    # 不报为错误（误报会淹没真实问题）。仅紧致环（无节流节点）= accidental 死循环才报 error。
    has_throttle = any(
        by_id.get(n, {}).get("type") in _THROTTLE_TYPES for n in cycle
    )
    if has_throttle:
        return []
    for nid in sorted(cycle):
        n = by_id.get(nid) or {}
        # 自环特判：该节点 wire 直接指向自己
        is_self = nid in (fwd.get(nid) or [])
        out.append({
            "level": "error",
            "rule": "R15",
            "node_id": nid,
            "node_type": n.get("type", "?"),
            "message": (
                "检测到**紧致环（死循环链）**：该节点处于一条无任何节流节点（delay/trigger/link）"
                "打断的消息回环中，msg 会无限重入，NR 表现为卡死/持续高负载。"
                + ("节点自身直接连回自己（自环）；" if is_self else "")
                + "白盒手搓 flow 时多因「反馈连线接错了上游节点」。请切断回环、"
                "或在循环内插入 delay/trigger 等节流节点。"
            ),
        })
    return out


# ── R23：事件环检测（自触发 / 经 HA 状态重入）──
# 与 R15（紧致 wire 环 = error）互补：R15 抓「消息在节点间无限重入」；
# R23 抓「事件经 HA 状态回灌」——例：server-state-changed 监听实体 E →
# 下游 api-call-service 又改了 E → E 状态变 → 再次触发同一触发器 → 无限事件环。
# 这种环在 wire 图里看不到（trigger→action 是 DAG），但运行时是死循环。
# 即便中间有 delay 节流（R15 会放过），事件环在语义上依然存在（只是变慢），
# 故 R23 不受 throttle 影响，统一报 warning（提示而非硬拦，避免误伤
# 「延迟后回写同实体」的合法写法）。可由 _LINT_BLOCK_RULES 升级为硬拦。
_TRIGGER_TYPES = {"server-state-changed", "api-current-state", "events: all"}


def _node_entities_watched(n: Dict[str, Any]) -> List[str]:
    """触发器节点监听的实体 id 列表。"""
    out: List[str] = []
    e = n.get("entityId") or n.get("entity_id")
    if isinstance(e, str) and e.strip():
        out.append(e.strip())
    return out


def _node_entities_modified(n: Dict[str, Any]) -> List[str]:
    """动作节点修改的实体 id 列表（api-call-service 等）。"""
    out: List[str] = []
    e = n.get("entityId") or n.get("entity_id")
    if isinstance(e, str) and e.strip():
        out.append(e.strip())
    data = n.get("data")
    if isinstance(data, str) and data.strip():
        try:
            d = json.loads(data)
        except Exception:
            d = {}
    elif isinstance(data, dict):
        d = data
    else:
        d = {}
    if isinstance(d, dict):
        for k in ("entity_id", "entityId"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip())
    return out


def _lint_event_loops(
    nodes: List[Dict[str, Any]],
    fwd: Dict[str, List[str]],
    idset: set,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    triggers = [n for n in nodes if n.get("type") in _TRIGGER_TYPES]
    for tr in triggers:
        tr_id = tr.get("id")
        watched = set(_node_entities_watched(tr))
        if not watched:
            continue
        # 正向 BFS 下游可达节点（fwd 已含 link 链，仅限本 flow idset）
        seen: set = set()
        stack = list(fwd.get(tr_id) or [])
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            cn = by_id.get(cur)
            if cn and cn.get("type") == "api-call-service":
                hit = watched & set(_node_entities_modified(cn))
                if hit:
                    out.append({
                        "level": "warning",
                        "rule": "R23",
                        "node_id": tr_id,
                        "node_type": tr.get("type", "?"),
                        "message": (
                            f"检测到**事件环（经 HA 状态重入）**：触发器监听实体 "
                            f"{sorted(hit)}，其下游 api-call-service 又修改了同一实体 → "
                            f"状态变化会再次触发本触发器，形成无限事件循环"
                            f"（即便中间有 delay 节流也只是变慢）。"
                            f"若非有意，请让动作改其它实体，或在触发器上加去抖条件"
                            f"（如仅当 state 从 off→on 时）避免自触发。"
                        ),
                    })
                    break  # 同一触发器已报，跳出避免重复
            stack.extend(fwd.get(cur) or [])
    return out


# ── R16：重复节点 id（同一 flow 内 id 撞车）──
# NR 导入/部署时若同一 flow 出现两个相同 id 的节点，NR 会**静默丢弃**其中一个
# （保留第一个），被丢的节点及其整段连线凭空消失，且静态合法、gate 虚拟重放也难抓。
# 单一真相源要求：节点 id 必须全局唯一。error 级（硬伤）。
def _lint_duplicate_ids(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: Dict[str, List[str]] = {}
    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        seen.setdefault(nid, []).append(n.get("type", "?"))
    for nid, types in seen.items():
        if len(types) <= 1:
            continue
        uniq = sorted(set(types))
        out.append({
            "level": "error",
            "rule": "R16",
            "node_id": nid,
            "node_type": "/".join(uniq),
            "message": (
                f"重复节点 id `{nid}`：同一 flow 内出现 {len(types)} 个相同 id 的节点"
                f"（类型：{', '.join(uniq)}）。NR 部署时会**静默丢弃**重复节点"
                f"（保留第一个），被丢的节点及其连线凭空消失，且静态合法、虚拟重放也难抓。"
                f"请为每个节点生成唯一 id（编译期应确保 id 不撞车）。"
            ),
        })
    return out


# ── R18：子流程定义端口未连接（死端口）──
# 子流程定义（type="subflow"）在其 `in`/`out` 数组里声明端口，每个端口的 `wires`
# 指向子流程**内部**的节点（in 端口连到首个内部节点，out 端口从某内部节点引出）。
# 若某个已声明的 in/out 端口 `wires` 为空 `[]` → 该端口是死端口：
#   - in 端口空：外部消息无法进入子流程内部（入口断连）；
#   - out 端口空：子流程内部该出口永远不向外部发消息。
# 这类问题编辑器不报错、部署后表现为"子流程部分功能失效"，属静态合法运行必错。
# 注意：R8 已校验端口数组格式；R18 补连通性。仅对 subflow **定义**节点生效，
# 子流程**实例**（type="subflow:xxx"）不在此查（其实例端口由 NR 据定义生成）。
def _lint_subflow_dead_ports(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        if n.get("type") != "subflow":
            continue
        nid = n.get("id") or "?"
        for key in ("in", "out"):
            ports = n.get(key)
            if not isinstance(ports, list):
                continue
            for idx, port in enumerate(ports):
                if not isinstance(port, dict):
                    continue
                wires = port.get("wires")
                if isinstance(wires, list) and len(wires) == 0:
                    is_in = (key == "in")
                    out.append({
                        "level": "error",
                        "rule": "R18",
                        "node_id": nid,
                        "node_type": "subflow",
                        "message": (
                            f"子流程定义的 `{key}` 第 {idx} 个端口**声明了但无任何连线**"
                            f"（wires 为空）。该端口是死端口："
                            f"{'外部消息无法进入子流程内部（入口断连）' if is_in else '子流程内部该出口永远不向外部发消息'}。"
                        f"请在该端口的 wires 里连上子流程内部的对应节点。"
                    ),
                })
    return out


# ── R21：switch 死分支（某条规则无输出连线）──
# switch 节点每个规则对应一个 output（wires[i] 为该规则的下游连线列表）。
# 若某规则 wires[i] 为空 → 该分支即便命中也不触发任何节点，是「静默死分支」。
# 注意：switch 常被用作过滤器（只连 matched 输出、unmatched 故意空连丢弃），
# 故本报告为 warning（提示而非硬拦），避免误伤合法的「丢弃分支」写法。
def _lint_switch_dead_branches(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    _SW_T = {
        "eq": "等于", "neq": "不等于", "lt": "小于", "lte": "小于等于",
        "gt": "大于", "gte": "大于等于", "btwn": "介于", "cont": "包含",
        "regex": "正则", "true": "为真", "false": "为假",
        "else": "否则(otherwise)", "jsonata": "JSONata",
        "head": "取头", "index": "按索引", "tail": "取尾",
    }
    for n in nodes:
        if n.get("type") != "switch":
            continue
        nid = n.get("id") or "?"
        rules = n.get("rules") or []
        if not rules:
            continue  # 空 rules 由 R22 报 error，避免重复
        wires = n.get("wires") or []
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            out_wires = wires[i] if i < len(wires) else None
            if not out_wires:  # 空列表 / 缺失 → 死分支
                rtype = rule.get("t", "?")
                label = _SW_T.get(rtype, str(rtype))
                out.append({
                    "level": "warning",
                    "rule": "R21",
                    "node_id": nid,
                    "node_type": "switch",
                    "message": (
                        f"switch 节点第 {i + 1} 条分支（规则类型：{label}）"
                        f"没有连出任何下游节点（wires 为空）。该分支即便命中也不会触发任何后续节点，"
                        f"等于一条静默死分支。若这是有意的「丢弃分支」可忽略；"
                        f"否则请为该分支连接下游，或删除多余分支。"
                    ),
                })
    return out


# ── R22：节点必填字段表（A 静态预检核心）──
# 文档 §5 A：白盒 agent 手写 JSON 绕过 NR 编辑器可视化校验，最常见漏填 = 必填字段缺失，
# 导致「静态合法、运行必错」。本规则按节点类型查必填字段：
#   error 级（不可运行，硬拦）：
#     - api-call-service / ha-call-service：缺 service（NR 报 "Service is not defined"）
#     - switch：rules 为空（NR 报 "no rules"）
#   warning 级（软提示，NR 允许但极可能是 bug）：
#     - http request：url 为空（将依赖 msg.url，上游未设则失败）
#     - change：rules 为空（节点什么都不做）
#     - inject：无任何自动触发（repeat/crontab/once 全空，仅手动点击注入）
# api-current-state 的 entityId 由 R20 独占（error），此处不重复。
def _lint_node_required_fields(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")

        if ntype in ("api-call-service", "ha-call-service"):
            svc = n.get("service")
            if not svc or not str(svc).strip():
                out.append({
                    "level": "error", "rule": "R22", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        f"{ntype} 节点缺少 `service` 字段（要调用的 HA 服务，如 light.turn_on）。"
                        f"NR 部署后会报 'Service is not defined' 并无法运行。请补全 service。"
                    ),
                })

        elif ntype == "switch":
            rules = n.get("rules")
            if not isinstance(rules, list) or len(rules) == 0:
                out.append({
                    "level": "error", "rule": "R22", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        "switch 节点 `rules` 为空（未配置任何分支规则）。"
                        "NR 部署后会报 'Switch node has no rules' 并无法运行。"
                        "请至少配置一条分支规则。"
                    ),
                })

        elif ntype == "http request":
            url = n.get("url")
            if not url or not str(url).strip():
                out.append({
                    "level": "warning", "rule": "R22", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        "http request 节点 `url` 为空。NR 会改从 msg.url 取地址；"
                        "若上游未设置 msg.url，该请求将以空地址发出并失败。"
                        "请直接填写 url，或确认上游已构造 msg.url。"
                    ),
                })

        elif ntype == "change":
            rules = n.get("rules")
            if not isinstance(rules, list) or len(rules) == 0:
                out.append({
                    "level": "warning", "rule": "R22", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        "change 节点 `rules` 为空（未配置任何 set/change 操作）。"
                        "该节点部署后什么都不做，通常是白盒 agent 漏填规则。请补全 rules。"
                    ),
                })

        elif ntype == "inject":
            repeat = n.get("repeat")
            crontab = n.get("crontab")
            once = n.get("once")
            has_auto = bool(repeat) or bool(crontab) or bool(once)
            if not has_auto:
                out.append({
                    "level": "warning", "rule": "R22", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        "inject 节点未配置任何自动触发（repeat/crontab/once 均为空），"
                        "仅能在编辑器里手动点击注入。若该节点意在「自动/周期触发」，"
                        "请设置 interval(repeat) 或 crontab；若确为手动触发节点可忽略。"
                    ),
                })

    return out


# ── R32：关键空参（编译过但必废）──
# 白名单式拦截「字段存在、但被清空 = 部署后静默失效 / 静默死节点」的关键参数。
# 与 R22 的分工：R22 查「必填字段缺失 / 类型错」（service / url / inject 触发等）；
# R32 专查「字段在、但值是空串 / 空数组 = 节点看似连好却什么都不做」——这正是本次
# 「节点参数没填、信息流却正常流过闸门」陷阱的根因。
#   - function：func 为空 / 纯空白 → error。空 function 返回 undefined，消息被静默终止，
#     整条链路断在该节点却无任何报错（编译 / 校验全过）。
#   - api-call-service / ha-call-service：domain 为空 / 纯空白 → error。domain 空时服务调用
#     变成「.<service>」，HA 运行态拒绝并报错，但静态 lint（R22 只查 service 字段）放过了它。
# 注：switch / change 的「空 rules」已由 R22 覆盖（switch=error、change=warning），此处不再
# 重复，避免同一节点双报。R32 只补 R22 没覆盖的两个死角。
def _lint_key_empty_params(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for n in nodes:
        nid = n.get("id") or "?"
        ntype = n.get("type", "?")

        if ntype == "function":
            func = n.get("func")
            if func is None or not str(func).strip():
                out.append({
                    "level": "error", "rule": "R32", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        "function 节点的 `func` 为空（没有任何代码）。该节点部署后返回 undefined，"
                        "消息会在这一节点被静默终止——链路看似连好、静态校验全过，但什么都不会发生。"
                        "请补全函数体（至少 `return msg;` 以显式透传），或删除该节点。"
                    ),
                })

        elif ntype in ("api-call-service", "ha-call-service"):
            domain = n.get("domain")
            if domain is None or not str(domain).strip():
                out.append({
                    "level": "error", "rule": "R32", "node_id": nid,
                    "node_type": ntype,
                    "message": (
                        f"{ntype} 节点的 `domain` 为空。服务调用会变成「.<service>」形式，"
                        "HA 在运行态拒绝并报错，但静态校验（R22 仅查 service 字段）放过了它——"
                        "这正是「参数没填、信息流却正常流过闸门」的陷阱。请补全 domain（如 light / switch）。"
                    ),
                })

    return out


# ── R33：整条流无 effectful 节点（纯 pass-through / stub）──
# 启发式 warning（不阻塞，logic 段 fail-open）。一条 flow 若所有节点都是「被动 / 结构性」节点
# （inject / debug / comment / link / status / catch / server-state-changed 触发，或
# function / change / switch 的空体、api-call-service 的空 domain 等），部署后对环境不产生任何
# 可观察影响——通常是未完成的草稿（stub）或「参数全空」的流。R32 已逐节点拦空参，R33 从整流
# 视角兜底：即便每个节点单看「合法」，拼起来仍是一潭死水。
_EFFECTFUL_TYPES = {
    "api-call-service", "ha-call-service",   # 调用 HA 服务（副作用）
    "http request",                            # 发 HTTP 请求
    "api-current-state", "api-get-history",    # 读状态 / 历史（驱动下游）
    "delay",                                   # 延时（时间副作用）
    "template", "api-render-template",         # 渲染
    "mqtt out", "tcp out", "udp out", "websocket out", "exec", "email", "push",  # 输出类
    "change", "switch",                        # 改消息 / 路由（需非空 rules 才算有效，见下）
    "function",                                # 跑代码（需非空 func 才算有效，见下）
}


def _lint_noop_flow(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not nodes:
        return []
    for n in nodes:
        ntype = n.get("type", "?")
        # 子流程实例（subflow:xxx）一定做了实事
        if ntype.startswith("subflow:") or ntype == "subflow":
            return []
        if ntype in _EFFECTFUL_TYPES:
            # function / change / switch 需非空体才算真有效果
            if ntype == "function":
                if str(n.get("func") or "").strip():
                    return []
                continue
            if ntype in ("change", "switch"):
                rules = n.get("rules")
                if isinstance(rules, list) and rules:
                    return []
                continue
            if ntype in ("api-call-service", "ha-call-service"):
                # domain 或 service 任一为空 → 该节点其实无效，不算 effectful
                if not str(n.get("domain") or "").strip() or not str(n.get("service") or "").strip():
                    continue
                return []
            # 其余 effectful 类型（http / 状态读 / 延时 / 输出等）一律算有效
            return []
    # 遍历完没有任何 effectful 节点
    return [{
        "level": "warning", "rule": "R33", "node_id": "",
        "node_type": "flow",
        "message": (
            "该 flow 没有任何会产生实际效果（调用服务 / 读取状态 / 改写消息 / 路由 / 延时 / 输出等）的节点，"
            "部署后不会对环境产生任何可观察影响。这通常是未完成的草稿（stub）或「参数全空」的流。"
            "请确认是否漏接了动作节点；若确为纯观测 / 调试流可忽略。"
        ),
    }]
