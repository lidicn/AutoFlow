#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1(B1=R14 不可达节点) / B2(R15 环检测) 单元测试（纯静态，不触真实 HA/NR）。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow


def _mk(nodes):
    return {"id": "flow1", "label": "t", "nodes": nodes}


# R14 默认关闭，需显式开启（b1_unreachable=True）；封装方便调用
def _lint14(nodes):
    return lint_flow(_mk(nodes), b1_unreachable=True)


class TestR14Unreachable(unittest.TestCase):
    def test_linear_chain_all_reachable(self):
        """inject → function → change 全部可达，无 R14。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["fn1"]]},
            {"id": "fn1", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [[]]},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        self.assertEqual(r14, [], r14)

    def test_orphan_function_flagged(self):
        """主链 inject→change 正常；一个游离 function 无连线 → R14。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [[]]},
            {"id": "fn_orphan", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [[]]},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        self.assertEqual(len(r14), 1, r14)
        self.assertEqual(r14[0]["node_id"], "fn_orphan")
        self.assertEqual(r14[0]["level"], "warning")

    def test_disconnected_subgraph_all_dead(self):
        """整段子图悬空：单独的 switch+function 都不可达 → 各报 R14。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [[]]},
            # 悬空子图：sw2 无入边，fn2 只被 sw2 指向但 sw2 本身不可达
            {"id": "sw2", "type": "switch", "z": "flow1", "outputs": 2,
             "rules": [{"t": "eq", "v": "on"}, {"t": "else"}],
             "wires": [["fn2"], []]},
            {"id": "fn2", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [[]]},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        ids = {i["node_id"] for i in r14}
        self.assertEqual(ids, {"sw2", "fn2"}, r14)

    def test_entry_node_not_flagged(self):
        """独立的 trigger/server-state-changed（无入边）不应被 R14 判死。"""
        flow = _mk([
            {"id": "trg", "type": "server-state-changed", "z": "flow1",
             "entities": {"entity": ["light.x"]}, "wires": [[]]},
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [[]]},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        self.assertEqual(r14, [], r14)

    def test_config_node_not_flagged(self):
        """server / comment / ui_group 等配置节点无 wires，不应 R14。"""
        flow = _mk([
            {"id": "srv", "type": "server", "name": "HA"},
            {"id": "cmt", "type": "comment", "z": "flow1"},
            {"id": "ui", "type": "ui_group", "z": "flow1"},
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [[]]},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        self.assertEqual(r14, [], r14)

    def test_api_call_service_not_double_reported(self):
        """api-call-service 孤立由 R13(error) 独占，R14 不应重复报。"""
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "entityId": ["light.x"], "wires": [[]]},
        ])
        issues = _lint14(flow["nodes"])
        r13 = [i for i in issues if i["rule"] == "R13"]
        r14 = [i for i in issues if i["rule"] == "R14"]
        self.assertEqual(len(r13), 1, issues)
        self.assertEqual(r14, [], issues)

    def test_link_chain_reachable(self):
        """link in 是触发源；link in→function→link out 全部可达，无 R14。"""
        flow = _mk([
            {"id": "lin", "type": "link in", "z": "flow1", "links": [],
             "wires": [["fn1"]]},
            {"id": "fn1", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["lout"]]},
            {"id": "lout", "type": "link out", "z": "flow1",
             "links": ["external_link_in"], "wires": []},
        ])
        r14 = [i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]
        self.assertEqual(r14, [], r14)

    def test_r14_off_by_default(self):
        """R14 默认关闭：即便有孤立节点也不报，除非显式 b1_unreachable=True。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [[]]},
            {"id": "fn_orphan", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [[]]},
        ])
        # 默认关闭
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R14"], [])
        # 显式开启才报
        self.assertEqual(
            len([i for i in _lint14(flow["nodes"]) if i["rule"] == "R14"]), 1)


class TestR15Cycle(unittest.TestCase):
    def test_no_cycle_linear(self):
        flow = _mk([
            {"id": "a", "type": "inject", "z": "flow1", "wires": [["b"]]},
            {"id": "b", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["c"]]},
            {"id": "c", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [[]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual(r15, [], r15)

    def test_cycle_three_nodes(self):
        """A→B→C→A 形成环，三者都报 R15 error。"""
        flow = _mk([
            {"id": "a", "type": "inject", "z": "flow1", "wires": [["b"]]},
            {"id": "b", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["c"]]},
            {"id": "c", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload.x", "pt": "msg", "to": "1"}],
             "wires": [["a"]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual({i["node_id"] for i in r15}, {"a", "b", "c"}, r15)
        self.assertTrue(all(i["level"] == "error" for i in r15))

    def test_self_loop(self):
        """自环 A→A 报 R15，并提示「自环」。"""
        flow = _mk([
            {"id": "a", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["a"]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual(len(r15), 1, r15)
        self.assertIn("自环", r15[0]["message"])

    def test_controlled_loop_via_link_chain_skipped(self):
        """含 link out→link in 的受控循环（队列/调度器常用）不报 R15（误报防护）。"""
        flow = _mk([
            {"id": "lout", "type": "link out", "z": "flow1",
             "links": ["lin"], "wires": []},
            {"id": "lin", "type": "link in", "z": "flow1", "links": [],
             "wires": [["lout"]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual(r15, [], r15)

    def test_controlled_loop_with_delay_skipped(self):
        """function→delay→function 自触发（含 delay 节流）不报 R15。"""
        flow = _mk([
            {"id": "a", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["d"]]},
            {"id": "d", "type": "delay", "z": "flow1", "wires": [["a"]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual(r15, [], r15)

    def test_tight_loop_without_throttle_flagged(self):
        """无 delay/link 等节流的紧致环（function→function→function）报 R15 error。"""
        flow = _mk([
            {"id": "a", "type": "inject", "z": "flow1", "wires": [["b"]]},
            {"id": "b", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["c"]]},
            {"id": "c", "type": "function", "z": "flow1",
             "func": "return msg;", "outputs": 1, "wires": [["b"]]},
        ])
        r15 = [i for i in lint_flow(flow) if i["rule"] == "R15"]
        self.assertEqual({i["node_id"] for i in r15}, {"b", "c"}, r15)
        self.assertTrue(all(i["level"] == "error" for i in r15))


if __name__ == "__main__":
    unittest.main(verbosity=2)
