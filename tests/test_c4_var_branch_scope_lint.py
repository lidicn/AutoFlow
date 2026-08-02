#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C4 回归：变量↔分支作用域一致性结构 lint（R26, warning 级）。

压测报告 C4 (iss_bbf90c6afa) 的剩余结构 lint：白盒手写 flow 里 change 把变量写到
flow/global 作用域，但下游 switch 从 msg 作用域读同名变量（propertyType=="msg" 且
property 为裸名）→ 变量永远 undefined → 死分支。这是 C2 编译器缺陷在白箱路径的等价形态。

R26 仅 warning（不硬拦），且要求 switch 读的是「裸 msg 字段」（无 "." 嵌套），精确命中
C2 形态、避免误伤真实 HA 读取（几乎都是 msg.payload.x 嵌套）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow


def _r26(flow):
    return [i for i in lint_flow(flow) if i.get("rule") == "R26"]


class TestC4VarBranchScope(unittest.TestCase):
    def test_flow_var_read_by_msg_switch_flagged(self):
        """change 写 flow.var，switch 从 msg.var 裸读 → R26（C2 白盒形态）。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "flow", "to": "1", "tot": "num"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        issues = _r26({"nodes": nodes})
        self.assertTrue(issues, "flow 变量被 switch 从 msg 裸读必须报 R26")
        self.assertEqual(issues[0]["node_id"], "sw")

    def test_global_scope_write_also_flagged(self):
        """change 写 global.var，switch 从 msg.var 读 → 同样 R26。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "global", "to": "x", "tot": "str"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "x"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertTrue(_r26({"nodes": nodes}), "global 变量被 msg 裸读必须报 R26")

    def test_correct_flow_scope_read_passes(self):
        """change 写 flow.var，switch 也从 flow 读 → 作用域一致，不误报。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "flow", "to": "1", "tot": "num"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "flow",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r26({"nodes": nodes}), [],
                         "作用域一致（都 flow）不应误报")

    def test_msg_var_read_by_msg_switch_passes(self):
        """change 写 msg.var，switch 从 msg 读 → 一致，不误报。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "msg", "to": "1", "tot": "num"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r26({"nodes": nodes}), [],
                         "msg→msg 一致不应误报")

    def test_nested_msg_read_not_flagged(self):
        """switch 读 msg.payload.var（嵌套）→ 非裸字段，不误伤真实 HA 读取。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "flow", "to": "1", "tot": "num"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "payload.var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r26({"nodes": nodes}), [],
                         "msg.payload.var 嵌套读取不应误报")

    def test_no_change_nodes_passes(self):
        """流里无 change 节点 → 0 R26。"""
        nodes = [
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r26({"nodes": nodes}), [],
                         "无 change 节点不应误报")

    def test_v2_switch_per_rule_msg_read_flagged(self):
        """switch v2：某条规则从 msg 裸读 flow 变量 → R26（按规则级 propertyType）。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "mode", "pt": "flow", "to": "a", "tot": "str"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "x", "propertyType": "msg",
             "rules": [
                 {"t": "eq", "v": "a", "property": "mode", "propertyType": "msg"},
                 {"t": "else"},
             ], "wires": [[], []]},
            {"id": "t", "type": "tab"},
        ]
        issues = _r26({"nodes": nodes})
        self.assertTrue(issues, "v2 规则级 msg 裸读 flow 变量必须报 R26")

    def test_r26_is_warning_not_error(self):
        """R26 为 warning 级（不硬拦，避免误伤合法白盒流）。"""
        nodes = [
            {"id": "chg", "type": "change", "z": "t", "rules": [
                {"t": "set", "p": "var", "pt": "flow", "to": "1", "tot": "num"},
            ]},
            {"id": "sw", "type": "switch", "z": "t",
             "property": "var", "propertyType": "msg",
             "rules": [{"t": "eq", "v": "1"}], "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        issues = _r26({"nodes": nodes})
        self.assertTrue(issues)
        self.assertTrue(all(i["level"] == "warning" for i in issues),
                        "R26 必须是 warning 级，不应阻塞合法白盒流")


if __name__ == "__main__":
    unittest.main()
