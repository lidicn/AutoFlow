"""DSL 编译器不变量断言库（Tier1 普查 harness 的底层工具，非 test_ 前缀，不被 run_tests.py 单独收集）。

用途：把「编译产物应当永远成立」的结构性不变量抽成一组纯函数，每个返回
该不变量在当前 flow 上的违规字符串列表（空 = 通过）。Tier1 矩阵测试与
Tier2 golden 测试都复用本库，避免断言逻辑散落多处、口径不一致。

设计原则：
- 不变量聚焦「编译器生成的节点结构」：连线形态 / name 必填 / 实体已解析 /
  节点类型契约 / 时长不污染 ifState / 绝不生成 Function 等。
- 每个违规都带 node_id + 字段，便于 P3 直接归因到源码行。
- 不变量之间允许重叠（同一畸形可能被多条命中），冗余是好事；P3 汇总时
  按 (case, 主不变量) 归类，重叠项视为同一根因。
- 本库只断言「正确 DSL 编译出的 flow 必须成立」的事；语义缺口 / 解析错误
  由 compile_dsl 在编译期高声拒绝，不在本库范围内。

运行依赖：系统 Python 3.13.2（与网关同运行时），import autoflow_gateway 包。
"""

import json
import re

from autoflow_gateway.dsl_engine import compile_dsl, DSLError, HA_SERVER_ID
from autoflow_gateway.flow_linter import lint_flow

# ── 常量（与 flow_linter._DURATION_WORDS / _TRUE_MULTI_OUTPUT_TYPES 对齐）──
DURATION_WORDS = ("持续", "分钟", "小时", "秒", "min", "mins", "sec", "secs", "hour", "hours")
MULTI_OUTPUT_TYPES = {
    "switch", "api-current-state", "server-state-changed",
    "catch", "status", "complete", "rbe", "delay", "trigger", "filter", "function",
}
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
ENTITY_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
PLACEHOLDER_RE = re.compile(r"^(REPLACE_WITH|{{)")


# ── 基础访问器 ────────────────────────────────────────────────────────────
def compile_check(text: str, target: str = "staging") -> dict:
    """便捷封装：DSL 文本 → flow（dict）。失败时抛 DSLError（已带行号）。"""
    return compile_dsl(text, target=target)


def by_type(flow: dict, t: str) -> list:
    return [n for n in flow["nodes"] if n.get("type") == t]


def id_set(flow: dict) -> set:
    return {n["id"] for n in flow["nodes"] if n.get("id")}


# ── 不变量函数（每个返回违规字符串列表，空=通过）─────────────────────────
def inv_no_function(flow, target="staging", text="") -> list:
    """铁律 §18.3：编译器绝不生成 function 节点（含 escape hatch 也禁用）。"""
    out = []
    for n in flow["nodes"]:
        if n.get("type") == "function":
            out.append(f"[R-铁律] 出现 function 节点 id={n.get('id')}：编译器绝不应生成 Function")
    return out


def inv_unique_ids(flow, target="staging", text="") -> list:
    """节点 id 必须全局唯一（否则 NR POST /flow 报 duplicate id）。"""
    out = []
    seen = {}
    for n in flow["nodes"]:
        nid = n.get("id")
        if nid in seen:
            out.append(f"[id] 节点 id 重复：{nid}（前次出现 type={seen[nid]}）")
        else:
            seen[nid] = n.get("type")
    return out


def inv_id_prefix(flow, target="staging", text="") -> list:
    """节点 id 必须以 flow_id 前缀（af_scene_*）开头，保证跨 flow 全局唯一。"""
    out = []
    for n in flow["nodes"]:
        nid = n.get("id", "")
        if not nid.startswith("af_scene_"):
            out.append(f"[id] 节点 id 缺少 af_scene_ 前缀：{nid}（type={n.get('type')}）")
    return out


def inv_node_name(flow, target="staging", text="") -> list:
    """每个节点必须带非空的 name（可视化/可读/部署审计需要）。"""
    out = []
    for n in flow["nodes"]:
        nm = n.get("name")
        if not isinstance(nm, str) or not nm.strip():
            out.append(f"[name] 节点缺 name：id={n.get('id')} type={n.get('type')}")
    return out


