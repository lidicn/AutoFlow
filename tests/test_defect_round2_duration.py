"""round2 Bug C 回归测试：history_duration 子流输出字段名契约不符。

锁死一类已修的【静默错误结果】：history_duration 子流程的真实输出字段是
payload.total_seconds（另有 ratio/total_human），但用户常误写 payload.duration。
旧实现把 `$number(payload.duration) > 100` 原样编译 → 运行期 $number(duration)
恒 NaN → 比较恒 false → 漏报（即便灯组亮了几小时也报"不足100秒"），且 lint 全过、无声。

修复后预期：
  · 编译期把 payload.duration 自动重映射为 payload.total_seconds（消除静默 NaN）；
  · lint 给出 C_HISTORY_DURATION_FIELD 告警，引导用户显式改用 payload.total_seconds。

运行：python tests/test_defect_round2_duration.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile

DURATION_WRONG_FIELD = """
场景: BugC_duration字段误用
触发: inject
调用子流程: history_duration(entity=light.study_desk, start=1小时前, end=现在, state=on)
分支: $number(payload.duration) > 100
  观测: 超过100秒
否则:
  观测: 不足100秒
"""

DURATION_RIGHT_FIELD = """
场景: BugC_duration字段正确
触发: inject
调用子流程: history_duration(entity=light.study_desk, start=1小时前, end=现在, state=on)
分支: $number(payload.total_seconds) > 100
  观测: 超过100秒
否则:
  观测: 不足100秒
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _cond_rule(flow):
    sws = [n for n in flow["nodes"] if n["type"] == "switch"]
    # history_duration 输出在 payload.total_seconds，取引用该字段的 switch
    return next(s for s in sws
                if any("total_seconds" in r.get("v", "") or "duration" in r.get("v", "")
                       for r in s.get("rules", [])))


def test_wrong_duration_field_auto_remapped():
    """核心修复：payload.duration 被自动重映射为 payload.total_seconds，消除静默 NaN。"""
    flow = _compile(DURATION_WRONG_FIELD)
    sw = _cond_rule(flow)
    v = sw["rules"][0].get("v", "")
    assert "payload.total_seconds" in v, f"duration 未重映射为 total_seconds: {v}"
    assert "payload.duration" not in v, f"误写字段名被原样保留（修复前 bug）: {v}"


def test_wrong_duration_field_warns():
    """误用 payload.duration 应触发 C_HISTORY_DURATION_FIELD 告警，引导改法。"""
    flow = _compile(DURATION_WRONG_FIELD)
    codes = [l.get("rule") for l in flow.get("lint", [])]
    assert "C_HISTORY_DURATION_FIELD" in codes, \
        f"未给出 C_HISTORY_DURATION_FIELD 告警；lint={flow.get('lint')}"


def test_right_duration_field_no_warning():
    """显式用 payload.total_seconds 不应触发该告警（无过度告警/回归）。"""
    flow = _compile(DURATION_RIGHT_FIELD)
    codes = [l.get("rule") for l in flow.get("lint", [])]
    assert "C_HISTORY_DURATION_FIELD" not in codes, \
        f"正确写法不应告警；lint={flow.get('lint')}"
