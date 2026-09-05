# -*- coding: utf-8 -*-
"""#C-tab：tab 链接逆生成 Link API —— 纯函数自省测试（无网络 / 无 webui 依赖）。

覆盖：
- _parse_tab_url：完整编辑器链接 / 裸 hash / 裸 id / 带查询参数
- _introspect_nr_tab_from_flows：
  * link in 入口 → registerable，entry_id = link in 节点 id，params 推断自下游 msg.<x>
  * inject 入口 → registerable=False
  * http in 入口 → registerable=False
  * 无入口节点 → registerable=False
  * 未知 tab id → ok=False error
  * 信封字段（payload/topic…）被过滤，入口自身读取不计入
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.subflows import (
    _parse_tab_url, _introspect_nr_tab_from_flows,
)


def _flows(tab_id, entry_type, internal_extra=None):
    """构造最小 flows：一个 tab + 入口节点 + 可选下游节点。"""
    nodes = [{"id": "entry", "type": entry_type, "z": tab_id,
              "name": "入口", "wires": []}]
    if internal_extra:
        nodes.extend(internal_extra)
    return [{"id": tab_id, "type": "tab", "label": "智能语音播报队列"}, *nodes]


class TestParseTabUrl(unittest.TestCase):
    def test_full_editor_url(self):
        self.assertEqual(
            _parse_tab_url("http://<NAS_IP>:1990/#flow/e70a201b5f004927"),
            "e70a201b5f004927")

    def test_bare_hash(self):
        self.assertEqual(_parse_tab_url("#flow/abc123"), "abc123")

    def test_bare_id(self):
        self.assertEqual(_parse_tab_url("abc123"), "abc123")

    def test_with_query(self):
        self.assertEqual(_parse_tab_url("http://h:1990/#flow/xyz?foo=1"), "xyz")

    def test_empty(self):
        self.assertIsNone(_parse_tab_url(""))


class TestIntrospectLinkIn(unittest.TestCase):
    def test_registerable_with_params(self):
        flows = _flows("tab1", "link in", internal_extra=[
            {"id": "fn1", "type": "function", "z": "tab1",
             "func": "var t = msg.text; var v = msg.volume; return msg;"},
            {"id": "ch1", "type": "change", "z": "tab1",
             "rules": [{"to": "msg.payload", "p": "msg.voice"}]},
        ])
        r = _introspect_nr_tab_from_flows(flows, "tab1")
        self.assertTrue(r["ok"])
        self.assertTrue(r["registerable"])
        self.assertEqual(r["entry_kind"], "link_in")
        self.assertEqual(r["entry_id"], "entry")
        names = {p["name"] for p in r["params"]}
        self.assertEqual(names, {"text", "volume", "voice"})

    def test_reserved_msg_filtered(self):
        flows = _flows("tab2", "link in", internal_extra=[
            {"id": "fn1", "type": "function", "z": "tab2",
             "func": "var x = msg.payload; var y = msg.topic; var z = msg.custom;"},
        ])
        r = _introspect_nr_tab_from_flows(flows, "tab2")
        names = {p["name"] for p in r["params"]}
        self.assertEqual(names, {"custom"})  # payload/topic 属信封字段，被过滤

    def test_no_params(self):
        flows = _flows("tab3", "link in")
        r = _introspect_nr_tab_from_flows(flows, "tab3")
        self.assertTrue(r["registerable"])
        self.assertEqual(r["params"], [])


class TestIntrospectNonRegisterable(unittest.TestCase):
    def test_inject_entry(self):
        flows = _flows("tab4", "inject")
        r = _introspect_nr_tab_from_flows(flows, "tab4")
        self.assertTrue(r["ok"])
        self.assertFalse(r["registerable"])
        self.assertEqual(r["entry_kind"], "inject")
        self.assertIn("inject", r["reason"])

    def test_http_in_entry(self):
        flows = _flows("tab5", "http in")
        r = _introspect_nr_tab_from_flows(flows, "tab5")
        self.assertFalse(r["registerable"])
        self.assertEqual(r["entry_kind"], "http_in")

    def test_no_entry(self):
        flows = [
            {"id": "tab6", "type": "tab", "label": "x"},
            {"id": "n1", "type": "debug", "z": "tab6"},
        ]
        r = _introspect_nr_tab_from_flows(flows, "tab6")
        self.assertFalse(r["registerable"])
        self.assertIsNone(r["entry_kind"])

    def test_unknown_tab(self):
        r = _introspect_nr_tab_from_flows(
            [{"id": "other", "type": "tab"}], "missing")
        self.assertFalse(r["ok"])
        self.assertIn("missing", r["error"])


if __name__ == "__main__":
    unittest.main()
