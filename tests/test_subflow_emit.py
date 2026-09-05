"""子流程发射修复测试（Task #267）。

验证 type=subflow 的发射把子流程实例 lid 作为 tail 返回，
使下游连线接到子流程输出口（而非 change 节点），从而透传返回值。
不依赖 live NR。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.subflows import get_subflow


DSL_HISTORY = """
场景: 测试历史子流程返回值
触发: sensor.front_door 有人
调用子流程: history_state_at(entity=light.study, at=昨晚23:12)
观测: 看结果
"""


def _find(nodes, **pred):
    return [n for n in nodes if all(n.get(k) == v for k, v in pred.items())]


def test_subflow_instance_is_tail():
    flow = compile_dsl(DSL_HISTORY)
    nodes = flow["nodes"]
    spec = get_subflow("history_state_at")
    sub_type = f"subflow:{spec.call['subflow_id']}"
    subs = _find(nodes, type=sub_type)
    assert subs, f"缺少历史子流程实例（{sub_type}）"
    sub = subs[0]
    changes = _find(nodes, type="change")
    assert changes, "缺少设置入参的 change 节点"
    # change → subflow 实例（cid→lid 已 connect）
    assert sub["id"] in changes[0]["wires"][0], "change 节点应连到子流程实例"

    # 下游观测节点应连到子流程实例的输出口（修复点：tail=lid 而非 cid）
    debugs = _find(nodes, type="debug")
    assert debugs, "缺少下游观测节点"
    assert debugs[0]["id"] in sub["wires"][0], (
        f"下游应连到子流程实例输出口，实得 sub.wires={sub['wires']}"
    )
    # 回归保护：change 节点的输出口不应直连下游（旧 bug 的表现）
    assert debugs[0]["id"] not in changes[0]["wires"][0], (
        "回归：下游不应绕开子流程直连 change 节点"
    )


if __name__ == "__main__":
    test_subflow_instance_is_tail()
    print("✅ test_subflow_emit 通过")
