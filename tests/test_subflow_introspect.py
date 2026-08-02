#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""introspect_nr_subflow 自省抽取单测（#576，离线 mock flows JSON，不触真实 NR）。

验证：从 mock 的 NR flows（含 subflow def + 内部节点）能正确抽取
  - env_requirements（def.env → 子流程级配置变量）
  - input_schema（内部 function/change 读 msg.<x> → 调用方入参）
  - 找不到子流程 → ok:False
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.subflows import _introspect_nr_subflow_from_flows


MOCK_FLOWS = [
    {
        "type": "subflow", "id": "sf1", "name": "我的子流程",
        "in": [{"x": 0, "y": 0, "wires": [{"id": "fn1"}]}],
        "out": [{"x": 0, "y": 0, "wires": [{"id": "fn2", "port": 0}]}],
        "env": [
            {"name": "MY_TOKEN", "type": "str"},
            {"name": "MY_URL", "type": "str"},
        ],
    },
    {"id": "fn1", "type": "function", "z": "sf1",
     "func": "const a = msg.device;\nconst b = msg.room;\nmsg.payload = a;"},
    {"id": "fn2", "type": "change", "z": "sf1",
     "rules": [{"t": "set", "p": "payload", "pt": "msg",
                "to": "msg.result", "tot": "msg"}]},
]


class TestIntrospectSubflow(unittest.TestCase):
    def test_extract_env_and_inputs(self):
        r = _introspect_nr_subflow_from_flows(MOCK_FLOWS, "sf1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["title"], "我的子流程")
        self.assertEqual(r["in_ports"], 1)
        self.assertEqual(r["out_ports"], 1)
        self.assertEqual(r["internal_node_count"], 2)
        # env_requirements 来自 def.env
        self.assertEqual(r["env_requirements"], [
            {"name": "MY_TOKEN", "type": "str"},
            {"name": "MY_URL", "type": "str"},
        ])
        # input_schema 来自内部节点 msg.<x> 读取（payload 等信封字段被过滤）
        names = [p["name"] for p in r["input_schema"]]
        self.assertEqual(names, ["device", "result", "room"])
        for p in r["input_schema"]:
            self.assertEqual(p["type"], "str")
            self.assertFalse(p["required"])

    def test_not_found(self):
        r = _introspect_nr_subflow_from_flows(MOCK_FLOWS, "nope")
        self.assertFalse(r["ok"])
        self.assertIn("未找到", r["error"])

    def test_empty_def(self):
        # def 存在但无 env / 无内部节点 → 空列表而非报错
        flows = [{
            "type": "subflow", "id": "empty", "name": "空",
            "in": [], "out": [], "env": [],
        }]
        r = _introspect_nr_subflow_from_flows(flows, "empty")
        self.assertTrue(r["ok"])
        self.assertEqual(r["env_requirements"], [])
        self.assertEqual(r["input_schema"], [])
        self.assertEqual(r["in_ports"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