def inv_wiring(flow, target="staging", text="") -> list:
    """连线形态不变量（捕获 R10 经典反模式 + R17 悬空 + 多数组误用）：
    1. wires 必须是「数组的数组」：任一元素不能是裸字符串。
    2. 每个 wire 目标 id 必须存在于本 flow（无悬空引用）。
    3. 单 output 节点（declared outputs==1 且非多输出类型）必须只有 1 个 wire 数组。
    4. wire 数组数不得超过节点声明 outputs（多余数组 NR 忽略，属结构混乱）。
    """
    out = []
    ids = id_set(flow)
    for n in flow["nodes"]:
        nid = n.get("id", "?")
        wires = n.get("wires")
        if not isinstance(wires, list):
            continue
        for wi, wl in enumerate(wires):
            if not isinstance(wl, list):
                out.append(f"[wire] 节点 {nid}({n.get('type')}) wires[{wi}] 是裸字符串而非数组"
                           f"（经典 R10 反模式：应写 [['a','b']] 而非 [['a'],['b']]）")
                continue
            for tgt in wl:
                if not isinstance(tgt, str) or tgt not in ids:
                    out.append(f"[wire] 节点 {nid}({n.get('type')}) wires[{wi}] 引用悬空 id `{tgt}`"
                               f"（R17：NR 静默丢弃该连线，下游永不触发）")
        declared = n.get("outputs", 1) or 1
        ntype = n.get("type", "?")
        if declared == 1 and ntype not in MULTI_OUTPUT_TYPES:
            if len(wires) > 1:
                out.append(f"[wire] 单 output 节点 {nid}({ntype}) 有 {len(wires)} 个 wire 数组"
                           f"（R10：应合并为 [['a','b']]）")
        # 多 output 节点：wire 数组数不得超过声明 outputs
        if len(wires) > max(declared, 1):
            out.append(f"[wire] 节点 {nid}({ntype}) wire 数组数 {len(wires)} > 声明 outputs {declared}"
                       f"（多余数组 NR 忽略，结构混乱）")
    return out


def inv_ssc(flow, target="staging", text="") -> list:
    """server-state-changed 节点（prod 触发）契约：
    - server 必须是占位符 REPLACE_WITH_HA_SERVER（部署时由网关替换）。
    - entities.entity 是非空字符串列表。
    - ifState（若有）不得含时长词（R24：持久等待应拆进 for，而非污染 ifState）。
    - 若 for 非空且 != "0"：forType=num / forUnits=minutes / for 为合法数值。
    - 若有 ifState：ifStateOperator=is / ifStateType=str。
    """
    out = []
    for n in by_type(flow, "server-state-changed"):
        nid = n.get("id", "?")
        if n.get("server") != HA_SERVER_ID:
            out.append(f"[ssc] 节点 {nid} server 不是占位符（={n.get('server')!r}）")
        ents = (n.get("entities") or {}).get("entity") or []
        if not isinstance(ents, list) or not ents or not all(isinstance(e, str) and e.strip() for e in ents):
            out.append(f"[ssc] 节点 {nid} entities.entity 非法：{ents!r}")
        ifs = n.get("ifState")
        if ifs:
            low = str(ifs).lower()
            hit = [w for w in DURATION_WORDS if w in low]
            if hit:
                out.append(f"[ssc] 节点 {nid} ifState={ifs!r} 含时长词 {hit}（R24：应拆进 for）")
            if n.get("ifStateOperator") != "is":
                out.append(f"[ssc] 节点 {nid} ifStateOperator={n.get('ifStateOperator')!r} 应为 'is'")
            if n.get("ifStateType") != "str":
                out.append(f"[ssc] 节点 {nid} ifStateType={n.get('ifStateType')!r} 应为 'str'")
        fv = n.get("for")
        if fv not in (None, "", "0"):
            if n.get("forType") != "num":
                out.append(f"[ssc] 节点 {nid} for={fv!r} 但 forType={n.get('forType')!r} 应为 'num'")
            if n.get("forUnits") != "minutes":
                out.append(f"[ssc] 节点 {nid} for={fv!r} 但 forUnits={n.get('forUnits')!r} 应为 'minutes'")
            try:
                float(str(fv))
            except Exception:
                out.append(f"[ssc] 节点 {nid} for={fv!r} 不是合法数值（空串会触发 NR Invalid config）")
    return out


