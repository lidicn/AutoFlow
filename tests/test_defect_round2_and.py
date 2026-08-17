"""round2 Bug B 回归测试：单条 分支 用 and 组合「子流输出字段 + 另一字段」。

锁死一类已修的【静默编译错误】：旧实现把整条 `payload.occurred == true and
$number(payload.temp) > 25` 当成「等式」匹配，误判 LHS=payload.occurred、
RHS="true and ..." → 编译成 t='eq'/vt='str' 的非法开关规则（字符串永远不等于
布尔，且 and 组合被折叠成字面值），条件恒假、下游静默走 else，lint/闸门全过。

修复后预期：分支条件含【顶层】and/or 时走 jsonata 路径，编译为
t='jsonata_exp' vt='jsonata' 的合法 JSONata 规则，and 组合被完整保留、可正确求值。

运行：python tests/test_defect_round2_and.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile

AND_COMBO = """
场景: BugB_and组合折叠
触发: inject
取值: sensor.temp x
分支: payload.occurred == true and $number(x) > 25
  观测: 命中
否则:
  观测: 未命中
"""

SIMPLE_EQ = """
场景: BugB_纯等式不误伤
触发: inject
取值: sensor.occurred o
分支: o == true
  观测: 真
否则:
  观测: 假
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _switch(flow):
    sws = [n for n in flow["nodes"] if n["type"] == "switch"]
    # 取「条件含 and」的那个 switch（本场景唯一）
    return next(s for s in sws
                if any("and" in r.get("v", "") for r in s.get("rules", [])))


def test_and_combined_branch_is_jsonata_rule():
    """核心修复：and 组合必须是 jsonata_exp 规则，and 子表达式被保留、不被折叠成字面值。"""
    flow = _compile(AND_COMBO)
    sw = _switch(flow)
    cond = sw["rules"][0]
    assert cond.get("t") == "jsonata_exp", f"and 组合应走 jsonata 规则，实际 t={cond.get('t')}"
    assert cond.get("vt") == "jsonata", f"and 组合规则 vt 应为 jsonata，实际 {cond.get('vt')}"
    assert "and" in cond.get("v", ""), f"and 子表达式被丢失: {cond.get('v')}"
    # 反例：不得是「等式折叠」出来的 str 字面值
    assert cond.get("vt") != "str", "修复前 bug：被折叠成 vt='str' 字面值"
    assert "true and" not in cond.get("v", "").replace(" ", ""), \
        "修复前 bug：RHS 被折叠成 'true and ...' 字面值"


def test_and_combined_branch_else_present():
    """and 组合分支仍正确带 else（双输出、不丢 false 路径）。"""
    flow = _compile(AND_COMBO)
    sw = _switch(flow)
    assert sw["outputs"] == 2, f"outputs 应为 2，实际 {sw['outputs']}"
    assert any(r.get("t") == "else" for r in sw["rules"]), "缺少 else 规则"


def test_simple_eq_branch_unaffected():
    """纯等式分支仍走 eq/bool 快速路径（无回归，不误伤简单条件）。"""
    flow = _compile(SIMPLE_EQ)
    sws = [n for n in flow["nodes"] if n["type"] == "switch"]
    assert len(sws) == 1
    cond = sws[0]["rules"][0]
    assert cond.get("t") == "eq", f"纯等式应走 eq，实际 {cond.get('t')}"
    assert cond.get("vt") == "bool", f"纯等式 vt 应为 bool，实际 {cond.get('vt')}"
