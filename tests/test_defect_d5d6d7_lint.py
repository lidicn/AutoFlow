r"""回归：dsl_bug_hunt_round5.md 的 D5–D7 lint/动态参数缺陷。

  D5 · R31 误报字符串字面量 —— `payload.x = "on"` 中 `on` 被当未定义字段
  D6 · 动态动作参数被字符串化 —— `brightness_pct=payload.brightness` 变字面串
  D7 · R31 漏报函数调用内字段 —— `$number(payload.nonexistent)` 未识别
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.flow_linter import lint_flow


def _r31(flow) -> list:
    return [i for i in lint_flow(flow) if i["rule"] == "R31"]


def _ac(nodes) -> list:
    return [n for n in nodes if n["type"] == "api-call-service"]


# ── D5：R31 不误报字符串字面量 ───────────────────────────────────────────

def test_D5_string_literal_not_reported():
    """payload.state1 = "on" 的 on 是字符串字面量，不得报 R31 未定义字段。"""
    dsl = (
        "场景: D5\n"
        "触发: inject\n"
        "取值: light.A state1\n"
        "取值: light.B state2\n"
        "分支: payload.state1 = \"on\" and payload.state2 = \"on\"\n"
        "  动作: switch.turn_on(switch.C)\n"
        "否则:\n"
        "  动作: switch.turn_off(switch.C)\n"
    )
    assert _r31(compile_dsl(dsl)) == [], "字符串字面量不得触发 R31"


def test_D5_single_quoted_and_backtick_literals():
    """单引号 / 反引号字符串字面量同样忽略。"""
    dsl = (
        "场景: D5b\n"
        "触发: inject\n"
        "取值: light.A s\n"
        "分支: payload.s = 'on' and payload.s = `off`\n"
        "  动作: switch.turn_on(switch.C)\n"
    )
    assert _r31(compile_dsl(dsl)) == [], "单引号/反引号字面量不得触发 R31"


def test_D5_declared_field_still_ok():
    """已声明的字段引用（payload.state1）正常不报。"""
    dsl = (
        "场景: D5c\n"
        "触发: inject\n"
        "取值: light.A state1\n"
        "分支: payload.state1 = \"on\"\n"
        "  动作: switch.turn_on(switch.C)\n"
    )
    assert _r31(compile_dsl(dsl)) == [], "已声明字段不应报 R31"


# ── D6：动态动作参数走 jsonata ───────────────────────────────────────────

def test_D6_payload_expr_jsonata():
    """brightness_pct=payload.brightness → dataType=jsonata，值原样为表达式。"""
    dsl = (
        "场景: D6\n"
        "触发: inject\n"
        "取值: sensor.zong brightness\n"
        "动作: light.turn_on(light.A, brightness_pct=payload.brightness)\n"
    )
    ac = _ac(compile_dsl(dsl)["nodes"])[0]
    assert ac.get("dataType") == "jsonata", f"应 jsonata：{ac.get('dataType')}"
    assert "payload.brightness" in ac.get("data", ""), \
        f"值应为 JSONata 表达式：{ac.get('data')}"
    assert '"payload.brightness"' not in ac.get("data", ""), \
        "值不得被字符串化（带引号）"


def test_D6_flow_and_msg_expr_jsonata():
    """flow.x / msg.x 动态表达式同样走 jsonata。"""
    dsl = (
        "场景: D6b\n"
        "触发: inject\n"
        "动作: light.turn_on(light.A, color_temp=flow.色温, name=msg.title)\n"
    )
    ac = _ac(compile_dsl(dsl)["nodes"])[0]
    assert ac.get("dataType") == "jsonata", f"应 jsonata：{ac.get('dataType')}"
    assert "flow.色温" in ac.get("data", "") and "msg.title" in ac.get("data", "")


def test_D6_static_param_unchanged_json():
    """回归：静态数值参数保持 dataType=json 与 JSON number。"""
    dsl = (
        "场景: D6c\n"
        "触发: inject\n"
        "动作: light.turn_on(light.A, brightness_pct=80)\n"
    )
    ac = _ac(compile_dsl(dsl)["nodes"])[0]
    assert ac.get("dataType") == "json", f"静态参数应 json：{ac.get('dataType')}"
    assert '"brightness_pct": 80' in ac.get("data", ""), \
        f"数值应为 JSON number：{ac.get('data')}"


def test_D6_scene_var_unchanged_flow():
    """回归：场景变量参数仍走 flow.<变量名>（#506 既有行为）。"""
    dsl = (
        "场景: D6d\n"
        "触发: inject\n"
        "变量: 亮度=80\n"
        "动作: light.turn_on(light.A, brightness_pct=亮度)\n"
    )
    ac = _ac(compile_dsl(dsl)["nodes"])[0]
    assert ac.get("dataType") == "jsonata", f"变量参数应 jsonata：{ac.get('dataType')}"
    assert "flow.亮度" in ac.get("data", ""), f"应引用 flow 上下文：{ac.get('data')}"


def test_D6_mixed_static_and_dynamic():
    """同动作静态+动态参数混合：静态保持字面量、动态保持表达式。"""
    dsl = (
        "场景: D6e\n"
        "触发: inject\n"
        "取值: sensor.s brightness\n"
        "动作: light.turn_on(light.A, brightness_pct=payload.brightness, transition=2)\n"
    )
    ac = _ac(compile_dsl(dsl)["nodes"])[0]
    assert ac.get("dataType") == "jsonata"
    assert "payload.brightness" in ac.get("data", "")
    assert '"transition": 2' in ac.get("data", ""), \
        f"静态参数应保持数值：{ac.get('data')}"


# ── D7：R31 识别函数调用内字段 ────────────────────────────────────────────

def test_D7_function_wrapped_missing_field_reported():
    """$number(payload.nonexistent) 中不存在的字段必须报 R31。"""
    dsl = (
        "场景: D7\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.nonexistent) > 0\n"
        "  动作: light.turn_on(light.A)\n"
        "否则:\n"
        "  动作: light.turn_off(light.B)\n"
    )
    r31 = _r31(compile_dsl(dsl))
    assert r31, "函数调用内不存在的字段必须报 R31"
    assert "nonexistent" in r31[0]["message"], f"应点名 nonexistent：{r31[0]['message']}"


def test_D7_existing_function_field_ok():
    """函数调用内已声明字段不报（$number(payload.power) 的 power 已声明）。"""
    dsl = (
        "场景: D7b\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.power) > 0\n"
        "  动作: light.turn_on(light.A)\n"
        "否则:\n"
        "  动作: light.turn_off(light.B)\n"
    )
    assert _r31(compile_dsl(dsl)) == [], "已声明字段在函数内也不应报 R31"


def test_D7_bare_missing_field_still_reported():
    """回归：裸字段引用（powr）仍报 R31。"""
    dsl = (
        "场景: D7c\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: powr > 0\n"
        "  动作: light.turn_on(light.A)\n"
    )
    r31 = _r31(compile_dsl(dsl))
    assert r31 and "powr" in r31[0]["message"], "裸字段误写仍须报 R31"


def test_D7_nested_path_leaf():
    """多级路径 payload.a.b 的叶子 b 也参与检查。"""
    dsl = (
        "场景: D7d\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: payload.some.deep_missing = 1\n"
        "  动作: light.turn_on(light.A)\n"
    )
    r31 = _r31(compile_dsl(dsl))
    assert r31 and "deep_missing" in r31[0]["message"], \
        f"多级路径叶子应报：{r31[0]['message'] if r31 else '无 R31'}"
