r"""回归：dsl_bug_hunt_round7/8/9 的 D11–D16 缺陷。

  D11 · 子流程参数裸变量名被字面化（需反引号才引用，动作参数却无需）
  D12 · 定时触发 name 重复「定时」（定时 定时 每天 22:30）
  D13 · 多实体动作第二个实体被静默丢弃（light.turn_on(A, B) 只留 A）
  D14 · link_out 异步子流程后串行步骤被静默并行化（无任何警告）
  D15 · R10/R19 error 级却不在阻断集（validate 报错但 will_deploy_block=false）
  D16 · 预期块 state: 前缀拼进 entity_id / 结构化格式被展平为无意义断言
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway.dsl_engine import DSLError, compile_dsl, parse
from autoflow_gateway.flow_linter import lint_flow


def _nodes(dsl: str) -> list:
    return compile_dsl(dsl)["nodes"]


# ── D11：子流程参数裸变量名 ───────────────────────────────────────────────

def test_D11_bare_var_ref_promoted():
    """子流程参数 text=播报文本（无反引号）→ 自动识别为变量引用 flow.播报文本。"""
    dsl = (
        "场景: D11\n"
        "触发: inject\n"
        "变量: 播报文本=动态文本\n"
        "调用子流程: demo_notify(text=播报文本, room=书房, level=一般)\n"
    )
    ch = [n for n in _nodes(dsl)
          if n["type"] == "change" and "入参" in (n.get("name") or "")][0]
    s = str(ch["rules"])
    assert "flow.播报文本" in s, f"裸变量名应绑定 flow 上下文：{s}"
    assert '"to": "播报文本"' not in s, "变量名不得被字面化"


def test_D11_backtick_still_works():
    """回归：反引号写法（text=`播报文本`）保持正确。"""
    dsl = (
        "场景: D11b\n"
        "触发: inject\n"
        "变量: 播报文本=动态文本\n"
        "调用子流程: demo_notify(text=`播报文本`, room=书房)\n"
    )
    ch = [n for n in _nodes(dsl)
          if n["type"] == "change" and "入参" in (n.get("name") or "")][0]
    assert "flow.播报文本" in str(ch["rules"])


def test_D11_literal_value_unchanged():
    """回归：非变量名的普通字符串参数保持字面量（text=欢迎回家 不当变量）。"""
    dsl = (
        "场景: D11c\n"
        "触发: inject\n"
        "变量: 阈值=30\n"
        "调用子流程: demo_notify(text=欢迎回家, room=书房)\n"
    )
    ch = [n for n in _nodes(dsl)
          if n["type"] == "change" and "入参" in (n.get("name") or "")][0]
    assert '"欢迎回家"' in str(ch["rules"]), "普通字符串必须保持字面量"


# ── D12：定时触发 name ────────────────────────────────────────────────────

def test_D12_timer_name_no_dup():
    """触发: 定时 每天 22:30 → inject name 不含重复「定时」。"""
    inj = [n for n in _nodes(
        "场景: D12\n触发: 定时 每天 22:30\n动作: light.turn_off(light.A)")
        if n["type"] == "inject"][0]
    name = inj.get("name") or ""
    assert name.count("定时") == 1, f"name 不得重复「定时」：{name!r}"
    assert name == "定时 每天 22:30", f"name 应规范：{name!r}"
    assert inj.get("crontab") == "30 22 * * *", "crontab 不受影响"


def test_D12_plain_time_unchanged():
    """回归：触发: 每天 22:30（无「定时」前缀）行为不变。"""
    inj = [n for n in _nodes(
        "场景: D12b\n触发: 每天 22:30\n动作: light.turn_off(light.A)")
        if n["type"] == "inject"][0]
    assert inj.get("name") == "定时 每天 22:30"
    assert inj.get("crontab") == "30 22 * * *"


# ── D13：多实体动作 ───────────────────────────────────────────────────────

def test_D13_multi_entity_positional():
    """light.turn_on(light.A, light.B) → entityId 含两个实体。"""
    ac = [n for n in _nodes(
        "场景: D13\n触发: inject\n动作: light.turn_on(light.A, light.B)")
        if n["type"] == "api-call-service"][0]
    assert ac.get("entityId") == ["light.A", "light.B"], \
        f"多实体应全保留：{ac.get('entityId')}"


def test_D13_multi_entity_with_params():
    """light.turn_on(light.A, light.B, brightness_pct=80) → 实体+参数均正确。"""
    ac = [n for n in _nodes(
        "场景: D13b\n触发: inject\n动作: light.turn_on(light.A, light.B, brightness_pct=80)")
        if n["type"] == "api-call-service"][0]
    assert len(ac.get("entityId") or []) == 2
    assert '"brightness_pct": 80' in ac.get("data", "")


def test_D13_array_syntax_unchanged():
    """回归：数组写法 light.turn_on([light.A, light.B]) 保持正确。"""
    ac = [n for n in _nodes(
        "场景: D13c\n触发: inject\n动作: light.turn_on([light.A, light.B])")
        if n["type"] == "api-call-service"][0]
    assert ac.get("entityId") == ["light.A", "light.B"]


def test_D13_single_entity_unchanged():
    """回归：单实体动作不受影响。"""
    ac = [n for n in _nodes(
        "场景: D13d\n触发: inject\n动作: light.turn_on(light.A)")
        if n["type"] == "api-call-service"][0]
    assert ac.get("entityId") == ["light.A"]


# ── D14：link_out 后串行警告 ──────────────────────────────────────────────

def test_D14_linkout_after_serial_warns():
    """demo_notify 后接动作 → 产出 C_SUBFLOW_ASYNC_SERIAL 警告（不静默并行）。"""
    res = compile_dsl(
        "场景: D14\n触发: inject\n"
        "动作: light.turn_on(light.A)\n"
        "调用子流程: demo_notify(text=灯已打开, room=书房, level=一般)\n"
        "动作: light.turn_off(light.B)\n")
    w = [l for l in res["lint"] if l.get("rule") == "C_SUBFLOW_ASYNC_SERIAL"]
    assert w, "link_out 后接串行步骤必须给出 C_SUBFLOW_ASYNC_SERIAL 警告"
    assert w[0]["level"] == "warning"


def test_D14_linkout_as_last_no_warn():
    """link_out 子流程是最后一个步骤 → 不产生警告。"""
    res = compile_dsl(
        "场景: D14b\n触发: inject\n"
        "调用子流程: demo_notify(text=晚安, room=书房)\n")
    w = [l for l in res["lint"] if l.get("rule") == "C_SUBFLOW_ASYNC_SERIAL"]
    assert w == [], "末尾 link_out 不应误报"


def test_D14_request_response_serial_ok():
    """回归：请求/响应型子流程（history_occurred）后接动作 → 正常串行、无警告。"""
    res = compile_dsl(
        "场景: D14c\n触发: inject\n"
        "调用子流程: history_occurred(entity=sensor.x, start=今天, end=现在)\n"
        "动作: light.turn_on(light.A)\n")
    w = [l for l in res["lint"] if l.get("rule") == "C_SUBFLOW_ASYNC_SERIAL"]
    assert w == [], "请求/响应型子流程后串行不应误报"


# ── D15：R10/R19 阻断集 ───────────────────────────────────────────────────

def test_D15_R10_blocks():
    """多数组连线（第 2+ 数组目标永不触发）→ R10 error 且会阻断。"""
    flow = {"id": "x", "label": "x", "nodes": [
        {"id": "n1", "type": "inject", "z": "x", "wires": [["n2", "n3"], ["n4"]]},
        {"id": "n2", "type": "debug", "z": "x", "wires": []},
        {"id": "n3", "type": "debug", "z": "x", "wires": []},
        {"id": "n4", "type": "debug", "z": "x", "wires": []}]}
    r10 = [i for i in lint_flow(flow) if i.get("rule") == "R10"]
    assert r10 and r10[0]["level"] == "error"
    _BLOCK = {"R10", "R13", "R15", "R20", "R17", "R19", "R22", "R30", "R32"}
    blk = [i for i in lint_flow(flow)
           if i.get("level") == "error" and i.get("rule") in _BLOCK]
    assert any(b["rule"] == "R10" for b in blk), "R10 必须进阻断集"


def test_D15_R19_blocks():
    """api-call-service 用 entity_id（snake_case）→ R19 error 且会阻断。"""
    flow = {"id": "x", "label": "x", "nodes": [
        {"id": "n1", "type": "inject", "z": "x", "wires": [["n2"]]},
        {"id": "n2", "type": "api-call-service", "z": "x",
         "domain": "light", "service": "turn_on",
         "entity_id": ["light.A"], "wires": [[]]}]}
    r19 = [i for i in lint_flow(flow) if i.get("rule") == "R19"]
    assert r19 and r19[0]["level"] == "error"
    _BLOCK = {"R10", "R13", "R15", "R20", "R17", "R19", "R22", "R30", "R32"}
    blk = [i for i in lint_flow(flow)
           if i.get("level") == "error" and i.get("rule") in _BLOCK]
    assert any(b["rule"] == "R19" for b in blk), "R19 必须进阻断集"


# ── D16：预期块解析 ────────────────────────────────────────────────────────

def test_D16_state_prefix_stripped():
    """预期: state: light.A = on → entity_id=light.A、state=on（前缀不拼入）。"""
    sc = parse(
        "场景: D16\n触发: inject\n动作: light.turn_on(light.A)\n"
        "预期:\n  state: light.A = on\n")
    hit = [e for e in sc.expected
           if e.get("entity_id") == "light.A" and e.get("state") == "on"]
    assert hit, f"state: 前缀应剥离：{sc.expected}"


def test_D16_structured_format_blocked():
    """预期块结构化格式（entity:/attributes: 独立行）→ 明确报 C_EXPECTED_FORMAT。"""
    with pytest.raises(DSLError) as exc:
        parse(
            "场景: D16b\n触发: inject\n动作: light.turn_on(light.A)\n"
            "预期:\n  entity: light.A\n  state: on\n  attributes:\n    brightness_pct: 80\n")
    assert exc.value.code == "C_EXPECTED_FORMAT", \
        f"结构化格式应明确报错：{getattr(exc.value, 'code', None)}"


def test_D16_plain_format_unchanged():
    """回归：预期: light.A = on（无前缀）保持正确。"""
    sc = parse(
        "场景: D16c\n触发: inject\n动作: light.turn_on(light.A)\n"
        "预期:\n  light.A = on\n")
    hit = [e for e in sc.expected
           if e.get("entity_id") == "light.A" and e.get("state") == "on"]
    assert hit, f"普通写法不应受影响：{sc.expected}"


def test_D16_subflow_expected_unchanged():
    """回归：预期: subflow: demo_notify 保持正确解析。"""
    sc = parse(
        "场景: D16d\n触发: inject\n动作: light.turn_on(light.A)\n"
        "预期:\n  subflow: demo_notify\n")
    assert any(e.get("subflow") == "demo_notify" for e in sc.expected), \
        f"subflow 预期应正确：{sc.expected}"
