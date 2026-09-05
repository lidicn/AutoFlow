"""WB24 审计静默放行收口 —— 编译期三闸回归测试（N5/N6 数值链 + 未解析中文名）。

对应 autoflow_WB24_健壮性负向测试审计报告.md 的编译期收口项：
  - C_JSONATA_SYNTAX：分支/条件 JSONata 语法断裂（如 `$number(state) >` 尾悬运算符）
    此前编译接受、落 NR 后 switch 静默恒假，仅靠 gate 兜底 → 现编译期 error 拦截。
  - C_COMPARE_TYPE_WARN：裸字符串 state 与数字比大小（未包 $number()）
    在 JSONata 中恒 false → 现编译期 warning 提示（不阻断）。
  - C_ENTITY_UNRESOLVED：中文/友好实体名解析不到 entity_id 时此前原样写入 NR
    （永远找不到实体）→ 现编译期 warning 提示（fail-open：实体目录可能为空/未同步，
    不阻断编译但不再静默；无解析器/模板占位/ASCII id 均豁免）。

运行：python -m pytest tests/test_wb24_n679_gates.py -q
不依赖 live NR / HA —— 编译器离线可验，实体解析用注入的 mock resolver。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import (  # noqa: E402
    DSLError, parse, validate, compile_dsl, set_entity_resolver,
)


def _issues(dsl: str):
    return validate(parse(dsl))


# ── C_JSONATA_SYNTAX：语法断裂表达式编译期拦截 ─────────────────────────────
def test_branch_dangling_operator_rejected():
    """N5 收口：`分支: $number(payload.state) >` 尾悬运算符应编译期 error。"""
    dsl = (
        "场景: 断裂分支\n"
        "触发: light.x on\n"
        "分支: $number(payload.state) >\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "error" and "C_JSONATA_SYNTAX" in i.message
               for i in issues), issues


def test_branch_unbalanced_paren_rejected():
    """括号不平衡同样编译期拦截。"""
    dsl = (
        "场景: 括号断裂\n"
        "触发: light.x on\n"
        "分支: $number(payload.state > 25\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "error" and "C_JSONATA_SYNTAX" in i.message
               for i in issues), issues


def test_condition_dangling_operator_rejected():
    """顶层 条件: 走 jsonata 路径的断裂表达式同样拦截。"""
    dsl = (
        "场景: 断裂条件\n"
        "触发: light.x on\n"
        "条件: $number(payload.state) <\n"
        "动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "error" and "C_JSONATA_SYNTAX" in i.message
               for i in issues), issues


def test_branch_valid_jsonata_ok():
    """对照：完整合法 JSONata 分支不报错、可编译。"""
    dsl = (
        "场景: 合法分支\n"
        "触发: light.x on\n"
        "分支: $number(payload.state) > 25\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any(i.level == "error" for i in issues), issues
    flow = compile_dsl(dsl)
    assert flow.get("nodes"), "合法分支应正常编译出节点"


def test_branch_eq_rule_exempt():
    """对照：简单等式走 eq/ne 规则（非 jsonata），不受语法预检影响。"""
    dsl = (
        "场景: 等式分支\n"
        "触发: light.x on\n"
        "分支: payload.state == 有人\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any("C_JSONATA_SYNTAX" in i.message for i in issues), issues


def test_broken_branch_blocks_compile():
    """端到端：断裂分支应让 compile_dsl 抛 DSLError（聚合 C_MULTI_ERROR）。"""
    dsl = (
        "场景: 断裂分支e2e\n"
        "触发: light.x on\n"
        "分支: $number(payload.state) >\n"
        "    动作: light.turn_on(light.a)\n"
    )
    try:
        compile_dsl(dsl)
    except DSLError as e:
        assert "C_JSONATA_SYNTAX" in str(e), str(e)
        return
    raise AssertionError("断裂分支未被 compile_dsl 拦截")


# ── C_COMPARE_TYPE_WARN：裸字符串数值比较提示（warning 不阻断）──────────────
def test_bare_numeric_compare_warns():
    """`payload.state > 25` 未包 $number() 应给 warning。"""
    dsl = (
        "场景: 裸比较\n"
        "触发: light.x on\n"
        "分支: payload.state > 25\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "warning" and "C_COMPARE_TYPE_WARN" in i.message
               for i in issues), issues
    # warning 不阻断编译
    flow = compile_dsl(dsl)
    assert flow.get("nodes")


def test_number_wrapped_compare_no_warn():
    """对照：包了 $number() 不提示。"""
    dsl = (
        "场景: 显式转数\n"
        "触发: light.x on\n"
        "分支: $number(payload.state) > 25\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any("C_COMPARE_TYPE_WARN" in i.message for i in issues), issues


def test_eq_numeric_compare_branch_warns():
    """等值比较字段与数字字面量（分支 ==，未包 $number()）应给 warning（修复 WB44 静默放行）。"""
    dsl = (
        "场景: 等值数字比较\n"
        "触发: light.x on\n"
        "分支: payload.state == 1\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "warning" and "C_COMPARE_TYPE_WARN" in i.message
               for i in issues), issues


def test_neq_numeric_compare_branch_warns():
    """!= 等值比较字段与数字字面量同样应给 warning。"""
    dsl = (
        "场景: 不等数字比较\n"
        "触发: light.x on\n"
        "分支: payload.state != 0\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "warning" and "C_COMPARE_TYPE_WARN" in i.message
               for i in issues), issues


def test_string_literal_eq_numeric_branch_warns():
    """字符串字面量与数字等值比较（\"on\" == 1）应给 warning（WB44 最干净反例）。"""
    dsl = (
        "场景: 字符串字面量等值数字\n"
        "触发: light.x on\n"
        "分支: \"on\" == 1\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert any(i.level == "warning" and "C_COMPARE_TYPE_WARN" in i.message
               for i in issues), issues