def inv_entity_id(flow, target="staging", text="") -> list:
    """实体引用已解析、非空：
    - api-current-state：entityId 是非空字符串（不是空串/None/列表）。
    - api-call-service：entityId 是列表（notify 域可空列表，其余含 1 个实体）。
    - server-state-changed：entities.entity 非空列表（与 inv_ssc 互补，staging 无 SSC 时跳过）。
    """
    out = []
    for n in by_type(flow, "api-current-state"):
        nid = n.get("id", "?")
        eid = n.get("entityId")
        if not isinstance(eid, str) or not eid.strip():
            out.append(f"[entity] api-current-state {nid} entityId 非法：{eid!r}（应为已解析实体字符串）")
    for n in by_type(flow, "api-call-service"):
        nid = n.get("id", "?")
        eid = n.get("entityId")
        if not isinstance(eid, list):
            out.append(f"[entity] api-call-service {nid} entityId 不是列表：{eid!r}")
    for n in by_type(flow, "server-state-changed"):
        nid = n.get("id", "?")
        ents = (n.get("entities") or {}).get("entity") or []
        if not isinstance(ents, list) or not ents:
            out.append(f"[entity] server-state-changed {nid} entities.entity 为空：{ents!r}")
    return out


def inv_action(flow, target="staging", text="") -> list:
    """api-call-service 契约：
    - action == "{domain}.{service}"；domain/service 非空。
    - data 是合法 JSON 字符串、dataType=="json"（NR 字段契约）。
    - server == 占位符。
    """
    out = []
    for n in by_type(flow, "api-call-service"):
        nid = n.get("id", "?")
        dom, svc = n.get("domain"), n.get("service")
        if not dom or not svc:
            out.append(f"[action] api-call-service {nid} domain/service 缺失：{dom!r}/{svc!r}")
            continue
        if n.get("action") != f"{dom}.{svc}":
            out.append(f"[action] api-call-service {nid} action={n.get('action')!r} 应为 '{dom}.{svc}'")
        if n.get("dataType") != "json":
            out.append(f"[action] api-call-service {nid} dataType={n.get('dataType')!r} 应为 'json'")
        raw = n.get("data", "")
        try:
            json.loads(raw)
        except Exception:
            out.append(f"[action] api-call-service {nid} data 不是合法 JSON：{raw!r}")
        if n.get("server") != HA_SERVER_ID:
            out.append(f"[action] api-call-service {nid} server 不是占位符：{n.get('server')!r}")
    return out


def inv_switch(flow, target="staging", text="") -> list:
    """switch 节点契约：outputs==len(rules)；每条 rule 至少含 t；property 为字符串；
    wires 数组数不超过 outputs。

    注意：本不变量只查「结构」（outputs 与 rules 数一致、rule 有运算符 t）。
    不对 rule 的 v/vt 完整性做强制——原生节点(escape hatch)的 switch 由 agent 手写，
    其 rule 字段契约（含 else 规则缺 v/vt）不在编译器职责内，硬查会误报。"""
    out = []
    for n in by_type(flow, "switch"):
        nid = n.get("id", "?")
        rules = n.get("rules") or []
        if not isinstance(rules, list) or not rules:
            out.append(f"[switch] 节点 {nid} rules 非法/空：{rules!r}")
            continue
        if n.get("outputs") != len(rules):
            out.append(f"[switch] 节点 {nid} outputs={n.get('outputs')} != len(rules)={len(rules)}")
        for ri, r in enumerate(rules):
            if "t" not in r:
                out.append(f"[switch] 节点 {nid} rules[{ri}] 缺运算符字段 t：{r!r}")
        if not isinstance(n.get("property"), str):
            out.append(f"[switch] 节点 {nid} property 非字符串：{n.get('property')!r}")
        wires = n.get("wires") or []
        if len(wires) > max(n.get("outputs", 1), 1):
            out.append(f"[switch] 节点 {nid} wire 数组数 {len(wires)} > outputs {n.get('outputs')}")
    return out


