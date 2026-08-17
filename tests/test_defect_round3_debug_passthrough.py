"""round3 Bug D 回归测试：非终端 观测 必须 pass-through 转发，不能变成 sink。

锁死一类已修的【静默运行态断流】：NR debug 节点默认 passthrough=false，即「不把
消息转发到输出口」。旧实现 emit 的 debug 节点没显式设 passthrough，于是非终端的
观测（后面还跟着分支/动作/观测）会变成 sink——接线看着正确，运行态其下游节点
永远收不到 msg、静默不执行，lint/闸门全过，极难察觉。

修复后预期：debug 节点 passthrough=True，且非终端观测的 wires 指向后续节点
（仅最后一个终端观测的 wires 为 [[]]）。

运行：python tests/test_defect_round3_debug_passthrough.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile

# 两个连续观测：第一个非终端（后面还有观测），第二个终端。
TWO_DEBUGS = """
场景: BugD_非终端观测
触发: inject
观测: 第一观测点
观测: 第二观测点
"""

# 观测串在分支之前：观测 打印X → 再 分支 判断（最常见的串行用法）。
DEBUG_THEN_BRANCH = """
场景: BugD_观测后接分支
触发: inject
观测: 打印当前状态
分支: payload.ok == true
  观测: 命中
否则:
  观测: 未命中
"""

# 终端观测（其后无节点）也应 passthrough=True（转发到空口无害，保持行为一致）。
SOLO_DEBUG = """
场景: BugD_终端观测
触发: inject
观测: 仅此一个观测
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _debugs(flow):
    return [n for n in flow["nodes"] if n["type"] == "debug"]


def test_non_terminal_debug_passthrough_true():
    """核心修复：非终端观测 passthrough 必须为 True（否则下游断流）。"""
    flow = _compile(TWO_DEBUGS)
    dbgs = _debugs(flow)
    assert len(dbgs) == 2, f"应编译出 2 个 debug 节点，实际 {len(dbgs)}"
    for d in dbgs:
        assert d.get("passthrough") is True, (
            f"debug '{d.get('name')}' passthrough 必须为 True，实际 {d.get('passthrough')}"
        )


def test_non_terminal_debug_wires_to_next_node():
    """非终端观测的 wires 必须指向后续节点（第一个→第二个），仅终端的 wires=[[]]。"""
    flow = _compile(TWO_DEBUGS)
    dbgs = _debugs(flow)
    first, second = dbgs[0], dbgs[1]
    # 第一个非终端：wires[0] 非空（连到第二个）
    assert first["wires"] and first["wires"][0], (
        f"非终端观测应连到后续节点，实际 wires={first['wires']}"
    )
    # 第二个终端：wires=[[]]
    assert second["wires"] == [[]], f"终端观测 wires 应为 [[]]，实际 {second['wires']}"


def test_debug_before_branch_restores_passthrough():
    """观测 后接 分支：观测转发恢复，分支开关能从上游拿到 msg 正常求值。"""
    flow = _compile(DEBUG_THEN_BRANCH)
    dbgs = _debugs(flow)
    assert dbgs, "应存在 debug 节点"
    for d in dbgs:
        assert d.get("passthrough") is True, "观测 后接 分支 场景里所有观测都应 passthrough=True"
    sws = [n for n in flow["nodes"] if n["type"] == "switch"]
    assert sws, "应存在后续 分支 节点（验证观测未截断数据流）"
    assert any(r.get("t") == "jsonata_exp" or r.get("t") == "eq"
               for s in sws for r in s.get("rules", [])), "分支 条件规则应正常生成"


def test_solo_terminal_debug_passthrough_true():
    """终端观测也显式 passthrough=True（转发空口无害，行为统一）。"""
    flow = _compile(SOLO_DEBUG)
    dbgs = _debugs(flow)
    assert len(dbgs) == 1
    assert dbgs[0].get("passthrough") is True
    assert dbgs[0]["wires"] == [[]]
