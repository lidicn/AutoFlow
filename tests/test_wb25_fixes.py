"""WB25 数据流 / 连线完整性审计报告 —— 修复回归测试。

对应 autoflow_WB25_数据流连线完整性测试审计报告.md 的 NEW-1 / NEW-2 / NEW-3 修复：
  - NEW-1：跨实体同名字段碰撞（两个 取值 读同名 <field> 写同一 msg.payload.<field> → 静默覆盖）
  - NEW-2：取值→分支 数据断裂（switch JSONata 裸字段名解析 msg.<field> 而非 msg.payload.<field>）
  - NEW-3：动作 参数反引号注入（`` `x` `` 应转 mustache {{payload.x}}，而非死字面量）

运行：python tests/test_wb25_fixes.py   （或 python -m pytest tests/test_wb25_fixes.py -q）
不依赖 live NR / HA —— 编译器离线可验。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import (
    DSLError, parse, validate, compile_dsl,
)


# ── NEW-1：跨实体同名字段碰撞 ───────────────────────────────────────────────
def test_same_field_cross_entity_rejected():
    """NEW-1：两 取值 读同名 temperature 来自不同实体 → 编译期 error。"""
    dsl = (
        "场景: 双温对照\n"
        "触发: light.x on\n"
        "取值: sensor.indoor temperature\n"
        "取值: sensor.outdoor temperature\n"
        "动作: light.turn_on(light.y)\n"
    )
    scene = parse(dsl)
    issues = validate(scene)
    assert any(i.level == "error" and "字段名冲突" in i.message for i in issues), issues


def test_same_field_same_entity_allowed():
    """对照：同一实体重复读同字段（冗余但无害）→ 不报冲突 error。"""
    dsl = (
        "场景: 冗余读\n"
        "触发: light.x on\n"
        "取值: sensor.indoor temperature\n"
        "取值: sensor.indoor temperature\n"
        "动作: light.turn_on(light.y)\n"
    )
    scene = parse(dsl)
    issues = validate(scene)
    assert not any("字段名冲突" in i.message for i in issues), issues


def test_distinct_fields_ok():
    """对照：不同字段名 → 不报冲突。"""
    dsl = (
        "场景: 不同字段\n"
        "触发: light.x on\n"
        "取值: sensor.indoor temp_in\n"
        "取值: sensor.outdoor temp_out\n"
        "动作: light.turn_on(light.y)\n"
    )
    scene = parse(dsl)
    issues = validate(scene)
    assert not any("字段名冲突" in i.message for i in issues), issues


# ── NEW-2：取值→分支 数据断裂 ───────────────────────────────────────────────
def test_switch_jsonata_qualifies_read_field():
    """NEW-2：取值 X 后 分支 $number(X)>25 的 JSONata 应改写成 $number(payload.X)。"""
    dsl = (
        "场景: 温度路由\n"
        "触发: light.x on\n"
        "取值: sensor.temp temperature\n"
        "分支: $number(temperature) > 25\n"
        "    动作: light.turn_on(light.hot)\n"
        "否则:\n"
        "    动作: light.turn_off(light.hot)\n"
    )
    flow = compile_dsl(dsl)
    switch = next(n for n in flow["nodes"] if n["type"] == "switch")
    # 找第一个真分支规则（非 else）
    rule = next(r for r in switch["rules"] if r.get("t") == "jsonata_exp")
    assert "$number(payload.temperature)" in rule["v"], rule["v"]
    assert "payload.temperature" in rule["v"]
    assert "$number(temperature)" not in rule["v"], "裸字段名未被对齐 → 分支仍将读空"


def test_switch_eq_rule_property_qualified():
    """NEW-2 补：eq 规则 分支 temperature == 30 的节点级 property 应落到 payload.temperature。"""
    dsl = (
        "场景: 温度等值\n"
        "触发: light.x on\n"
        "取值: sensor.temp temperature\n"
        "分支: temperature == 30\n"
        "    动作: light.turn_on(light.fix)\n"
    )
    flow = compile_dsl(dsl)
    switch = next(n for n in flow["nodes"] if n["type"] == "switch")
    assert switch["property"] == "payload.temperature", switch.get("property")


def test_read_state_lands_payload_field():
    """NEW-2 配套：#634 修复后 取值 节点把状态写到 msg.payload.<field>（state_location=data）。"""
    dsl = (
        "场景: 取值落点\n"
        "触发: light.x on\n"
        "取值: sensor.temp temperature\n"
    )
    flow = compile_dsl(dsl)
    node = next(n for n in flow["nodes"] if n["type"] == "api-current-state")
    assert node.get("state_location") == "data", node.get("state_location")
    assert node.get("override_payload") is False, node.get("override_payload")
    props = {p["property"] for p in node.get("outputProperties", [])}
    assert "payload.temperature" in props


# ── NEW-3：动作 参数反引号注入 ──────────────────────────────────────────────
def test_action_backtick_becomes_mustache():
    """NEW-3：动作 参数 `current_temperature` 应转 {{payload.current_temperature}}。"""
    dsl = (
        "场景: 反引号注入\n"
        "触发: light.x on\n"
        "动作: climate.set_temperature(climate.living, temperature=`current_temperature`)\n"
    )
    flow = compile_dsl(dsl)
    svc = next(n for n in flow["nodes"] if n["type"] == "api-call-service")
    data = svc["data"]
    assert "{{payload.current_temperature}}" in data, data


def test_action_backtick_prefixed_keeps_path():
    """NEW-3 补：已带 payload. 前缀的反引号不重复加 payload.。"""
    dsl = (
        "场景: 反引号已定前缀\n"
        "触发: light.x on\n"
        "动作: climate.set_temperature(climate.living, temperature=`payload.current_temperature`)\n"
    )
    flow = compile_dsl(dsl)
    svc = next(n for n in flow["nodes"] if n["type"] == "api-call-service")
    assert "{{payload.current_temperature}}" in svc["data"], svc["data"]


def test_action_plain_value_untouched():
    """对照：普通数字/字符串参数不被改写（仍走数值归一）。"""
    dsl = (
        "场景: 普通参数\n"
        "触发: light.x on\n"
        "动作: light.turn_on(light.y, brightness=80)\n"
    )
    flow = compile_dsl(dsl)
    svc = next(n for n in flow["nodes"] if n["type"] == "api-call-service")
    import json
    data = json.loads(svc["data"])
    assert data["brightness"] == 80, data


if __name__ == "__main__":
    test_same_field_cross_entity_rejected()
    test_same_field_same_entity_allowed()
    test_distinct_fields_ok()
    test_switch_jsonata_qualifies_read_field()
    test_switch_eq_rule_property_qualified()
    test_read_state_lands_payload_field()
    test_action_backtick_becomes_mustache()
    test_action_backtick_prefixed_keeps_path()
    test_action_plain_value_untouched()
    print("ALL WB25 FIX TESTS PASSED")