def inv_no_orphans(flow, target="staging", text="") -> list:
    """孤儿节点检测（R13 类根因）：任一非触发源、非注释节点必须存在入边
    （有其它节点的 wires 指向它）。否则该节点永不触发（典型「分支首节点/嵌套门
    首节点漏连」——历史上 22 例真实提案皆此因）。

    合法根节点：inject / server-state-changed（触发器入口）。
    comment 节点不参与连线（仅可视化），恒为入度 0，豁免。"""
    out = []
    ROOTS = {"inject", "server-state-changed", "comment"}
    indeg = {n["id"]: 0 for n in flow["nodes"] if n.get("id")}
    for n in flow["nodes"]:
        for wl in (n.get("wires") or []):
            for tgt in (wl if isinstance(wl, list) else [wl]):
                if tgt in indeg:
                    indeg[tgt] += 1
    for n in flow["nodes"]:
        nid = n.get("id")
        if nid is None:
            continue
        ntype = n.get("type", "?")
        if ntype in ROOTS:
            continue
        if indeg.get(nid, 0) == 0:
            out.append(f"[orphan] 节点 {nid}({ntype}) 无入边（孤儿，永不触发）")
    return out


def inv_reachable(flow, target="staging", text="") -> list:
    """可达性（互补于 inv_no_orphans）：从触发根（inject/server-state-changed）
    BFS 遍历 wires，所有非注释节点必须可达。否则存在「孤岛」——某段逻辑被
    编译出来却完全没接到触发链上（典型：并行 fan-out / 多触发分配漏接）。"""
    out = []
    by_id = {n["id"]: n for n in flow["nodes"] if n.get("id")}
    roots = [n["id"] for n in flow["nodes"]
             if n.get("type") in ("inject", "server-state-changed")]
    seen = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for wl in (by_id[cur].get("wires") or []):
            for tgt in (wl if isinstance(wl, list) else [wl]):
                if tgt in by_id and tgt not in seen:
                    stack.append(tgt)
    for n in flow["nodes"]:
        nid = n.get("id")
        if nid is None:
            continue
        if n.get("type") == "comment":
            continue
        if nid not in seen:
            out.append(f"[reach] 节点 {nid}({n.get('type')}) 从触发根不可达（孤岛）")
    return out


def inv_delay(flow, target="staging", text="") -> list:
    """delay 节点契约：pauseType=="delay"；timeoutUnits=="milliseconds"；timeout 非空字符串且数值。"""
    out = []
    for n in by_type(flow, "delay"):
        nid = n.get("id", "?")
        if n.get("pauseType") != "delay":
            out.append(f"[delay] 节点 {nid} pauseType={n.get('pauseType')!r} 应为 'delay'")
        if n.get("timeoutUnits") != "milliseconds":
            out.append(f"[delay] 节点 {nid} timeoutUnits={n.get('timeoutUnits')!r} 应为 'milliseconds'")
        to = n.get("timeout")
        if not isinstance(to, str) or not to.strip():
            out.append(f"[delay] 节点 {nid} timeout 非法：{to!r}")
        else:
            try:
                float(to)
            except Exception:
                out.append(f"[delay] 节点 {nid} timeout={to!r} 不是数值")
    return out


def inv_time_range(flow, target="staging", text="") -> list:
    """time-range-switch 契约：类型必须是 'time-range-switch'（非 'time-range' 旧错名）；
    startTime/endTime 匹配 HH:MM；outputs==2；wires 数组数<=2。"""
    out = []
    for n in flow["nodes"]:
        nid = n.get("id", "?")
        t = n.get("type")
        if t == "time-range":  # 旧错名，未注册
            out.append(f"[timerange] 节点 {nid} 类型误用 'time-range'（应为 'time-range-switch'，否则部署即坏）")
        if t == "time-range-switch":
            for fld in ("startTime", "endTime"):
                v = n.get(fld)
                if not isinstance(v, str) or not TIME_RE.match(v):
                    out.append(f"[timerange] 节点 {nid} {fld}={v!r} 不匹配 HH:MM")
            if n.get("outputs") != 2:
                out.append(f"[timerange] 节点 {nid} outputs={n.get('outputs')} 应为 2")
            if len(n.get("wires") or []) > 2:
                out.append(f"[timerange] 节点 {nid} wire 数组数 {len(n.get('wires') or [])} > 2")
    return out


