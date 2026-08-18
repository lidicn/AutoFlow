"""round3 Bug B / C4 回归测试：线性链顺序 2×history_* 子流程互相覆盖 msg.payload。

锁死一类【静默失败】：history_* 子流程把答案「替换式」写回 msg.payload，
后调用者整体覆盖前调用者。纯线性序列里顺序调 2 个不同 history_* 时，第二个会
静默抹掉第一个输出 → 后续若读第一个字段则恒 undefined、分支永假且不报错。

历史演进：
- round3：纯线性链顺序 2×history 仅给 warning 级 C_HISTORY_CLOBBER（fail-open，坏 flow 仍生成）。
- C4 (iss_3e5f462d01，见 BUG_REPORT.md §1.2)：将 C_HISTORY_CLOBBER 由 warning 升级为
  硬拦截（fail-closed）—— 顺序调 2 个不同 history_* 且前者输出未被中间『分支/提取』
  消费 → 编译期直接 DSLError，不再放行。故本文件两个探针由「断言 warning」升级为
  「断言抛 C_HISTORY_CLOBBER」。

安全写法（不误拒）：history_A → 分支(消费 A) → history_B，或 history_A → 提取(A) → history_B。
单 history 调用不误报。

运行：python tests/test_defect_round3_history_clobber.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile, C_HISTORY_CLOBBER, DSLError

# 线性链顺序 2 个不同 history_*（先 occurred，后 state_at）
SEQ_TWO_HISTORY = """
场景: BugB_顺序双history
触发: inject
调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
调用子流程: history_state_at(entity=switch.d4f0eaeab731_switch, at=现在)
观测: 结果
"""

# 单个 history 调用（其后接非 history 步骤）→ 不应误报
SINGLE_HISTORY = """
场景: BugB_单history
触发: inject
调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
观测: 结果
"""

# history 之间插入非 history 步骤后再次 history（仍是顺序 2×history）→ 应告警
HISTORY_NONHISTORY_HISTORY = """
场景: BugB_history间隔history
触发: inject
调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
观测: 中间打印
调用子流程: history_state_at(entity=switch.d4f0eaeab731_switch, at=现在)
观测: 结果
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _clobber_warnings(flow):
    return [l for l in flow.get("lint", []) if l.get("rule") == C_HISTORY_CLOBBER]


def test_sequential_two_history_raises_clobber():
    """C4 升级（fail-closed）：线性链顺序 2 个不同 history_* 必须硬拦截 DSLError C_HISTORY_CLOBBER。
    旧实现仅 warning 级 lint（fail-open），坏 flow 仍生成；现改为编译期拒绝并引导嵌套/提取暂存。"""
    try:
        _compile(SEQ_TWO_HISTORY)
    except DSLError as e:
        assert getattr(e, "code", None) == C_HISTORY_CLOBBER, (
            f"应抛 C_HISTORY_CLOBBER，实际 {getattr(e, 'code', None)}")
        return
    raise AssertionError("顺序 2×history 未被编译期拦截（应抛 C_HISTORY_CLOBBER）")


def test_single_history_no_false_positive():
    """单 history 调用（其后非 history 步骤）不应误报 C_HISTORY_CLOBBER、也不应抛错。"""
    flow = _compile(SINGLE_HISTORY)
    warns = _clobber_warnings(flow)
    assert not warns, f"单 history 调用不应告警，实际 {warns}"


def test_history_then_nonhistory_then_history_raises():
    """history-非history-history 仍属顺序 2×history（中间无『分支/提取』消费前者）→ 硬拦截。"""
    try:
        _compile(HISTORY_NONHISTORY_HISTORY)
    except DSLError as e:
        assert getattr(e, "code", None) == C_HISTORY_CLOBBER, (
            f"应抛 C_HISTORY_CLOBBER，实际 {getattr(e, 'code', None)}")
        return
    raise AssertionError("history-非history-history 未被编译期拦截（应抛 C_HISTORY_CLOBBER）")
