"""嵌套双层 否则 误接线 bug 回归测试。

锁死一类已修的【静默编译错误】：当 DSL 同时出现两层 否则（内层 否则 + 外层 否则），
解析层上下文栈在 indent==0 的块关键字处未清掉仍压栈的内层 switch 帧，导致外层 否则
被误挂到内层 switch 上 —— 外层 switch 仅生成 1 个输出（缺 else）、外层 false 路径
完全不可达，且编译器不报错、闸门 gate_passed=true。

修复后预期：
  · 外层 switch 必须含 else 规则、outputs==2，外层 否则 子树上挂到外层 switch 的
    else 输出口（wires[1]）；
  · 内层 switch 的 wires 中【不得】出现外层 否则 节点（否则即误接线复发）。

运行：python tests/test_defect_nested_else.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile

NESTED_DOUBLE_ELSE = """
场景: 嵌套否则误接线复现
触发: inject
取值: sensor.temp x
分支: $number(x) > 25
  调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
  分支: payload.occurred == true
    观测: 内层真
  否则:
    观测: 内层假
否则:
  观测: 外层假
"""

SINGLE_ELSE = """
场景: 单层否则
触发: inject
取值: sensor.temp x
分支: $number(x) > 25
  观测: 大于25
否则:
  观测: 不大于25
"""

MULTIWAY_TOP = """
场景: 同级多路分支
触发: inject
取值: sensor.temp x
分支: $number(x) > 25
  观测: 高温
否则如果: $number(x) > 15
  观测: 中温
否则:
  观测: 低温
"""

NESTED_NO_OUTER_ELSE = """
场景: 嵌套无外层否则
触发: inject
取值: sensor.temp x
分支: $number(x) > 25
  调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
  分支: payload.occurred == true
    观测: 内层真
  否则:
    观测: 内层假
  观测: 收尾
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _switches(flow):
    return [n for n in flow["nodes"] if n["type"] == "switch"]


def _debug_id_by_name(flow, name):
    return next(n["id"] for n in flow["nodes"]
                if n.get("type") == "debug" and n.get("name") == name)


def _all_wire_targets(sw):
    outs = sw.get("wires", [])
    t = []
    for o in outs:
        if isinstance(o, list):
            t.extend(o)
        else:
            t.append(o)
    return t


def test_nested_double_else_outer_switch_has_else_output():
    """外层 switch 必须含 else 规则且 outputs==2（修复核心）。"""
    flow = _compile(NESTED_DOUBLE_ELSE)
    switches = _switches(flow)
    assert len(switches) == 2, f"应有 2 个 switch，实际 {len(switches)}"
    outer = next(s for s in switches
                 if any("$number(payload.x)" in r.get("v", "") for r in s.get("rules", [])))
    assert outer["outputs"] == 2, f"外层 switch outputs 应为 2，实际 {outer['outputs']}"
    assert any(r.get("t") == "else" for r in outer["rules"]), "外层 switch 缺少 else 规则"
    # 外层 否则 必须挂到 else 输出口（wires[1]）
    outer_false = _debug_id_by_name(flow, "外层假")
    assert outer_false in outer["wires"][1], "外层假 未挂到外层 switch 的 else 输出(wires[1])"


def test_nested_double_else_inner_switch_clean():
    """内层 switch 的连线中不得出现外层 否则 节点（修复核心：误接线消除）。"""
    flow = _compile(NESTED_DOUBLE_ELSE)
    switches = _switches(flow)
    inner = next(s for s in switches
                 if any(r.get("property") == "payload.occurred" for r in s.get("rules", [])))
    inner_targets = _all_wire_targets(inner)
    outer_false = _debug_id_by_name(flow, "外层假")
    assert outer_false not in inner_targets, "外层假 仍挂在内层 switch 上（误接线复发）"
    # 内层自身两个分支正确
    assert "内层真" in [n.get("name") for n in flow["nodes"] if n["id"] in inner["wires"][0]]
    assert "内层假" in [n.get("name") for n in flow["nodes"] if n["id"] in inner["wires"][1]]


def test_single_else_unaffected():
    """单层 否则 行为不变（无回归）。"""
    flow = _compile(SINGLE_ELSE)
    switches = _switches(flow)
    assert len(switches) == 1
    sw = switches[0]
    assert sw["outputs"] == 2
    assert any(r.get("t") == "else" for r in sw["rules"])
    big = _debug_id_by_name(flow, "大于25")
    small = _debug_id_by_name(flow, "不大于25")
    assert big in sw["wires"][0]
    assert small in sw["wires"][1]


def test_multiway_top_level_unaffected():
    """同级 分支/否则如果/否则 多路分支不受影响（无回归）。"""
    flow = _compile(MULTIWAY_TOP)
    switches = _switches(flow)
    assert len(switches) == 1
    sw = switches[0]
    # 2 个条件分支 + 1 个 else = 3 输出
    assert sw["outputs"] == 3, f"多路分支 outputs 应为 3，实际 {sw['outputs']}"
    assert any(r.get("t") == "else" for r in sw["rules"])
    names_on_outputs = []
    for out in sw["wires"]:
        for tid in out:
            n = next((x for x in flow["nodes"] if x["id"] == tid), None)
            if n and n.get("type") == "debug":
                names_on_outputs.append(n.get("name"))
    assert "高温" in names_on_outputs
    assert "中温" in names_on_outputs
    assert "低温" in names_on_outputs


def test_nested_no_outer_else_unaffected():
    """嵌套 switch 但无外层 否则 时，内层 else + 收尾节点正确（无回归）。"""
    flow = _compile(NESTED_NO_OUTER_ELSE)
    switches = _switches(flow)
    assert len(switches) == 2
    inner = next(s for s in switches
                 if any(r.get("property") == "payload.occurred" for r in s.get("rules", [])))
    assert inner["outputs"] == 2
    assert "内层真" in [n.get("name") for n in flow["nodes"] if n["id"] in inner["wires"][0]]
    assert "内层假" in [n.get("name") for n in flow["nodes"] if n["id"] in inner["wires"][1]]
    # 收尾 观测 应出现在内层 switch 之后（外层 branch0 体链）
    assert any(n.get("name") == "收尾" for n in flow["nodes"])
