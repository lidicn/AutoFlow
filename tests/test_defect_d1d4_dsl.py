r"""回归：dsl_bug_hunt_round4.md 的 D1–D4 解析器/语义缺陷。

  D1 · `否则如果:`(elif) 关键字解析错误 —— 被 `_extract_branch_cond` 的
      硬编码 len("分支") 切片切掉「否则」，留下「如果: cond」前缀 → elif 恒假
  D2 · `查询: entity = state` 等号写法 state_value 变 "= on" → 恒不匹配走 else
  D3 · `并行:` 后串行动作被静默并入并行（无汇聚原语）→ 至少高声警告
  D4 · 嵌套并行（并行内再并行）C_UNKNOWN_STEP 编译失败 → 展平同层扇出
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway.dsl_engine import DSLError, compile_dsl, parse


def _nodes(dsl: str) -> list:
    return compile_dsl(dsl)["nodes"]


# ── D1：否则如果: elif 关键字解析 ─────────────────────────────────────────

def test_D1_elif_condition_no_if_prefix():
    """否则如果: 的条件值不得带「如果: 」前缀（旧实现 elif 恒假、静默失败）。"""
    dsl = (
        "场景: D1\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.power) > 100\n"
        "  动作: light.turn_on(light.A)\n"
        "否则如果: $number(payload.power) > 50\n"
        "  动作: light.turn_on(light.B)\n"
        "否则:\n"
        "  动作: switch.turn_off(switch.C)\n"
    )
    sw = next(n for n in _nodes(dsl) if n["type"] == "switch")
    vals = [r.get("v") for r in sw["rules"]]
    assert vals == ["$number(payload.power) > 100",
                    "$number(payload.power) > 50",
                    "true"], f"elif 条件被污染：{vals}"
    assert not any(str(v).startswith("如果") for v in vals), "条件值不得带 如果: 前缀"


def test_D1_elif_else_branch_wiring():
    """三路 if/elif/else 各分支动作必须全部接线（B 分支命中节点必须可达）。"""
    dsl = (
        "场景: D1b\n"
        "触发: inject\n"
        "取值: sensor.x power\n"
        "分支: $number(payload.power) > 100\n"
        "  动作: light.turn_on(light.A)\n"
        "否则如果: $number(payload.power) > 50\n"
        "  动作: light.turn_on(light.B)\n"
        "否则:\n"
        "  动作: switch.turn_off(switch.C)\n"
    )
    nodes = _nodes(dsl)
    sw = next(n for n in nodes if n["type"] == "switch")
    # 三条输出口都必须非空（分支0/分支1/else）
    wires = sw["wires"]
    assert len(wires) == 3, f"switch 应有 3 条输出口，实际 {len(wires)}"
    assert all(w for w in wires), f"每条输出口都必须接线：{wires}"


def test_D1_branch_keyword_unchanged():
    """回归：`分支:` 连续多路写法（正确替代方案）不得被新解析影响。"""
    dsl = (
        "场景: D1c\n"
        "触发: inject\n"
        "分支: $number(payload.x) > 10\n"
        "  动作: light.turn_on(light.A)\n"
        "分支: $number(payload.x) > 5\n"
        "  动作: light.turn_on(light.B)\n"
        "否则:\n"
        "  动作: switch.turn_off(switch.C)\n"
    )
    sw = next(n for n in _nodes(dsl) if n["type"] == "switch")
    vals = [r.get("v") for r in sw["rules"]]
    assert vals[0] == "$number(payload.x) > 10", f"首分支条件回归：{vals}"
    assert vals[1] == "$number(payload.x) > 5", f"二分支条件回归：{vals}"


def test_D1_else_if_english_elif():
    """英文 elif: 前缀同样正确（与中文 否则如果: 走同一修复路径）。"""
    dsl = (
        "场景: D1d\n"
        "触发: inject\n"
        "分支: $number(payload.x) > 10\n"
        "  动作: light.turn_on(light.A)\n"
        "elif: $number(payload.x) > 2\n"
        "  动作: light.turn_on(light.B)\n"
        "否则:\n"
        "  动作: switch.turn_off(switch.C)\n"
    )
    sw = next(n for n in _nodes(dsl) if n["type"] == "switch")
    vals = [r.get("v") for r in sw["rules"]]
    assert vals[1] == "$number(payload.x) > 2", f"elif 条件污染：{vals}"


# ── D2：查询: 等号写法 ────────────────────────────────────────────────────

def test_D2_query_equals_state_value_stripped():
    """查询: light.test = on → state_value='on'，switch eq v='on'（旧为 '= on' 恒不匹配）。"""
    dsl = (
        "场景: D2\n"
        "触发: inject\n"
        "查询: light.test = on\n"
        "  动作: light.turn_off(light.test)\n"
        "否则:\n"
        "  动作: light.turn_on(light.test)\n"
    )
    nodes = _nodes(dsl)
    qs = next(n for n in nodes if n["type"] == "api-current-state")
    assert qs.get("state_value") == "on", f"state_value 应为 on，实际 {qs.get('state_value')!r}"
    sw = next(n for n in nodes if n["type"] == "switch")
    assert sw["rules"][0] == {"t": "eq", "v": "on", "vt": "str"}, \
        f"switch eq 规则应为 on：{sw['rules']}"


def test_D2_query_no_space_equals():
    """查询: light.test=on（无空格等号）同样支持。"""
    dsl = (
        "场景: D2b\n"
        "触发: inject\n"
        "查询: light.test=on\n"
        "  动作: light.turn_off(light.test)\n"
    )
    qs = next(n for n in _nodes(dsl) if n["type"] == "api-current-state")
    assert qs.get("state_value") == "on", f"无空格等号应解析为 on：{qs.get('state_value')!r}"


def test_D2_query_space_separated_unchanged():
    """回归：空格分隔写法 查询: light.test on 保持正确。"""
    dsl = (
        "场景: D2c\n"
        "触发: inject\n"
        "查询: light.test on\n"
        "  动作: light.turn_off(light.test)\n"
        "否则:\n"
        "  动作: light.turn_on(light.test)\n"
    )
    nodes = _nodes(dsl)
    qs = next(n for n in nodes if n["type"] == "api-current-state")
    assert qs.get("state_value") == "on", f"空格分隔应解析为 on：{qs.get('state_value')!r}"
    sw = next(n for n in nodes if n["type"] == "switch")
    assert sw["rules"][0]["v"] == "on", f"switch v 应为 on：{sw['rules']}"


def test_D2_query_bad_format_still_errors():
    """完全无状态值的查询仍报 C_QUERY_FORMAT（不因等号剥离而放宽）。"""
    dsl = (
        "场景: D2d\n"
        "触发: inject\n"
        "查询: light.test\n"
    )
    with pytest.raises(DSLError) as exc:
        compile_dsl(dsl)
    assert exc.value.code == "C_QUERY_FORMAT", \
        f"缺状态值应报 C_QUERY_FORMAT，实际 {getattr(exc.value, 'code', None)}"


# ── D3：并行后串行 ────────────────────────────────────────────────────────

def test_D3_parallel_then_sequential_warns():
    """并行块后还有串行步骤 → lint 必须产出 C_PARALLEL_SEQUENTIAL 警告（不静默）。"""
    dsl = (
        "场景: D3\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  动作: light.turn_on(light.B)\n"
        "动作: switch.turn_on(switch.C)\n"
    )
    res = compile_dsl(dsl)
    warn = [l for l in res["lint"] if l.get("rule") == "C_PARALLEL_SEQUENTIAL"]
    assert warn, "并行后串行必须给出 C_PARALLEL_SEQUENTIAL 警告"
    assert warn[0]["level"] == "warning", "该警告应为 warning 级（fail-open）"


def test_D3_parallel_as_last_step_no_warn():
    """并行块是 body 最后一个步骤 → 不产生 C_PARALLEL_SEQUENTIAL（无后续可并）。"""
    dsl = (
        "场景: D3b\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  动作: light.turn_on(light.B)\n"
    )
    res = compile_dsl(dsl)
    warn = [l for l in res["lint"] if l.get("rule") == "C_PARALLEL_SEQUENTIAL"]
    assert warn == [], "末尾并行块不应误报 C_PARALLEL_SEQUENTIAL"


def test_D3_comment_after_parallel_no_warn():
    """并行块后只有注释（可视化说明）→ 不算串行步骤，不误报。"""
    dsl = (
        "场景: D3c\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  动作: light.turn_on(light.B)\n"
        "注释: 以上两灯并行\n"
    )
    res = compile_dsl(dsl)
    warn = [l for l in res["lint"] if l.get("rule") == "C_PARALLEL_SEQUENTIAL"]
    assert warn == [], "注释不应触发 C_PARALLEL_SEQUENTIAL"


# ── D4：嵌套并行 ──────────────────────────────────────────────────────────

def test_D4_nested_parallel_compiles():
    """并行内再并行：不再 C_UNKNOWN_STEP，所有叶子从同一上游扇出。"""
    dsl = (
        "场景: D4\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  并行:\n"
        "    动作: light.turn_on(light.B)\n"
        "    动作: switch.turn_on(switch.C)\n"
    )
    nodes = _nodes(dsl)
    inj = next(n for n in nodes if n["type"] == "inject")
    acts = [n for n in nodes if n["type"] == "api-call-service"]
    assert len(acts) == 3, f"嵌套并行应有 3 个动作叶子，实际 {len(acts)}"
    assert len(inj["wires"][0]) == 3, \
        f"三个叶子都应从 inject 扇出，实际 {inj['wires'][0]}"


def test_D4_nested_parallel_three_levels():
    """三层嵌套并行同样展平（递归）。"""
    dsl = (
        "场景: D4b\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  并行:\n"
        "    动作: light.turn_on(light.B)\n"
        "    并行:\n"
        "      动作: switch.turn_on(switch.C)\n"
        "      动作: switch.turn_on(switch.D)\n"
    )
    nodes = _nodes(dsl)
    inj = next(n for n in nodes if n["type"] == "inject")
    assert len(inj["wires"][0]) == 4, f"三层嵌套应展平 4 叶，实际 {inj['wires'][0]}"


def test_D4_flat_parallel_unchanged():
    """回归：单层并行编译行为不变（两臂从同一上游扇出）。"""
    dsl = (
        "场景: D4c\n"
        "触发: inject\n"
        "并行:\n"
        "  动作: light.turn_on(light.A)\n"
        "  动作: light.turn_on(light.B)\n"
    )
    nodes = _nodes(dsl)
    inj = next(n for n in nodes if n["type"] == "inject")
    assert len(inj["wires"][0]) == 2, f"单层并行应 2 臂，实际 {inj['wires'][0]}"


def test_D4_nested_parallel_in_branch_body():
    """分支体内嵌套并行（C5 场景 + D4 组合）：switch 分支输出口必须接到所有叶子。"""
    dsl = (
        "场景: D4d\n"
        "触发: inject\n"
        "分支: payload.x = 1\n"
        "  并行:\n"
        "    动作: light.turn_on(light.A)\n"
        "    并行:\n"
        "      动作: light.turn_on(light.B)\n"
        "      动作: switch.turn_on(switch.C)\n"
    )
    nodes = _nodes(dsl)
    sw = next(n for n in nodes if n["type"] == "switch")
    branch0 = sw["wires"][0]
    assert len(branch0) == 3, f"分支0 应接到全部 3 个叶子，实际 {branch0}"
    # 无孤儿：所有 api-call-service 都必须有入边
    all_ids = {n["id"] for n in nodes}
    targets = {w for n in nodes for grp in n.get("wires", []) for w in grp}
    acts = [n for n in nodes if n["type"] == "api-call-service"]
    for a in acts:
        assert a["id"] in targets, f"动作 {a['id']} 成孤儿（无人连入）"
