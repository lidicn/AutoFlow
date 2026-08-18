"""Defect B（iss_60e4d57ce8 / high）回归测试。

根因：history_* 子流程把答案写回 msg.payload，串行调用会互相覆盖；下游 switch 若
同时引用两个 history 子流程的字段（前者已被后者抹掉）→ 条件永假、动作永不执行、
运行期不报错（黑箱静默 bug）。

守卫：
- 编译期（dsl_engine）：串行调用后 switch 引用被覆盖的较早子流程字段 → DSLError C_HISTORY_CLOBBER
- 部署闸（flow_linter R36）：同一 switch 引用 ≥2 个不同 history 子流程专属字段 → error
- C4 (iss_3e5f462d01) 进一步收紧：线性顺序 2 个不同 history_* 子流程（前者输出未被中间
  『分支/提取』消费）→ 编译期直接 DSLError C_HISTORY_CLOBBER（fail-closed，不再仅 warning）。
  因此『串行但下游只引用最新字段』(SERIAL_LATEST) 在 C4 之后也被硬拦截——前者输出被静默
  丢弃，属同一类危险。Defect B 的核心（stale-read 永假）仍由 switch 级检查守住；此测试仅
  将 SERIAL_LATEST 的断言从「允许」改为「应被 C4 拦截」以记录该收紧。

运行：python -m pytest tests/test_defect_b_history_clobber.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import compile_dsl, DSLError
from autoflow_gateway.flow_linter import lint_flow

_HEADER = "触发: inject\n"

# 坏：串行两个 history_*，下游 switch 同时引用两者字段
BAD = _HEADER + (
    "调用子流程: history_occurred(entity=light.study, start=8h前, end=现在)\n"
    "调用子流程: history_duration(entity=light.study, start=8h前, end=现在, state=on)\n"
    "分支: payload.occurred == true and payload.total_seconds > 3600\n"
    "  动作: light.turn_on(light.monitor)\n"
)

# 好：嵌套（第一个 history 命中后的分支内再调第二个）
NESTED = _HEADER + (
    "调用子流程: history_occurred(entity=light.study, start=8h前, end=现在)\n"
    "分支: payload.occurred == true\n"
    "  调用子流程: history_duration(entity=light.study, start=8h前, end=现在, state=on)\n"
    "  分支: payload.total_seconds > 3600\n"
    "    动作: light.turn_on(light.monitor)\n"
)

# 历史(C4 前)曾允许：串行但下游 switch 只引用最新子流程字段（无冲突）。
# C4 (iss_3e5f462d01) 收紧后此写法因前者输出被静默丢弃而一律硬拦截。
SERIAL_LATEST = _HEADER + (
    "调用子流程: history_occurred(entity=light.study, start=8h前, end=现在)\n"
    "调用子流程: history_duration(entity=light.study, start=8h前, end=现在, state=on)\n"
    "分支: payload.total_seconds > 3600\n"
    "  动作: light.turn_on(light.monitor)\n"
)


def test_compile_blocks_serial_history_clobber():
    try:
        compile_dsl(BAD)
    except DSLError as e:
        assert e.code == "C_HISTORY_CLOBBER", f"应抛 C_HISTORY_CLOBBER，实际 {e.code}"
        return
    raise AssertionError("坏 DSL 未被编译期拦截（应抛 C_HISTORY_CLOBBER）")


def test_compile_allows_nested_history():
    flow = compile_dsl(NESTED)
    assert flow and flow.get("nodes"), "嵌套 history DSL 应编译通过"


def test_compile_blocks_serial_referencing_latest_only_c4():
    """C4 (iss_3e5f462d01) 收紧：即便下游只引用最新 history 字段，顺序双 history 仍因前者
    输出被静默丢弃而硬拦截（fail-closed）。此用例历史上曾允许，C4 后必须被拒绝。"""
    try:
        compile_dsl(SERIAL_LATEST)
    except DSLError as e:
        assert e.code == "C_HISTORY_CLOBBER", f"应抛 C_HISTORY_CLOBBER，实际 {e.code}"
        return
    raise AssertionError("C4 后顺序双 history（只读最新字段）应被硬拦截")


def _hist_node(nid, suffix):
    return {"id": nid, "type": f"subflow:af_hist_{suffix}", "z": "f1", "wires": [[]]}


def _switch(ref_fields):
    # 构造一个引用若干 payload.<field> 的 jsonata switch 规则
    expr = " and ".join(f"payload.{f} == true" for f in ref_fields)
    return {"id": "s1", "type": "switch", "z": "f1",
            "rules": [{"t": "jsonata_exp", "v": expr}]}


def test_lint_r36_flags_two_history_fields():
    nodes = [_hist_node("h1", "occurred"), _hist_node("h2", "duration"),
             _switch(["occurred", "total_seconds"])]
    issues = lint_flow({"nodes": nodes})
    assert any(i["rule"] == "R36" for i in issues), f"应报 R36，实际 {[i['rule'] for i in issues]}"


def test_lint_r36_no_false_positive_generic_field():
    # 仅 1 个 history 节点 + switch 引用 payload.state（跨域通用名，不在专属映射中）
    nodes = [_hist_node("h1", "occurred"),
             {"id": "s1", "type": "switch", "z": "f1",
              "rules": [{"t": "eq", "v": "on", "vt": "str", "property": "payload.state"}]}]
    issues = lint_flow({"nodes": nodes})
    assert not any(i["rule"] == "R36" for i in issues), f"不应误报 R36，实际 {[i['rule'] for i in issues]}"


def test_lint_r36_no_false_positive_single_history():
    # 单 history 节点 + switch 仅引用其自身字段（无第二个子流程可冲突）
    nodes = [_hist_node("h1", "occurred"), _switch(["occurred"])]
    issues = lint_flow({"nodes": nodes})
    assert not any(i["rule"] == "R36" for i in issues), f"单一 history 不应误报 R36，实际 {[i['rule'] for i in issues]}"
