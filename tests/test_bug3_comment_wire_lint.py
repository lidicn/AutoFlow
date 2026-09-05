#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bug3 回归：comment 节点被当作消息中转必须被 lint 覆盖（R25, warning 级）。

旧实现结构 lint 只检查功能性节点连线（R17 悬空 wire / R13 孤立动作等），
却漏掉 `type=comment` 节点带 wires / 被接入主链。Node-RED 的 comment 节点
不转发消息，导致下游（debug）永不触发 —— 静默逻辑断裂且零告警。

对应压测报告 Bug-3（iss_649c9aae1a, low）；报告建议 warning 级覆盖。
注：编译器侧（DSL→NR）已在 `_emit_body`/`_Emitter` 跳过 comment，本规则
只补齐「白盒原生手写」这条无人值守的路径。

检测两类：
  (a) comment 节点自身 wires 非空（它试图向下游转发）
  (b) 任何功能节点的 wires 把消息送入 comment 节点 id（它作为某条连线目标）
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow


def _r25(flow):
    return [i for i in lint_flow(flow) if i.get("rule") == "R25"]


class TestBug3CommentWireLint(unittest.TestCase):
    def test_comment_with_outgoing_wires_flagged(self):
        """报告场景 inject→comment(带 wires)→debug：comment 自身 wires 非空 → R25。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["cm"]]},
            {"id": "cm", "type": "comment", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        issues = _r25({"nodes": nodes})
        self.assertTrue(issues, "comment 带 outgoing wires 必须报 R25")
        # comment 节点自身 wires 非空 → 命中 (a)
        self.assertTrue(any(i["node_id"] == "cm" for i in issues),
                        "comment 节点自身应被 (a) 命中")

    def test_comment_as_wire_target_flagged(self):
        """功能节点把消息送入 comment（comment 自身 wires 空）→ 仍报 R25 (b)。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["cm"]]},
            {"id": "cm", "type": "comment", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        issues = _r25({"nodes": nodes})
        self.assertTrue(issues, "comment 作为连线目标必须报 R25")
        self.assertTrue(any(i["node_id"] == "inj" for i in issues),
                        "指向 comment 的功能节点应被 (b) 命中")

    def test_pure_annotation_comment_passes(self):
        """纯注释 comment（wires=[] 且未被任何节点指向）→ 0 R25。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "cm", "type": "comment", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r25({"nodes": nodes}), [],
                         "纯注释 comment 不应误报")

    def test_comment_with_empty_wires_passes(self):
        """comment 字段缺失/为空 wires → 不误报（防御未写 wires 的合法手搓）。"""
        nodes = [
            {"id": "cm", "type": "comment", "z": "t"},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r25({"nodes": nodes}), [],
                         "wires 缺失的 comment 不应误报")

    def test_comment_wires_empty_inner_list_passes(self):
        """WB4 Bug#5 回归：comment wires=[[]]（内层空列表）必须不误报。

        Node-RED 导出时 comment 节点 wires 常写成 [[]]（外层长度=1，但内层 0 个真实目标）。
        旧实现用 len(wires)（外层）判断 → 误报『带 1 条 outgoing wires』。
        修复后用内层真实 target 数判断 → [[]] 不报。
        """
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "cm", "type": "comment", "z": "t", "wires": [[]]},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r25({"nodes": nodes}), [],
                         "wires=[[]] 的纯注释 comment 不应误报为带 outgoing wires")

    def test_comment_wires_real_inner_target_flagged(self):
        """对照：comment wires=[['dbg']]（内层有真实目标）→ 仍应报 R25 (a)。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["cm"]]},
            {"id": "cm", "type": "comment", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        issues = _r25({"nodes": nodes})
        self.assertTrue(any(i["node_id"] == "cm" for i in issues),
                        "comment wires 内层有真实目标时必须仍被 (a) 命中")

    def test_no_comment_nodes_passes(self):
        """流里根本没有 comment 节点 → 0 R25。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        self.assertEqual(_r25({"nodes": nodes}), [],
                         "无 comment 的流不应误报")

    def test_multiple_comments_one_relay(self):
        """多个 comment 混排：只有被接入主链的才报，纯注释不报。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["relay"]]},
            {"id": "relay", "type": "comment", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "note", "type": "comment", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        issues = _r25({"nodes": nodes})
        flagged_ids = {i["node_id"] for i in issues}
        self.assertIn("relay", flagged_ids, "relay comment 必须命中")
        self.assertIn("inj", flagged_ids, "指向 relay 的 inject 必须命中")
        self.assertNotIn("note", flagged_ids, "纯注释 comment 不应命中")

    def test_r25_is_warning_not_error(self):
        """R25 为 warning 级（对齐报告 low 严重度，不进白盒硬拦集）。"""
        nodes = [
            {"id": "inj", "type": "inject", "z": "t", "wires": [["cm"]]},
            {"id": "cm", "type": "comment", "z": "t", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "t", "wires": []},
            {"id": "t", "type": "tab"},
        ]
        issues = _r25({"nodes": nodes})
        self.assertTrue(issues)
        self.assertTrue(all(i["level"] == "warning" for i in issues),
                        "R25 必须是 warning 级，不应阻塞合法白盒流")


if __name__ == "__main__":
    unittest.main()
