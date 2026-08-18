r"""回归：dsl_bug_hunt_round6.md 的 D8–D10 e2e 追踪缺陷。

  D8 · e2e 条件分支假阳性 —— switch 未命中分支被误判为 missing/断点
  D9 · e2e 不校验动作参数 —— api-call-service 的 data 未入报告、字符串化逃逸
  D10 · expected_path 匹配混乱 —— name 匹配不上、inject 过滤失效、reached/missing 矛盾
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway.gateway import Gateway

# object.__new__ 绕过 __init__（不触网），_compare_trace/_derive_planned_path
# 只依赖 self 方法 + 模块级常量，无需完整 Gateway 状态。
_gw = object.__new__(Gateway)


def _branch_flow():
    """inject → switch(分支) → a(then) / b(else)，a 带字符串化动态参数。"""
    return {"nodes": [
        {"id": "inj", "type": "inject", "name": "手动触发", "wires": [["sw"]]},
        {"id": "sw", "type": "switch", "name": "分支",
         "property": "payload",
         "rules": [{"t": "jsonata_exp", "v": "1 > 0", "vt": "jsonata"},
                   {"t": "else", "v": "true", "vt": "jsonata"}],
         "wires": [["a"], ["b"]]},
        {"id": "a", "type": "api-call-service", "name": "light.turn_on",
         "domain": "light", "service": "turn_on", "entityId": ["light.A"],
         "data": "{\"brightness_pct\": \"payload.brightness\"}", "dataType": "json",
         "wires": [[]]},
        {"id": "b", "type": "api-call-service", "name": "light.turn_off",
         "domain": "light", "service": "turn_off", "entityId": ["light.B"],
         "data": "{}", "dataType": "json", "wires": [[]]},
    ]}


def _compare(flow, trace, expected_path=None):
    return Gateway._compare_trace(_gw, flow, trace, expected_path)


# ── D8：条件分支未命中 ≠ 断点 ─────────────────────────────────────────────

def test_D8_unhit_else_branch_not_breakpoint():
    """switch 的 then 分支命中、else 未走 → verdict=通过，else 归 unhit_branches。"""
    flow = _branch_flow()
    r = _compare(flow, [{"node": "sw"}, {"node": "a"}])
    assert r["verdict"] == "通过", f"条件分支未命中不应断点：{r['verdict']} {r['missing']}"
    assert r["missing"] == [], f"else 未命中不应进 missing：{r['missing']}"
    assert r["unhit_branches"] == ["light.turn_off"], \
        f"else 应归 unhit_branches：{r['unhit_branches']}"


def test_D8_all_branches_unreached_still_breakpoint():
    """switch 本身未触发（两分支都没走）→ 仍判断点（真中断）。"""
    flow = _branch_flow()
    r = _compare(flow, [])
    assert r["verdict"] == "断点", "全分支未命中应保持断点"
    assert "light.turn_on" in r["missing"] or "light.turn_off" in r["missing"]


def test_D8_linear_flow_unchanged():
    """无 switch 的线性 flow：全部到达 → 通过；缺尾 → 断点。"""
    flow = {"nodes": [
        {"id": "inj", "type": "inject", "name": "t", "wires": [["x"]]},
        {"id": "x", "type": "api-call-service", "name": "light.turn_on",
         "domain": "light", "service": "turn_on", "entityId": ["light.A"],
         "data": "{}", "dataType": "json", "wires": [[]]},
    ]}
    assert _compare(flow, [{"node": "x"}])["verdict"] == "通过"
    assert _compare(flow, [])["verdict"] == "断点"


# ── D9：动作参数审计 ──────────────────────────────────────────────────────

def test_D9_stringified_param_detected():
    """dataType=json 但 data 含 "payload.x" 引号包裹的动态引用 → param_warnings。"""
    r = _compare(_branch_flow(), [{"node": "sw"}, {"node": "a"}])
    assert r["param_warnings"], "字符串化动态参数必须进 param_warnings"
    assert r["param_warnings"][0]["params"] == ["payload.brightness"]


def test_D9_param_audit_records_action_fields():
    """reached 的 api-call-service 输出 domain/service/entityId/data/dataType 快照。"""
    r = _compare(_branch_flow(), [{"node": "sw"}, {"node": "a"}])
    audit = [a for a in r["param_audit"] if a["node_id"] == "a"]
    assert audit, "param_audit 应含 api-call-service 条目"
    a0 = audit[0]
    assert a0["domain"] == "light" and a0["service"] == "turn_on"
    assert a0["dataType"] == "json" and "payload.brightness" in a0["data"]


def test_D9_clean_params_no_warning():
    """正常静态参数（brightness_pct: 80）→ 无 param_warnings。"""
    flow = _branch_flow()
    for n in flow["nodes"]:
        if n["id"] == "a":
            n["data"] = '{"brightness_pct": 80}'
    r = _compare(flow, [{"node": "sw"}, {"node": "a"}])
    assert r["param_warnings"] == [], "正常参数不应误报"


# ── D10：expected_path 匹配 ────────────────────────────────────────────────

def test_D10_expected_path_by_name():
    """显式 expected_path 用节点 name（含 inject name）→ 正确匹配、inject 被过滤。"""
    r = _compare(_branch_flow(), [{"node": "sw"}, {"node": "a"}],
                 ["手动触发", "分支", "light.turn_on"])
    assert r["verdict"] == "通过", f"name 写法应匹配成功：{r['missing']}"
    assert "手动触发" not in r["missing"], "inject（sink）不得进 missing"


def test_D10_expected_path_by_id():
    """显式 expected_path 用节点 id → 同样正确。"""
    r = _compare(_branch_flow(), [{"node": "sw"}, {"node": "a"}], ["sw", "a"])
    assert r["verdict"] == "通过", f"id 写法应匹配成功：{r['missing']}"


def test_D10_reached_missing_mutually_exclusive():
    """同一节点不得同时出现在 reached 与 missing。"""
    r = _compare(_branch_flow(), [{"node": "sw"}, {"node": "a"}],
                 ["手动触发", "分支", "light.turn_on"])
    reached_set = set(r["reached_ids"])
    missing_set = set(r["missing_ids"])
    assert not (reached_set & missing_set), \
        f"reached 与 missing 互斥被破坏：{reached_set & missing_set}"


def test_D10_expected_path_missing_reports_real_id():
    """真正未达的节点：missing_ids 是真实节点 id（非 name 字符串）。"""
    flow = _branch_flow()
    r = _compare(flow, [{"node": "sw"}], ["sw", "a", "b"])
    # a/b 均未走：b 是 unhit（sw 有其它分支？没有——sw 只命中 0 分支）→ a 进 missing
    assert r["missing_ids"] and all(x in {"a", "b"} for x in r["missing_ids"]), \
        f"missing_ids 应为真实 id：{r['missing_ids']}"
