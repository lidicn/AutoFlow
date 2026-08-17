"""round2 Bug D 回归测试：并行 块 payload 隔离（子流输出丢弃 / 块内观测只见入口 msg）。

锁死一类已核实的【模型局限 + 静默 footgun】：DSL 的 并行 块只把每个子步骤作为
【独立叶子】从同一上游扇出，没有 per-branch 子链、也无隐式 join。编译产物图实测：
  · 并行块内的 调用子流程(history_*) → 子流实例节点 wires=[[]]，输出永不被消费（丢弃）；
  · 并行块内的 同一级 观测(debug) 直接接入口(inject 时间戳)，只见原始 msg，
    看不到任何 sibling 子流的输出；块后顺序节点同理只见入口 msg。
与报告运行时观测完全吻合（payload 全是 inject 时间戳，无 history_* 输出）。

本轮处置（低成本增量）：不重构并行模型，改在编译期给出 C_PARALLEL_PAYLOAD_ISOLATION
告警，把静默 footgun 变成可见提示，引导用户把要消费子流输出的步骤移出并行块。

运行：python tests/test_defect_round2_parallel.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile

PARALLEL_SUBFLOW_DEBUG = """
场景: BugD_并行子流与观测
触发: inject
并行:
  调用子流程: history_occurred(entity=switch.d4f0eaeab731_switch, start=1小时前, end=现在, state=on)
  观测: 并行内观测
  调用子流程: history_state_at(entity=switch.d4f0eaeab731_switch, at=现在)
  观测: 并行内观测2
观测: 块后顺序观测
"""

PARALLEL_TWO_ACTIONS = """
场景: BugD_纯动作并行无footgun
触发: inject
并行:
  动作: light.turn_on(light.living_main, brightness=80)
  动作: light.turn_off(light.living_main)
观测: 收尾
"""


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def test_parallel_subflow_outputs_discarded():
    """实测证据：并行块内子流实例节点输出口必须是空 wires（无 join，结果被丢弃）。"""
    flow = _compile(PARALLEL_SUBFLOW_DEBUG)
    subflow_nodes = [n for n in flow["nodes"]
                     if n["type"].startswith("subflow:")]
    assert subflow_nodes, "并行块内应编译出子流实例节点"
    for nd in subflow_nodes:
        flat = [d for arr in nd.get("wires", [[]]) for d in arr]
        assert flat == [], \
            f"并行块内子流 {nd.get('name')} 的输出被接走（修复前不该发生）：{flat}"


def test_parallel_subflow_debug_triggers_warning():
    """并行块含子流调用 / 观测 → 触发 C_PARALLEL_PAYLOAD_ISOLATION 告警。"""
    flow = _compile(PARALLEL_SUBFLOW_DEBUG)
    codes = [l.get("rule") for l in flow.get("lint", [])]
    assert "C_PARALLEL_PAYLOAD_ISOLATION" in codes, \
        f"未给出并行隔离告警；lint={flow.get('lint')}"


def test_parallel_two_actions_no_false_warning():
    """纯动作并行（无子流/观测）不应误报并行隔离告警（无过度告警/回归）。"""
    flow = _compile(PARALLEL_TWO_ACTIONS)
    codes = [l.get("rule") for l in flow.get("lint", [])]
    assert "C_PARALLEL_PAYLOAD_ISOLATION" not in codes, \
        f"纯动作并行不应告警；lint={flow.get('lint')}"
