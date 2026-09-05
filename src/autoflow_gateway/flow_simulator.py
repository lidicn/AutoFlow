"""AutoFlow L2 逻辑预检仿真器（Phase B — 白箱跑通保证·逻辑层）。

补 L1 结构层（R13 孤儿动作 / R14 死代码 / R17 断线 / R22 必填）之后还抓不到的那一类坑：
  「动作节点接了线、结构合法，但逻辑上永远触达不到」——
  典型是 server-state-changed 配了 ifState="on"，动作却挂在 switch 的 "off" 分支下；
  或 switch 某条规则在给定触发语义下恒为假，下游动作永远不跑。

本模块对**白盒手搓 / 已编译的 Node-RED flow JSON** 做纯静态、零依赖的逻辑仿真：
  - 从所有触发源（server-state-changed / inject / api-current-state / link in ...）派生「触发场景」；
  - 用一个「尽力而为」的符号消息沿 wires（+link 链）传播；
  - switch 分支按已知值精确判定、未知值保守「可能真」放行；
  - 断言：是否存在某触发场景，使消息流到某个「动作终点」
    （api-call-service / ha-call-service / http request）。
  - 支持 virtual_states 注入（问卷共识#1）：把实体当前状态喂给 api-current-state / 触发条件比对，
    可证明某分支「在所有注入状态下都恒假」。

这与 flow_linter 互补：linter 看「节点写对没」，simulator 看「消息跑通没」。
simulate_flow 不部署、不触真实 HA/NR。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

try:
    from .flow_linter import _build_forward_graph, _ENTRY_TYPES, _flat_wire_targets
except ImportError:  # 允许作为独立脚本运行
    from flow_linter import _build_forward_graph, _ENTRY_TYPES, _flat_wire_targets


# 动作终点：消息流到这些节点 = flow 对外部世界产生了作用（调 HA 服务 / 发 HTTP）。
_ACTION_TYPES = {"api-call-service", "ha-call-service", "http request"}
# 仅作观测/转发的终节点（不参与「动作可达」断言，但用于覆盖率说明）。
_TERMINAL_TYPES = {"debug", "link out", "link out"}

_UNK = object()  # 符号「未知值」哨兵


# ── 符号消息 ──
def _new_msg(payload: Any = _UNK) -> Dict[str, Any]:
    return {"payload": payload}


def _get_path(msg: Dict[str, Any], path: str) -> Any:
    """从符号消息读 dotted 路径（payload / payload.x）。读不到返回 _UNK。"""
    if not isinstance(msg, dict):
        return _UNK
    cur = msg
    for part in path.split("."):
        if not isinstance(cur, dict):
            return _UNK
        if part in cur:
            cur = cur[part]
        else:
            return _UNK
    return cur


def _set_payload(msg: Dict[str, Any], value: Any) -> Dict[str, Any]:
    m = dict(msg)
    m["payload"] = value
    return m


def _coerce_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _as_str(v: Any) -> str:
    if v is _UNK:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# ── switch 分支评估 ──
def _eval_rule(rule: Dict[str, Any], msg: Dict[str, Any], virtual: Dict[str, Any]) -> Any:
    """返回 True / False / _UNK（未知→保守「可能真」）。

    property 默认 payload；operator 见 NR switch.operators。
    virtual 仅用于 api-current-state 注入后的 payload，已由调用方写入 msg，这里不直接读。
    """
    t = rule.get("t")
    if t == "else":
        return "else"  # 特殊标记，由调用方结合前置分支判定
    if t in ("true",):
        val = _get_path(msg, rule.get("property") or "payload")
        if val is _UNK:
            return _UNK
        return bool(val) is True
    if t in ("false",):
        val = _get_path(msg, rule.get("property") or "payload")
        if val is _UNK:
            return _UNK
        return bool(val) is False
    if t == "jsonata":
        return _UNK  # 无法静态求值的表达式 → 保守放行
    if t in ("head", "index", "tail"):
        return _UNK
    # 关系型运算符
    prop = rule.get("property") or "payload"
    val = _get_path(msg, prop)
    target = rule.get("value")
    vt = rule.get("valueType")
    # 简单类型归一
    if isinstance(target, str) and vt in (None, "str", "string"):
        target_v = target
    elif isinstance(target, (int, float)) and vt in (None, "num", "number"):
        target_v = target
    else:
        target_v = target
    if val is _UNK:
        return _UNK
    if t == "eq":
        return _as_str(val) == _as_str(target_v)
    if t == "neq":
        return _as_str(val) != _as_str(target_v)
    if t == "cont":
        sv, tv = _as_str(val), _as_str(target_v)
        return (tv in sv) or (sv in tv)
    if t == "regex":
        import re
        try:
            return re.search(str(target_v), _as_str(val)) is not None
        except re.error:
            return _UNK
    # 数值比较
    a, b = _coerce_num(val), _coerce_num(target_v)
    if a is None or b is None:
        # 转不成数 → 退化为字符串比较的 eq/neq 已由上面处理；其余按未知
        return _UNK
    if t == "lt":
        return a < b
    if t == "lte":
        return a <= b
    if t == "gt":
        return a > b
    if t == "gte":
        return a >= b
    if t == "btwn":
        v2 = rule.get("value2")
        b2 = _coerce_num(v2)
        if b2 is None:
            return _UNK
        lo, hi = (a, b2) if a <= b2 else (b2, a)
        return lo <= b <= hi
    return _UNK


# ── 触发源 → 初始符号消息 ──
def _entry_initial_msg(
    entry: Dict[str, Any], virtual: Dict[str, Any]
) -> Tuple[Dict[str, Any], str]:
    """返回 (初始 msg, 场景说明)。"""
    etype = entry.get("type", "?")
    if etype == "server-state-changed":
        ifs = entry.get("ifState")
        ifs_type = entry.get("ifStateType", "str")
        if ifs is not None:
            if ifs_type == "num":
                try:
                    payload: Any = float(ifs)
                except (ValueError, TypeError):
                    payload = ifs
            elif ifs_type == "bool":
                payload = str(ifs).lower() in ("true", "1", "on", "yes")
            else:
                payload = ifs
            note = f"实体状态变化命中 ifState={ifs!r} → 触发，payload={payload!r}"
        else:
            payload = _UNK
            note = "server-state-changed 无 ifState 约束 → 任意状态变化均触发（payload 未知）"
        return _new_msg(payload), note
    if etype == "api-current-state":
        eid = entry.get("entityId")
        target = eid[0] if isinstance(eid, list) and eid else eid
        if isinstance(target, str) and target in virtual:
            payload = virtual[target]
            note = f"api-current-state 注入虚拟状态 {target}={payload!r} → pass 分支 payload={payload!r}"
        else:
            payload = _UNK
            note = "api-current-state 轮询实体状态（未注入虚拟状态 → payload 未知）"
        return _new_msg(payload), note
    if etype == "inject":
        return _new_msg(_UNK), "inject 手动/周期触发（payload 未知）"
    if etype == "link in":
        return _new_msg(_UNK), "link in 收到上游 link out 消息（payload 未知）"
    return _new_msg(_UNK), f"{etype} 触发（payload 未知）"


# ── 节点对消息的变换（尽力而为）──
def _transform(node: Dict[str, Any], msg: Dict[str, Any]) -> Dict[str, Any]:
    """返回节点输出后的消息（更新 payload 等已知字段）。"""
    t = node.get("type", "?")
    if t == "change":
        m = dict(msg)
        for r in (node.get("rules") or []):
            if r.get("t") in ("set", "change") and (r.get("p") or "").startswith("payload"):
                to = r.get("to")
                tot = r.get("tot")
                if tot in (None, "str", "num", "json", "msg", "flow", "global"):
                    m["payload"] = to
        return m
    if t == "template":
        if (node.get("field") or "payload") == "payload":
            return _set_payload(msg, node.get("format") or node.get("template") or _UNK)
        return msg
    if t == "function":
        # 黑箱：无法静态求值 → payload 变未知
        return _set_payload(msg, _UNK)
    return msg


# ── 单场景传播 ──
def _propagate(
    entry_id: str,
    msg0: Dict[str, Any],
    fwd: Dict[str, List[str]],
    nodes_by_id: Dict[str, Dict[str, Any]],
    idset: set,
) -> Tuple[set, set, List[str]]:
    """从 entry 出发 BFS 传播。返回 (可达节点集, 达动作终点集, 说明)。"""
    reached: set = set()
    reached_actions: set = set()
    notes: List[str] = []
    # 栈帧: (node_id, 收到的 msg)
    stack: List[Tuple[str, Dict[str, Any]]] = [(entry_id, msg0)]
    seen_edges = set()
    while stack:
        cur, msg = stack.pop()
        if cur in reached:
            continue
        reached.add(cur)
        node = nodes_by_id.get(cur, {})
        ntype = node.get("type", "?")
        if ntype in _ACTION_TYPES:
            reached_actions.add(cur)
        # 先经过本节点变换，得到传出消息
        out_msg = _transform(node, msg)
        if ntype == "switch":
            rules = node.get("rules") or []
            wires = node.get("wires") or []
            any_prior_taken = False
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    continue
                res = _eval_rule(rule, out_msg, {})
                if res == "else":
                    taken = not any_prior_taken
                else:
                    taken = (res is True) or (res is _UNK)
                    if res is True:
                        any_prior_taken = True
                if taken:
                    targets = wires[i] if i < len(wires) else []
                    for tgt in _flat_wire_targets(targets):
                        if tgt in idset:
                            stack.append((tgt, out_msg))
                else:
                    notes.append(
                        f"switch 节点 {cur} 第 {i + 1} 条分支在当前场景下恒为假 → 该分支不触发"
                    )
            continue
        # 非 switch：沿所有出边传播（link 链已在 fwd 内）
        for tgt in (fwd.get(cur) or []):
            if tgt in idset:
                stack.append((tgt, out_msg))
    return reached, reached_actions, notes


# ── 主入口 ──
def simulate_flow(
    flow: Dict[str, Any],
    virtual_states: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对 flow JSON（{nodes:[...]}）做 L2 逻辑预检仿真。

    virtual_states: {entity_id: state_value} 虚拟状态注入，用于验证分支在真实状态下是否可达。

    返回结构化报告：
      ok                  bool  —— 无 error 级 logic issue
      logic_issues        [ {level, rule, node_id, node_type, message} ]
      scenarios           [ {entry_id, entry_type, fires, note, reached_actions} ]
      action_endpoints    [id...]  图中全部动作终点
      reachable_actions   [id...]  至少在一个触发场景下可达的动作终点
      unreachable_actions [id...]  任何场景都触达不到的动作终点
      summary             str
    """
    issues: List[Dict[str, str]] = []
    nodes = flow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return {
            "ok": True,
            "logic_issues": [],
            "scenarios": [],
            "action_endpoints": [],
            "reachable_actions": [],
            "unreachable_actions": [],
            "summary": "flow 无节点，跳过逻辑仿真",
        }

    nodes_by_id = {n.get("id"): n for n in nodes if n.get("id")}
    idset = set(nodes_by_id)
    fwd = _build_forward_graph(nodes, idset)
    virtual = virtual_states or {}

    entries = [n for n in nodes if n.get("type") in _ENTRY_TYPES]
    all_actions = [nid for nid, n in nodes_by_id.items() if n.get("type") in _ACTION_TYPES]

    scenarios: List[Dict[str, Any]] = []
    reachable_actions: set = set()

    if not entries:
        issues.append({
            "level": "warning",
            "rule": "L0",
            "node_id": "",
            "node_type": "",
            "message": (
                "flow 中没有任何已知触发源（inject / server-state-changed / "
                "api-current-state / link in / ...）。没有入口，消息永无起点 → "
                "整条流不会自己运行。请确认触发节点类型或接好 link in。"
            ),
        })
    else:
        for e in entries:
            eid = e.get("id")
            msg0, note = _entry_initial_msg(e, virtual)
            reached, reached_acts, notes = _propagate(
                eid, msg0, fwd, nodes_by_id, idset
            )
            reachable_actions |= reached_acts
            scenarios.append({
                "entry_id": eid,
                "entry_type": e.get("type"),
                "fires": True,
                "note": note,
                "reached_actions": sorted(reached_acts),
                "notes": notes,
            })

    # 核心断言：存在动作终点，但任何场景都触达不到
    unreachable = [a for a in all_actions if a not in reachable_actions]
    for a in unreachable:
        n = nodes_by_id.get(a, {})
        issues.append({
            "level": "error",
            "rule": "L1",
            "node_id": a,
            "node_type": n.get("type", "?"),
            "message": (
                f"动作终点 `{n.get('type')}`（id={a}）在所有模拟触发场景（含虚拟状态注入）下"
                f"都**触达不到**。它虽然接了线、结构合法，但逻辑上没有任何触发+状态组合能让消息流到它。"
                f"常见根因：触发 ifState 与 switch 分支取值不匹配、或分支在注入状态下恒为假。"
                f"该动作永远不会执行 → 这条自动化实际失效。"
            ),
        })

    # 无动作终点（观测/纯转发型）——warning 提示，不硬拦
    if not all_actions:
        issues.append({
            "level": "warning",
            "rule": "L2",
            "node_id": "",
            "node_type": "",
            "message": (
                "flow 中没有任何世界动作终点（api-call-service / http request）。"
                "若这是纯观测/日志/转发流可忽略；若是本应控制设备的自动化，"
                "说明动作节点缺失或未被接进主链。"
            ),
        })

    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warning"]
    summary = (
        f"逻辑预检：{len(entries)} 个触发源 / {len(all_actions)} 个动作终点；"
        f"可达 {len(reachable_actions)} 个、不可达 {len(unreachable)} 个；"
        f"error {len(errors)} / warning {len(warns)}。"
    )
    return {
        "ok": len(errors) == 0,
        "logic_issues": issues,
        "scenarios": scenarios,
        "action_endpoints": sorted(all_actions),
        "reachable_actions": sorted(reachable_actions),
        "unreachable_actions": sorted(unreachable),
        "summary": summary,
    }


