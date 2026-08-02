"""WB24 健壮性负向测试审计报告 —— 修复回归测试。

对应 autoflow_WB24_健壮性负向测试审计报告.md 的 NEW-F1~F3 修复：
  - F1：子流程 attribute 参数名校验逃逸（history_* 的 attribute= 拼写错属性应被编译期拦）
  - F2：空并行块静默丢弃（并行: 空体应编译期报错，而非产出悬空 inject）
  - F3：子流程空必填参数校验不一致（anysearch_batch(keywords=) 空值应被拦，与 history 系列一致）

运行：python tests/test_wb24_fixes.py   （或 python -m pytest tests/test_wb24_fixes.py -q）
不依赖 live NR / HA —— 编译器离线可验，F1 用注入的 mock 属性解析器模拟网关 catalog。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import (
    DSLError, parse, validate, compile_dsl,
    set_entity_resolver, set_entity_attributes_resolver,
)


# ── F2：空并行块 ──────────────────────────────────────────────────────────
def test_empty_parallel_block_rejected():
    """NEW-F2：并行: 空体不得静默丢弃，应编译期报 error。"""
    scene = parse("场景: 空并行\n触发: light.x on\n并行:\n")
    issues = validate(scene)
    assert any(i.level == "error" and "并行块为空" in i.message for i in issues), issues


def test_nonempty_parallel_ok():
    """对照：含子步骤的并行块不报错。"""
    dsl = (
        "场景: 并行播报\n"
        "触发: light.x on\n"
        "并行:\n"
        "    动作: light.turn_on(light.a)\n"
        "    动作: light.turn_on(light.b)\n"
    )
    # 离线编译（无 resolver）应通过
    flow = compile_dsl(dsl)
    assert flow.get("ok", True) is not False


# ── F3：子流程空必填参数 ─────────────────────────────────────────────────
def test_anysearch_empty_keywords_blocked():
    """NEW-F3：anysearch_batch(keywords=) 空必填此前静默放行，现应编译期拦。"""
    dsl = (
        "场景: 搜索\n"
        "触发: light.x on\n"
        "调用子流程: anysearch_batch(keywords=)\n"
    )
    try:
        compile_dsl(dsl)
    except DSLError as e:
        assert "缺少必填参数" in str(e) or "keywords" in str(e), str(e)
        return
    raise AssertionError("空必填 keywords 未被拦截（F3 修复应拦下）")


def test_subflow_validate_args_empty_required():
    """单元层：validate_args 对空必填参数应等同缺失。"""
    from autoflow_gateway.subflows import get_subflow
    spec = get_subflow("anysearch_batch")
    try:
        spec.validate_args({"keywords": ""}, strict=True)
    except ValueError as e:
        assert "缺少必填参数" in str(e), str(e)
        return
    raise AssertionError("validate_args 未将空必填视为缺失（F3 修复应拦下）")


def test_anysearch_valid_keywords_ok():
    """对照：合法 keywords 不应被拦。"""
    dsl = (
        "场景: 搜索\n"
        "触发: light.x on\n"
        "调用子流程: anysearch_batch(keywords=`mac mini m5`)\n"
    )
    flow = compile_dsl(dsl)
    assert flow.get("ok", True) is not False


# ── F1：history_* attribute 属性名校验 ───────────────────────────────────
def _install_mock_attrs(entity_attrs: dict):
    """注入 mock 实体解析器 + 属性解析器，模拟网关 catalog 已就绪。"""
    def _resolver(name):
        return name if name in entity_attrs else None
    def _attr_resolver(eid):
        return set(entity_attrs.get(eid, {}).keys())
    set_entity_resolver(_resolver)
    set_entity_attributes_resolver(_attr_resolver)


def _reset_resolvers():
    set_entity_resolver(lambda t: t)
    set_entity_attributes_resolver(None)


def test_history_attribute_unknown_blocked():
    """NEW-F1：history_state_at 的 attribute= 拼写错属性应被编译期拦。"""
    _install_mock_attrs({"sensor.temp": {"state": "", "temperature": "", "unit_of_measurement": ""}})
    try:
        dsl = (
            "场景: 历史取值\n"
            "触发: light.x on\n"
            "调用子流程: history_state_at(entity=sensor.temp, at=1h前, attribute=frobnicate)\n"
        )
        try:
            compile_dsl(dsl)
        except DSLError as e:
            assert "已知属性" in str(e) or "frobnicate" in str(e), str(e)
            return
        raise AssertionError("非法 attribute=frobnicate 未被拦截（F1 修复应拦下）")
    finally:
        _reset_resolvers()


def test_history_attribute_valid_ok():
    """对照：合法 attribute=temperature 应通过。"""
    _install_mock_attrs({"sensor.temp": {"state": "", "temperature": "", "unit_of_measurement": ""}})
    try:
        dsl = (
            "场景: 历史取值\n"
            "触发: light.x on\n"
            "调用子流程: history_state_at(entity=sensor.temp, at=1h前, attribute=temperature)\n"
        )
        flow = compile_dsl(dsl)
        assert flow.get("ok", True) is not False
    finally:
        _reset_resolvers()


def test_history_attribute_unknown_unresolved_entity_skipped():
    """fail-open：实体无法解析 / 无属性信息时不拦（避免误伤动态/模板实体）。"""
    _install_mock_attrs({})  # sensor.temp 不在 catalog → 解析器返回 None → 跳过校验
    try:
        dsl = (
            "场景: 历史取值\n"
            "触发: light.x on\n"
            "调用子流程: history_state_at(entity=sensor.temp, at=1h前, attribute=frobnicate)\n"
        )
        # 不应因属性校验而报错（仅在 resolver 能确定属性集合时才拦）
        flow = compile_dsl(dsl)
        assert flow.get("ok", True) is not False
    finally:
        _reset_resolvers()


def test_value_field_is_output_label_not_attribute():
    """澄清 NEW-F1 前提：取值: <entity> <field> 的 field 是输出标签（写 msg.<field>），
    并非实体属性名 —— 即使实体已知属性不包含该标签，也不应被当作属性错误拦截。"""
    _install_mock_attrs({"sensor.temp": {"state": "", "temperature": ""}})
    try:
        dsl = (
            "场景: 取值标签\n"
            "触发: light.x on\n"
            "取值: sensor.temp 温度\n"
        )
        flow = compile_dsl(dsl)
        assert flow.get("ok", True) is not False
    finally:
        _reset_resolvers()


if __name__ == "__main__":
    test_empty_parallel_block_rejected()
    test_nonempty_parallel_ok()
    test_anysearch_empty_keywords_blocked()
    test_subflow_validate_args_empty_required()
    test_anysearch_valid_keywords_ok()
    test_history_attribute_unknown_blocked()
    test_history_attribute_valid_ok()
    test_history_attribute_unknown_unresolved_entity_skipped()
    test_value_field_is_output_label_not_attribute()
    print("ALL WB24 FIX TESTS PASSED")