def inv_http(flow, target="staging", text="") -> list:
    """http request 节点契约：url 非空字符串；method 非空字符串；ret=="obj"。"""
    out = []
    for n in by_type(flow, "http request"):
        nid = n.get("id", "?")
        url = n.get("url")
        if not isinstance(url, str) or not url.strip():
            out.append(f"[http] 节点 {nid} url 非法：{url!r}")
        meth = n.get("method")
        if not isinstance(meth, str) or not meth.strip():
            out.append(f"[http] 节点 {nid} method 非法：{meth!r}")
        if n.get("ret") != "obj":
            out.append(f"[http] 节点 {nid} ret={n.get('ret')!r} 应为 'obj'")
    return out


def inv_subflow(flow, target="staging", text="") -> list:
    """子流程节点契约：
    - link out：links 是非空列表（指向真实 entry link id）。
    - subflow 实例：type 必须 'subflow:<id>'（带前缀，NR5 才识别）。
    """
    out = []
    for n in by_type(flow, "link out"):
        nid = n.get("id", "?")
        links = n.get("links")
        if not isinstance(links, list) or not links:
            out.append(f"[subflow] link out {nid} links 非法/空：{links!r}")
    for n in flow["nodes"]:
        if str(n.get("type", "")).startswith("subflow:"):
            nid = n.get("id", "?")
            if not re.match(r"^subflow:[A-Za-z0-9_]+$", str(n.get("type"))):
                out.append(f"[subflow] 实例节点 {nid} type={n.get('type')!r} 不符合 'subflow:<id>'")
    return out


def inv_lint(flow, target="staging", text="") -> list:
    """复用 flow_linter 做最终网检：编译产物不得含 error 级 issue
    （R5/R7/R8/R10/R13/R15/R17/R19/R20/R22/R24 等）。warning 级不计入。"""
    out = []
    try:
        issues = list(lint_flow({"nodes": flow["nodes"]}))
    except Exception as e:  # lint 异常不应让普查崩溃
        return [f"[lint] lint_flow 抛异常：{type(e).__name__}: {e}"]
    for i in issues:
        if i.get("level") == "error":
            out.append(f"[lint/{i.get('rule')}] {i.get('node_id','?')} {i.get('message','')}")
    return out


# ── 注册表（顺序即报告顺序）────────────────────────────────────────────────
INVARIANTS = {
    "no_function": inv_no_function,
    "unique_ids": inv_unique_ids,
    "id_prefix": inv_id_prefix,
    "node_name": inv_node_name,
    "wiring": inv_wiring,
    "ssc": inv_ssc,
    "entity_id": inv_entity_id,
    "action": inv_action,
    "switch": inv_switch,
    "no_orphans": inv_no_orphans,
    "reachable": inv_reachable,
    "delay": inv_delay,
    "time_range": inv_time_range,
    "http": inv_http,
    "subflow": inv_subflow,
    "lint": inv_lint,
}


def check_invariants(flow: dict, target: str = "staging", text: str = "", only=None) -> dict:
    """对单个 flow 跑全部（或指定）不变量，返回 {name: [violations]}。

    only: 可选不变量名集合/列表，限定只跑这些（便于 P3 细分）。"""
    result = {}
    names = only if only else INVARIANTS.keys()
    for name in names:
        fn = INVARIANTS[name]
        try:
            viol = fn(flow, target=target, text=text) or []
        except Exception as e:
            viol = [f"{name} 断言自身抛异常：{type(e).__name__}: {e}"]
        if viol:
            result[name] = viol
    return result


def check_dsl(text: str, target: str = "staging", only=None) -> dict:
    """端到端：编译 DSL → 跑不变量。编译失败抛 DSLError（由调用方决定是否为 bug）。"""
    flow = compile_check(text, target=target)
    return check_invariants(flow, target=target, text=text, only=only)


def all_violations(result: dict) -> list:
    """把 {name: [viol]} 拍平成字符串列表。"""
    out = []
    for name, viols in result.items():
        for v in viols:
            out.append(f"{name}: {v}")
    return out
