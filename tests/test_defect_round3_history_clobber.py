"""round3 Bug B 回归测试：线性链顺序 2×history_* 子流程互相覆盖 msg.payload。

锁死一类已修的【静默失败】：history_* 子流程把答案「替换式」写回 msg.payload，
后调用者整体覆盖前调用者。纯线性序列里顺序调 2 个不同 history_* 时，第二个会
静默抹掉第一个输出 → 后续若读第一个字段则恒 undefined、分支永假且不报错。

旧实现仅在 switch 节点内引用 ≥2 不同 history 字段时 fail-loud（_check_history_clobber_in_switch）；
纯线性链顺序 2×history 漏检 → 用户无感。

修复后预期：线性链顺序调用 2 个不同 history_* 子流程时，compile 产出的 lint 含
warning 级 C_HISTORY_CLOBBER（引导改用嵌套 / 提取暂存），且单 history 调用不误报。

运行：python tests/test_defect_round3_history_clobber.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile, C_HISTORY_CLOBBER

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


def test_sequential_two_history_warns_clobber():
    """核心修复：线性链顺序 2 个不同 history_* 必须给出 C_HISTORY_CLOBBER 告警。"""
    flow = _compile(SEQ_TWO_HISTORY)
    warns = _clobber_warnings(flow)
    assert warns, "顺序 2×history 应触发 C_HISTORY_CLOBBER 告警，实际 lint 无该告警"
    assert warns[0]["level"] == "warning", "C_HISTORY_CLOBBER 应为 warning 级"
    # 引导文案应提示两种修正手段
    msg = warns[0]["message"]
    assert "嵌套" in msg or "提取" in msg, "告警应引导嵌套 / 提取暂存写法"


def test_single_history_no_false_positive():
    """单 history 调用（其后非 history 步骤）不应误报 C_HISTORY_CLOBBER。"""
    flow = _compile(SINGLE_HISTORY)
    warns = _clobber_warnings(flow)
    assert not warns, f"单 history 调用不应告警，实际 {warns}"


def test_history_then_nonhistory_then_history_warns():
    """history 之间插入打印等步骤后再次 history —— 仍是顺序 2×history，应告警。"""
    flow = _compile(HISTORY_NONHISTORY_HISTORY)
    warns = _clobber_warnings(flow)
    assert warns, "history-非history-history 仍属顺序 2×history，应告警"