# ── 离线自测（python flow_simulator.py）──
def _self_test() -> int:
    import os
    import sys

    py = sys.executable
    # 好 flow：t1 种子
    here = os.path.dirname(os.path.abspath(__file__))
    seed = os.path.normpath(os.path.join(
        here, "..", "..", "..", "..",
        "deepseek", "skill", "autoflow-white", "whitebox_golden",
        "t1_human_to_light.json",
    ))
    seed = os.path.normpath(seed)
    if os.path.exists(seed):
        with open(seed, "r", encoding="utf-8") as f:
            t1 = json.load(f)
        r = simulate_flow(t1)
        assert r["unreachable_actions"] == [], f"t1 不应有不可达动作，实得 {r['unreachable_actions']}"
        assert r["ok"], f"t1 应 ok，实得 logic_issues={r['logic_issues']}"
        print("[OK] t1 种子：动作可达，0 logic error")
    else:
        print(f"[SKIP] t1 种子不存在：{seed}")

    # 坏 flow：触发 ifState=on，但动作挂在 switch 的 eq off 分支下
    bad = {
        "nodes": [
            {
                "id": "trig", "type": "server-state-changed", "z": "t",
                "entities": {"entity": ["binary_sensor.x"]},
                "ifState": "on", "ifStateType": "str",
                "wires": [["sw"]],
            },
            {
                "id": "sw", "type": "switch", "z": "t",
                "property": "payload", "propertyType": "msg",
                "rules": [
                    {"t": "eq", "v": "off", "value": "off", "valueType": "str"},
                    {"t": "else"},
                ],
                "wires": [["act"], ["dbg"]],
            },
            {
                "id": "act", "type": "api-call-service", "z": "t",
                "service": "light.turn_on", "entityId": ["light.y"],
                "wires": [[]],
            },
            {"id": "dbg", "type": "debug", "z": "t", "wires": [[]]},
        ]
    }
    r = simulate_flow(bad)
    assert r["unreachable_actions"] == ["act"], f"坏 flow 应报 act 不可达，实得 {r['unreachable_actions']}"
    assert not r["ok"], "坏 flow 应 ok=False"
    print("[OK] 坏 flow：act 在所有场景触达不到 → logic error L1")

    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
