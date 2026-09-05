#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bug-1 回归：畸形嵌套 list wires 不再触发 `unhashable type: 'list'` 崩溃。

覆盖三处遍历入口（lint 建图 / R17 / simulate / deploy 清理），并验证：
- 不再抛异常；
- 嵌套中的合法字符串 id 能被恢复（_clean_wires / _flat_wire_targets）；
- 真正悬空引用仍被 R17 检出（没因为拍平而吞掉检测）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow, _flat_wire_targets
from autoflow_gateway.flow_simulator import simulate_flow
from autoflow_gateway.gateway import Gateway


def _mk(nodes):
    return {"id": "flow1", "label": "t", "nodes": nodes}


class TestNestedWiresNoCrash(unittest.TestCase):
    def test_lint_nested_wires_no_crash_and_detects_dangling(self):
        """嵌套 list wires 传给 lint_flow 不应抛异常，且仍能检出真正悬空引用。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "wires": [[["n_a"], "n_b"]]},
            {"id": "n_a", "type": "debug", "z": "flow1", "wires": []},
            # n_b 的 wires 内嵌 [["ghost"]]：ghost 真悬空，应被 R17 抓到
            {"id": "n_b", "type": "debug", "z": "flow1", "wires": [["ghost"]]},
        ])
        issues = lint_flow(flow)  # 此前会抛 unhashable type: 'list'
        r17 = [i for i in issues if i["rule"] == "R17"]
        self.assertEqual(len(r17), 1, r17)
        self.assertIn("ghost", r17[0]["message"])

    def test_simulate_nested_wires_no_crash(self):
        """嵌套 list wires 传给 simulate_flow 不应崩溃，且动作可达性正确。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [[["n_a"]]]},
            {"id": "n_a", "type": "api-call-service", "z": "flow1",
             "wires": [], "domain": "light", "service": "turn_on",
             "entityId": "light.x"},
        ])
        res = simulate_flow(flow)  # 此前会抛 unhashable type: 'list'
        self.assertIn("reachable_actions", res)
        self.assertIn("n_a", res.get("reachable_actions", []))

    def test_clean_wires_nested_recovers_valid(self):
        """_clean_wires 拍平嵌套 list，仅保留 valid_ids 内的字符串 id。"""
        wires = [["n2", ["n3"]], [["n4"], "gone"]]
        cleaned = Gateway._clean_wires(wires, {"n2", "n3", "n4"})
        self.assertEqual(cleaned, [["n2", "n3"], ["n4"]])

    def test_clean_wires_degenerate_form_safe(self):
        """退化单输出形态 [t1, t2] 也安全（不抛、不丢合法 id）。"""
        wires = ["n1", ["n2"]]
        cleaned = Gateway._clean_wires(wires, {"n1", "n2"})
        self.assertEqual(cleaned, ["n1", "n2"])

    def test_flat_wire_targets_helper(self):
        self.assertEqual(_flat_wire_targets([["a"], "b", None, 3]), ["a", "b"])
        self.assertEqual(_flat_wire_targets("x"), [])
        self.assertEqual(_flat_wire_targets(None), [])
        self.assertEqual(_flat_wire_targets([["deep", ["deeper"]]]), ["deep", "deeper"])


if __name__ == "__main__":
    unittest.main()
