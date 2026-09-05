"""C4 (iss_3e5f462d01 / high) 回归测试：顺序双 history_* 子流程 fail-closed 硬拦截。

锁死 BUG_REPORT.md §1.2 的【fail-open 未拦截】缺陷：history_* 子流程把答案「替换式」写回
msg.payload，顺序调 2 个不同 history_* 且前者输出未被中间『分支/提取』消费 → 后者整体覆盖
前者 → 前者字段恒 undefined、分支永假、动作永不执行、运行期不报错。

旧实现仅给 warning 级 C_HISTORY_CLOBBER lint（fail-open，坏 flow 仍生成）。C4 升级为
编译期硬拦截（fail-closed）：顺序调 2 个不同 history_* 直接抛 DSLError C_HISTORY_CLOBBER。

消费感知（不误拒）：若第一个 history 的输出字段被中间『分支』(条件引用) 或『提取』(暂存到变量)
消费，则顺序调第二个 history 是安全的，不报错：
  - history_A → 分支(消费 A) → history_B
  - history_A → 提取(暂存 A) → history_B

运行：python tests/test_defect_c4_history_failclosed.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile, C_HISTORY_CLOBBER, DSLError

_HDR = "触发: inject\n"
_HIST_A = "调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)"
_HIST_B = "调用子流程: history_state_at(entity=switch.d4f0eaeab731_switch, at=现在)"


# C4 坏：报告最小复现——顺序两 history，中间无分支消费前者，分支读被覆盖的字段
UNSAFE_LINEAR = _HDR + (
    f"{_HIST_A}\n"
    f"{_HIST_B}\n"
    "分支: payload.occurred == true\n"
    "  动作: light.turn_on(light.a)\n"
)

# C4 坏：顺序两 history + 观测（无任何消费），前者输出被静默丢弃
UNSAFE_LINEAR_OBSERVE = _HDR + (
    f"{_HIST_A}\n"
    f"{_HIST_B}\n"
    "观测: 结果\n"
)

# C4 安全：history_A → 分支(消费 occurred) → history_B
SAFE_VIA_BRANCH = _HDR + (
    f"{_HIST_A}\n"
    "分支: payload.occurred == true\n"
    "  动作: light.turn_on(light.a)\n"
    "否则:\n"
    "  动作: light.turn_off(light.a)\n"
    f"{_HIST_B}\n"
    "  动作: light.turn_on(light.b)\n"
)

# C4 安全：history_A → 提取(暂存 occurred) → history_B
SAFE_VIA_EXTRACT = _HDR + (
    f"{_HIST_A}\n"
    "提取: occ = payload.occurred\n"
    f"{_HIST_B}\n"
    "分支: payload.occ == true\n"
    "  动作: light.turn_on(light.b)\n"
    "否则:\n"
    "  动作: light.turn_off(light.b)\n"
)

# C4 单 history：不误报、不误拒
SINGLE_HISTORY = _HDR + (
    f"{_HIST_A}\n"
    "分支: payload.occurred == true\n"
    "  动作: light.turn_on(light.a)\n"
)


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _inbound(flow):
    inc = {n["id"]: set() for n in flow["nodes"]}
    for n in flow["nodes"]:
        for grp in (n.get("wires") or []):
            for t in (grp or []):
                if t in inc:
                    inc[t].add(n["id"])
    return inc


def _orphans(flow):
    return [(n["id"], n["type"], n.get("name")) for n in flow["nodes"]
            if n["type"] not in ("inject", "tab", "comment") and not _inbound(flow)[n["id"]]]


def test_unsafe_linear_history_raises():
    """C4 核心：顺序两 history（中间无消费）+ 分支读被覆盖字段 → 硬拦截 C_HISTORY_CLOBBER。"""
    try:
        _compile(UNSAFE_LINEAR)
    except DSLError as e:
        assert getattr(e, "code", None) == C_HISTORY_CLOBBER, (
            f"应抛 C_HISTORY_CLOBBER，实际 {getattr(e, 'code', None)}")
        return
    raise AssertionError("顺序双 history（读被覆盖字段）未被编译期拦截")


def test_unsafe_linear_history_observe_raises():
    """顺序两 history + 仅观测（无任何消费）→ 前者输出被静默丢弃，同样硬拦截。"""
    try:
        _compile(UNSAFE_LINEAR_OBSERVE)
    except DSLError as e:
        assert getattr(e, "code", None) == C_HISTORY_CLOBBER, (
            f"应抛 C_HISTORY_CLOBBER，实际 {getattr(e, 'code', None)}")
        return
    raise AssertionError("顺序双 history（无消费）未被编译期拦截")


def test_safe_via_branch_compiles():
    """安全写法 history_A → 分支(消费 A) → history_B 必须编译通过，且无孤儿节点。"""
    flow = _compile(SAFE_VIA_BRANCH)
    assert flow and flow.get("nodes"), "安全分支消费写法应编译通过"
    assert not _orphans(flow), f"安全写法不应有孤儿节点，实际 {_orphans(flow)}"


def test_safe_via_extract_compiles():
    """安全写法 history_A → 提取(暂存 A) → history_B 必须编译通过，且无孤儿节点。"""
    flow = _compile(SAFE_VIA_EXTRACT)
    assert flow and flow.get("nodes"), "安全提取暂存写法应编译通过"
    assert not _orphans(flow), f"安全写法不应有孤儿节点，实际 {_orphans(flow)}"


def test_single_history_no_false_positive():
    """单个 history 调用（后续分支引用其字段属正常消费）不误报、不误拒。"""
    flow = _compile(SINGLE_HISTORY)
    assert flow and flow.get("nodes"), "单 history 调用应编译通过"
    warns = [l for l in flow.get("lint", []) if l.get("rule") == C_HISTORY_CLOBBER]
    assert not warns, f"单 history 调用不应告警，实际 {warns}"