def test_eq_string_no_warn():
    """字符串字段与字符串字面量等值比较（payload.state == \"on\"）不应误报。"""
    dsl = (
        "场景: 字符串等值\n"
        "触发: light.x on\n"
        "分支: payload.state == \"on\"\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any("C_COMPARE_TYPE_WARN" in i.message for i in issues), issues


def test_eq_number_wrapped_no_warn():
    """已包 $number() 的等值比较不应误报（豁免）。"""
    dsl = (
        "场景: 已转数等值\n"
        "触发: light.x on\n"
        "分支: $number(payload.state) == 1\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any("C_COMPARE_TYPE_WARN" in i.message for i in issues), issues


def test_flow_var_compare_no_warn():
    """对照：场景变量按原生类型存 flow 上下文（#505），与数字比较合法，豁免提示。"""
    dsl = (
        "场景: 变量比较\n"
        "变量: 阈值 = 25\n"
        "触发: light.x on\n"
        "分支: 阈值 > 20\n"
        "    动作: light.turn_on(light.a)\n"
    )
    issues = _issues(dsl)
    assert not any("C_COMPARE_TYPE_WARN" in i.message for i in issues), issues


# ── C_ENTITY_UNRESOLVED：未解析中文实体名编译期拦截 ─────────────────────────
def test_unresolved_cjk_action_target_warns():
    """动作目标中文名解析不到应给 warning（此前原样写入 NR 完全静默）。"""
    set_entity_resolver(lambda name: None)
    try:
        dsl = (
            "场景: 中文目标\n"
            "触发: light.x on\n"
            "动作: light.turn_on(书房灯)\n"
        )
        issues = _issues(dsl)
        assert any(i.level == "warning" and "C_ENTITY_UNRESOLVED" in i.message
                   for i in issues), issues
        # warning 不阻断编译（fail-open：实体目录可能为空/未同步）
        flow = compile_dsl(dsl)
        assert flow.get("nodes")
    finally:
        set_entity_resolver(None)


def test_unresolved_cjk_trigger_warns():
    """触发实体中文名解析不到同样给 warning。"""
    set_entity_resolver(lambda name: None)
    try:
        dsl = "场景: 中文触发\n触发: 书房灯 on\n动作: light.turn_on(light.a)\n"
        issues = _issues(dsl)
        assert any(i.level == "warning" and "C_ENTITY_UNRESOLVED" in i.message
                   for i in issues), issues
    finally:
        set_entity_resolver(None)


def test_resolvable_cjk_name_ok():
    """对照：能解析的中文名不报错。"""
    set_entity_resolver(
        lambda name: "light.study_desk_lamp" if name == "书房灯" else None)
    try:
        dsl = (
            "场景: 可解析中文\n"
            "触发: light.x on\n"
            "动作: light.turn_on(书房灯)\n"
        )
        issues = _issues(dsl)
        assert not any("C_ENTITY_UNRESOLVED" in i.message for i in issues), issues
    finally:
        set_entity_resolver(None)


def test_no_resolver_offline_exempt():
    """对照：无解析器（离线编译）跳过检查——保持离线可编译（fail-open）。"""
    set_entity_resolver(None)
    dsl = (
        "场景: 离线中文\n"
        "触发: light.x on\n"
        "动作: light.turn_on(书房灯)\n"
    )
    issues = _issues(dsl)
    assert not any("C_ENTITY_UNRESOLVED" in i.message for i in issues), issues


def test_template_placeholder_exempt():
    """对照：模板占位实体（<TEMP>）豁免。"""
    set_entity_resolver(lambda name: None)
    try:
        dsl = (
            "场景: 模板占位\n"
            "触发: light.x on\n"
            "动作: light.turn_on(<灯>)\n"
        )
        issues = _issues(dsl)
        assert not any("C_ENTITY_UNRESOLVED" in i.message for i in issues), issues
    finally:
        set_entity_resolver(None)


if __name__ == "__main__":
    fails = 0
    g = dict(globals())
    for name, fn in g.items():
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
