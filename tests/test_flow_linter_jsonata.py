"""flow_linter JSONata / 子流程反模式检测测试（A2）。

运行：python tests/test_flow_linter_jsonata.py
覆盖 R5($defined) / R6($flowContext) / R7(全角括号) / R8(子流程out/in) / R9(双引号字面量)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.flow_linter import lint_flow


def _change_node(rules):
    return {"id": "c1", "type": "change", "z": "f1", "rules": rules}


def _rules(to_expr, tot="jsonata"):
    return [{"t": "set", "p": "payload", "pt": "msg", "to": to_expr, "tot": tot}]


def _lint_change_rules(rules):
    flow = {"nodes": [_change_node(rules)]}
    return lint_flow(flow)


def _rules_of(issues, rule):
    return [i for i in issues if i["rule"] == rule]


def test_R5_defined_flagged():
    issues = _lint_change_rules(_rules("bark_qs & ($defined(x) ? '&a' : '')"))
    assert _rules_of(issues, "R5"), "应检测 $defined 误用"
    assert any(i["level"] == "error" for i in _rules_of(issues, "R5"))


def test_R6_flowContext_warning():
    issues = _lint_change_rules(_rules("$flowContext('title') or title or 'AutoFlow'"))
    assert _rules_of(issues, "R6"), "应检测 $flowContext 空对象毒化"
    assert any(i["level"] == "warning" for i in _rules_of(issues, "R6"))


def test_R7_fullwidth_paren_outside_string_flagged():
    # （x） 裸露在单引号字符串之外 → 应报错
    issues = _lint_change_rules(_rules("（x） & '跟[' & name & ']打了个招呼'"))
    assert _rules_of(issues, "R7"), "裸全角括号应报错"


def test_R7_fullwidth_paren_inside_string_ok():
    # 全角括号在单引号字符串内 → 不报 R7
    issues = _lint_change_rules(_rules("'🔔 书房专注模式已启动（测试）'"))
    assert not _rules_of(issues, "R7"), "单引号内的全角括号不应报错"


def test_R9_double_quote_warning():
    issues = _lint_change_rules(_rules('"hello " & name'))
    assert _rules_of(issues, "R9"), "双引号字面量应警告"


def test_correct_bark_style_no_false_positive():
    # 真实 Bark 风格（单引号 + $encodeUrlComponent + & '' 兜底）不应触发 R5/R6/R7/R9
    expr = "'$encodeUrlComponent((title or \\'AutoFlow\\') & \\'\\')'"
    # 用不带转义的干净写法：
    clean = "$encodeUrlComponent((title or 'AutoFlow') & '')"
    issues = _lint_change_rules(_rules(clean))
    for r in ("R5", "R6", "R7", "R9"):
        assert not _rules_of(issues, r), f"正确 Bark 风格不应触发 {r}"


def test_R8_subflow_out_bad_number():
    flow = {"nodes": [
        {"id": "sf1", "type": "subflow", "name": "坏", "out": [1], "in": [], "nodes": []}
    ]}
    issues = lint_flow(flow)
    assert _rules_of(issues, "R8"), "out=[1] 应报错"


def test_R8_subflow_out_empty_ok():
    flow = {"nodes": [
        {"id": "sf2", "type": "subflow", "name": "好", "out": [], "in": [], "nodes": []}
    ]}
    issues = lint_flow(flow)
    assert not _rules_of(issues, "R8"), "out=[] 不应报错"


def test_R8_subflow_out_valid_port_ok():
    flow = {"nodes": [
        {"id": "sf3", "type": "subflow", "name": "好",
         "out": [{"x": 0, "y": 0, "wires": [["next"]]}], "in": [], "nodes": []}
    ]}
    issues = lint_flow(flow)
    assert not _rules_of(issues, "R8"), "合法端口对象不应报错"


if __name__ == "__main__":
    test_R5_defined_flagged()
    test_R6_flowContext_warning()
    test_R7_fullwidth_paren_outside_string_flagged()
    test_R7_fullwidth_paren_inside_string_ok()
    test_R9_double_quote_warning()
    test_correct_bark_style_no_false_positive()
    test_R8_subflow_out_bad_number()
    test_R8_subflow_out_empty_ok()
    test_R8_subflow_out_valid_port_ok()
    print("✅ test_flow_linter_jsonata 全部通过")
