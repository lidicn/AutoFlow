"""C5 (iss_69c34b1539 / high) 回归测试：分支/门 体首步为 并行 时，所有臂都必须接到入口输出口。

锁死一类【静默运行态断流】：`_emit_body` 被 分支(_emit_switch) / 查询状态门(_emit_current_state)
/ 时间段门(_emit_time_range) 以 `sources=[]`、`last=None` 调用（分支体/否则体首步）。若体首步是
`并行` 块，旧实现该块的 fan-out 循环因 `upstream=[]` 且无 entry 注入，仅由 caller 的 `connect_out`
接到【第一个臂】，后臂成孤儿节点(R13 同类)——运行态永不执行、lint 全过、极难察觉。

修复后预期：分支/门 体首步的并行块，经 `entry=(src_id, out_idx)` 注入入口输出口，所有臂都被
`connect_out` 扇出，无任何臂成为孤儿（inbound 非空）。

运行：python tests/test_defect_c5_parallel_in_branch.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile


# C5：分支 内 并行（两臂都须接到 switch 的 true 分支输出口）
C5_BRANCH_PARALLEL = """
场景: 分支内并行
触发: inject
分支: $number(1) > 0
  并行:
    动作: light.turn_on(light.a)
    动作: light.turn_on(light.b)
否则:
  动作: light.turn_off(light.c)
"""

# C5：分支 内 3 臂并行（验证不止两臂都接）
C5_BRANCH_PARALLEL_3 = """
场景: 分支内三臂并行
触发: inject
分支: $number(1) > 0
  并行:
    动作: light.turn_on(light.a)
    动作: light.turn_on(light.b)
    动作: light.turn_on(light.d)
否则:
  动作: light.turn_off(light.c)
"""

# C5：查询状态门 否则体 为 并行（两臂须接到门的 else 输出口）
C5_GATE_ELSE_PARALLEL = """
场景: 门否则并行
触发: inject
查询状态: switch.d4f0eaeab731_switch == on
  动作: light.turn_on(light.a)
否则:
  并行:
    动作: light.turn_on(light.b)
    动作: light.turn_on(light.c)
"""

# C5：时间段门 否则体 为 并行（两臂须接到时间段门的 else 输出口）
C5_TIMERANGE_ELSE_PARALLEL = """
场景: 时段外并行
触发: inject
时间段: 07:00-23:00
  动作: light.turn_on(light.a)
否则:
  并行:
    动作: light.turn_on(light.b)
    动作: light.turn_on(light.c)
"""


def _inbound(flow):
    inc = {n["id"]: set() for n in flow["nodes"]}
    for n in flow["nodes"]:
        for grp in (n.get("wires") or []):
            for t in (grp or []):
                if t in inc:
                    inc[t].add(n["id"])
    return inc


def _orphans(flow):
    out = []
    for n in flow["nodes"]:
        t = n["type"]
        if t in ("inject", "tab", "comment"):
            continue
        if not _inbound(flow)[n["id"]]:
            out.append((n["id"], t, n.get("name")))
    return out


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def test_branch_parallel_no_orphans():
    """分支 内 并行：两臂都接到 switch，无孤儿节点。"""
    flow = _compile(C5_BRANCH_PARALLEL)
    orph = _orphans(flow)
    assert not orph, f"分支内并行不应有孤儿节点，实际 {orph}"
    # 至少应有两个动作节点（两臂），且都接到了入边
    actions = [n for n in flow["nodes"] if n["type"] == "api-call-service"]
    assert len(actions) >= 2, f"应编译出 ≥2 个动作节点（两臂），实际 {len(actions)}"


def test_branch_parallel_3arms_no_orphans():
    """分支 内 3 臂并行：所有臂都接到 switch，无孤儿节点。"""
    flow = _compile(C5_BRANCH_PARALLEL_3)
    orph = _orphans(flow)
    assert not orph, f"分支内三臂并行不应有孤儿节点，实际 {orph}"
    actions = [n for n in flow["nodes"] if n["type"] == "api-call-service"]
    assert len(actions) >= 3, f"应编译出 ≥3 个动作节点（三臂），实际 {len(actions)}"


def test_gate_else_parallel_no_orphans():
    """查询状态门 否则体 并行：两臂都接到门的 else 输出口，无孤儿节点。"""
    flow = _compile(C5_GATE_ELSE_PARALLEL)
    orph = _orphans(flow)
    assert not orph, f"门否则并行不应有孤儿节点，实际 {orph}"


def test_timerange_else_parallel_no_orphans():
    """时间段门 否则体 并行：两臂都接到时间段门的 else 输出口，无孤儿节点。"""
    flow = _compile(C5_TIMERANGE_ELSE_PARALLEL)
    orph = _orphans(flow)
    assert not orph, f"时间段门否则并行不应有孤儿节点，实际 {orph}"
