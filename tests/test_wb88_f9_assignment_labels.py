# -*- coding: utf-8 -*-
"""WB88 F9 回归防护：赋值通道（变量/提取/构建）未定义引用必须 fail-closed。

背景：WB86/87 的 C_LABEL_UNDEFINED 只铺到 分支/条件/动作 三个发射器，
漏了 `_emit_variable`(compile 内联) / `_emit_extract` / `_emit_build` 三条赋值通道，
导致 `变量: 阈值 = $number(不存在的标签)` 静默编译通过、gate=True，
运行时 flow.阈值 是坏串 → $number()→NaN → 分支恒假 → 静默反向执行（开灯变关灯）。

本测试锁定「WB87 R1 单一共享校验器」的补齐结果：所有产出 jsonata 的通道
共用 `_find_undefined_labels`，并守住误伤面（变量值是【字面量】语义，
无引号中文字面量不得误报）。
"""
import pytest

from autoflow_gateway.dsl_engine import compile_dsl, DSLError, C_LABEL_UNDEFINED


def _reject(dsl, code=C_LABEL_UNDEFINED):
    with pytest.raises(DSLError) as ei:
        compile_dsl(dsl)
    assert ei.value.code == code


def _ok(dsl):
    compile_dsl(dsl)  # 不应抛


# ── F9：赋值通道漏拦（应 fail-closed）──

def test_f9a_variable_value_undefined_label():
    """变量 值引用未定义标签 → 拦截（否则 NaN→分支恒假→静默反向执行）。"""
    _reject(
        "场景: x\n触发: inject\n"
        "变量: 阈值 = $number(不存在的标签)\n"
        "分支: $number(阈值) < 10\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n")


def test_f9b_extract_undefined_label():
    """提取 表达式以 tot=jsonata 落盘并求值 → 未定义标签必须拦截。"""
    _reject("场景: x\n触发: inject\n提取: n = $number(不存在的标签)\n动作: light.turn_on(灯)\n")


def test_f9c_build_json_kind_undefined_label():
    """构建 JSON 字面量分支（json.loads 成功）同样过共享校验器。"""
    _reject("场景: x\n触发: inject\n构建: {\"v\": $number(不存在的标签)}\n"
            "请求: POST https://example.com/api\n")


def test_f9d_build_jsonata_kind_undefined_label():
    """构建 反引号 JSONata 分支（非合法 JSON 会退化到此）→ 拦截。"""
    _reject("场景: x\n触发: inject\n构建: `{\"v\": $number(不存在的标签)}`\n"
            "请求: POST https://example.com/api\n")


def test_f9e_variable_references_undeclared_flow_var():
    """变量 引用未声明的 flow 变量 → 拦截。"""
    _reject("场景: x\n触发: inject\n变量: 阈值 = flow.未声明变量\n动作: light.turn_on(灯)\n")


# ── 误伤面（必须保持编译通过）──

def test_no_fp_variable_plain_number():
    _ok("场景: x\n触发: inject\n变量: 亮度 = 0\n动作: light.turn_on(灯)\n")


def test_no_fp_variable_quoted_literal():
    _ok("场景: x\n触发: inject\n变量: 名称 = \"有人\"\n动作: light.turn_on(灯)\n")


def test_no_fp_variable_unquoted_chinese_literal():
    """最大误伤面：变量值是【字面量】语义，无引号中文不得被当成未定义标签。"""
    _ok("场景: x\n触发: inject\n变量: 消息 = 检测到有人移动\n动作: light.turn_on(灯)\n")


def test_no_fp_variable_referencing_defined_read_field():
    """报告正对照：引用已定义的取值标签必须放行。"""
    _ok("场景: x\n触发: inject\n取值: sensor.light 光照\n变量: 阈值 = $number(光照)\n"
        "分支: $number(阈值) < 10\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n")


def test_no_fp_extract_referencing_defined_read_field():
    """真实生产用法（max_study_scenario）：提取 引用已定义取值标签。"""
    _ok("场景: x\n触发: inject\n取值: sensor.t 温度\n提取: 温度数值 = $number(温度)\n"
        "动作: light.turn_on(灯)\n")


def test_no_fp_extract_payload_path():
    _ok("场景: x\n触发: inject\n提取: 回复 = payload.choices[0].message.content\n"
        "动作: light.turn_on(灯)\n")


def test_no_fp_extract_count_payload():
    _ok("场景: x\n触发: inject\n提取: n = $count(payload)\n动作: light.turn_on(灯)\n")


def test_no_fp_build_json_literal_with_chinese():
    _ok("场景: x\n触发: inject\n构建: {\"content\":\"用轻松语气提醒用户\"}\n"
        "请求: POST https://example.com/api\n")


def test_no_fp_build_jsonata_expression():
    _ok("场景: x\n触发: inject\n构建: 'hello ' & $defined(msg.name)\n"
        "请求: POST https://example.com/api\n")


def test_no_fp_build_jsonata_referencing_payload():
    _ok("场景: x\n触发: inject\n构建: `{\"temp\": payload.temperature, \"time\": $now()}`\n"
        "请求: POST https://example.com/api\n")


# ── 错误优先级：废弃指令仍报 C_DEPRECATED，不被新检查抢先 ──

def test_deprecated_history_still_reports_c_deprecated():
    from autoflow_gateway.dsl_engine import C_DEPRECATED
    _reject("场景: x\n触发: inject\n历史: light.a relative=24h\n"
            "提取: 次数 = $count(history)\n动作: light.turn_on(灯)\n", code=C_DEPRECATED)
