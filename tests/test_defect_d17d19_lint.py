r"""回归：dsl_bug_hunt_round10.md 的 D17–D19 缺陷。

  D17 · R4 lint 误报无参数 JSONata 函数调用（$now() 被判空括号不完整）
  D18 · create_subflow/propose_subflow 未校验 dsl_name 字符集（bad-name! 被接受）
  D19 · R31 lint 误报 payload.xxx 字段引用（构建设置的字段被判未定义 → 闸门跳过分支）
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.flow_linter import lint_flow
from autoflow_gateway.gateway import Gateway

_gw = object.__new__(Gateway)  # 绕过 __init__（不触网）


def _r(flow, rule):
    return [i for i in lint_flow(flow) if i["rule"] == rule]


# ── D17：R4 无参数函数调用 ────────────────────────────────────────────────

def test_D17_now_no_r4_false_positive():
    """构建含 $now()（无参数函数）→ 不得报 R4 空括号。"""
    dsl = (
        "场景: D17\n"
        "触发: inject\n"
        "构建: `{\"temp\": payload.temperature, \"time\": $now()}`\n"
    )
    assert _r(compile_dsl(dsl), "R4") == [], "$now() 不得触发 R4"


def test_D17_with_arg_func_no_r4():
    """有参数函数 $length(payload.items) 本就不误报（回归）。"""
    dsl = (
        "场景: D17b\n"
        "触发: inject\n"
        "构建: `$length(payload.items)`\n"
    )
    assert _r(compile_dsl(dsl), "R4") == []


def test_D17_lone_parens_still_flagged():
    """对照：真正的孤立空括号仍报 R4。"""
    dsl = (
        "场景: D17c\n"
        "触发: inject\n"
        "构建: `payload.()`\n"
    )
    assert _r(compile_dsl(dsl), "R4"), "孤立 () 必须仍报 R4"


# ── D18：dsl_name 字符集 ──────────────────────────────────────────────────

def _defn():
    return {"id": "s1", "nodes": [], "in_ports": [], "out_ports": []}


def test_D18_invalid_chars_blocked():
    """dsl_name 含连字符/感叹号 → input 阶段拦截。"""
    for bad in ("bad-name!", "name with space", "1starts_with_digit", "中文名"):
        r = Gateway.propose_subflow(_gw, dsl_name=bad, name="x", definition=_defn())
        assert r["ok"] is False and r["stage"] == "input", \
            f"{bad!r} 应被 input 拦截：{r.get('error')}"


def test_D18_valid_names_pass_charset():
    """合法标识符通过字符校验（后续失败只可能因桩缺 cfg，与字符无关）。"""
    r = Gateway.propose_subflow(_gw, dsl_name="good_name2", name="x",
                                definition=_defn())
    err = r.get("error") or ""
    assert "非法字符" not in err and "标识符" not in err, \
        f"合法名不应被字符校验拦截：{err}"


# ── D19：R31 payload.xxx 字段引用 ─────────────────────────────────────────

def test_D19_build_field_not_reported():
    """构建: {"value": 42} 后分支 payload.value > 30 → 不得报 R31（value 已声明）。"""
    dsl = (
        "场景: D19\n"
        "触发: inject\n"
        "构建: {\"value\": 42}\n"
        "分支: payload.value > 30\n"
        "  动作: light.turn_on(light.A)\n"
        "否则:\n"
        "  动作: light.turn_off(light.B)\n"
    )
    assert _r(compile_dsl(dsl), "R31") == [], "构建声明的字段不得报 R31"


def test_D19_multi_key_build():
    """构建多键对象，任一键分支引用都不误报。"""
    dsl = (
        "场景: D19b\n"
        "触发: inject\n"
        "构建: {\"value\": 42, \"temp\": 26.5}\n"
        "分支: payload.temp > 20\n"
        "  动作: light.turn_on(light.A)\n"
    )
    assert _r(compile_dsl(dsl), "R31") == []


def test_D19_D7_regression_missing_field_still_flagged():
    """D7 回归：$number(payload.nonexistent) 内真缺失字段仍报 R31。"""
    dsl = (
        "场景: D7r\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.nonexistent) > 0\n"
        "  动作: light.turn_on(light.A)\n"
    )
    r31 = _r(compile_dsl(dsl), "R31")
    assert r31 and "nonexistent" in r31[0]["message"], \
        f"真缺失字段必须仍报：{r31[0]['message'] if r31 else '无 R31'}"


def test_D19_D7_regression_declared_field_ok():
    """D7 回归：$number(payload.power) 内已声明字段不报。"""
    dsl = (
        "场景: D7rb\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.power) > 0\n"
        "  动作: light.turn_on(light.A)\n"
    )
    assert _r(compile_dsl(dsl), "R31") == []


def test_D19_D5_regression_string_literal_ok():
    """D5 回归：字符串字面量仍不误报。"""
    dsl = (
        "场景: D5r\n"
        "触发: inject\n"
        "取值: light.A state1\n"
        "分支: payload.state1 = \"on\"\n"
        "  动作: switch.turn_on(switch.C)\n"
    )
    assert _r(compile_dsl(dsl), "R31") == []
