"""nr_client 护栏增强测试（A4，离线）。

运行：python tests/test_nr_client_guard.py
验证 _validate_subflow_ports（防 forEach 崩溃）与 _diff_flows（dry_run 预览）。
均不触真实 NR —— 仅纯函数逻辑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "autoflow_gateway" / "lib"))

from nr_client import NodeRedClient

client = NodeRedClient("http://dummy:1880")  # 不触网，仅构造


def test_subflow_ports_bad_number():
    flows = [{"id": "sf", "type": "subflow", "name": "坏", "out": [1], "in": [], "nodes": []}]
    probs = client._validate_subflow_ports(flows)
    assert probs, "out=[1] 应报告问题"
    assert any("sf" in p for p in probs)


def test_subflow_ports_empty_ok():
    flows = [{"id": "sf", "type": "subflow", "name": "好", "out": [], "in": [], "nodes": []}]
    assert client._validate_subflow_ports(flows) == [], "out=[] 应无问题"


def test_subflow_ports_valid_ok():
    flows = [{"id": "sf", "type": "subflow", "name": "好",
              "out": [{"x": 0, "y": 0, "wires": [["n"]]}], "in": [], "nodes": []}]
    assert client._validate_subflow_ports(flows) == [], "合法端口对象应无问题"


def test_subflow_ports_non_list():
    flows = [{"id": "sf", "type": "subflow", "name": "坏", "out": "x", "in": [], "nodes": []}]
    probs = client._validate_subflow_ports(flows)
    assert probs and "不是数组" in probs[0]


def test_diff_add_modify_remove():
    live = [
        {"id": "a", "type": "inject", "x": 1},
        {"id": "c", "type": "inject", "x": 1},
    ]
    proposed = [
        {"id": "a", "type": "inject", "x": 2},   # modified
        {"id": "b", "type": "inject", "x": 1},   # added
        # c removed
    ]
    d = client._diff_flows(live, proposed)
    assert d["modified"] == ["a"], d
    assert d["added"] == ["b"], d
    assert d["removed"] == ["c"], d
    assert d["modified_count"] == 1 and d["added_count"] == 1 and d["removed_count"] == 1


def test_diff_no_change():
    live = [{"id": "a", "type": "inject", "x": 1}]
    proposed = [{"id": "a", "type": "inject", "x": 1}]
    d = client._diff_flows(live, proposed)
    assert d["added_count"] == 0 and d["removed_count"] == 0 and d["modified_count"] == 0


if __name__ == "__main__":
    test_subflow_ports_bad_number()
    test_subflow_ports_empty_ok()
    test_subflow_ports_valid_ok()
    test_subflow_ports_non_list()
    test_diff_add_modify_remove()
    test_diff_no_change()
    print("✅ test_nr_client_guard 全部通过")
