#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T1 回归：run_e2e_trace 入参契约统一 + HA server 占位符注入。

压测报告 Bug-T1（iss_35b5d34da2, medium）：
  现象1：validate_flow/simulate_flow 接受裸节点数组，run_e2e_trace 对裸数组报
        "缺少 nodes" → 三姊妹工具入参形状不一致。
  现象2：即便以 {"nodes":[...]} 过 input，e2e 部署阶段 POST /flow -> 400，
        server 占位符 REPLACE_WITH_HA_SERVER 未替换（部署路径漏接注入逻辑 / 漂移）。

修复：
  - _normalize_e2e_flow_input：裸数组 / 字符串 / {"nodes":...} 统一为 {"nodes":...}
  - _inject_ha_server：deploy_raw 与 run_e2e_trace 系列共用单一占位符注入，消除漂移
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.gateway import Gateway


class _FakeCfg:
    nr_ha_server_id = ""


class _FakeNR:
    def __init__(self, server_id):
        self._sid = server_id

    def get_default_server_id(self):
        return self._sid


class TestT1NormalizeE2EInput(unittest.TestCase):
    """现象1：run_e2e_trace 应像 validate/simulate 一样接受裸节点数组。"""

    def test_bare_list_wrapped(self):
        nodes = [{"id": "a", "type": "inject"}]
        flow, err = Gateway._normalize_e2e_flow_input(nodes)
        self.assertIsNone(err)
        self.assertEqual(flow, {"nodes": nodes})

    def test_stringified_bare_list_wrapped(self):
        flow, err = Gateway._normalize_e2e_flow_input('[{"id":"a","type":"inject"}]')
        self.assertIsNone(err)
        self.assertEqual(flow, {"nodes": [{"id": "a", "type": "inject"}]})

    def test_dict_with_nodes_passthrough(self):
        data = {"nodes": [{"id": "a"}], "label": "x"}
        flow, err = Gateway._normalize_e2e_flow_input(data)
        self.assertIsNone(err)
        self.assertEqual(flow, data)

    def test_stringified_dict_passthrough(self):
        flow, err = Gateway._normalize_e2e_flow_input('{"nodes":[{"id":"a"}]}')
        self.assertIsNone(err)
        self.assertEqual(flow, {"nodes": [{"id": "a"}]})

    def test_empty_dict_rejected(self):
        flow, err = Gateway._normalize_e2e_flow_input({})
        self.assertIsNotNone(err)
        self.assertIsNone(flow)

    def test_invalid_json_rejected(self):
        flow, err = Gateway._normalize_e2e_flow_input('not json')
        self.assertIsNotNone(err)
        self.assertIsNone(flow)


class TestT1InjectHaServer(unittest.TestCase):
    """现象2：占位符 REPLACE_WITH_HA_SERVER 必须在部署前被替换为真实 server id。"""

    def _fake_gw(self, server_id):
        gw = object.__new__(Gateway)
        gw.cfg = _FakeCfg()
        gw.nr = _FakeNR(server_id)
        return gw

    def test_placeholder_replaced_when_server_known(self):
        gw = self._fake_gw("server_real_123")
        flow = {"nodes": [
            {"id": "s", "type": "server-state-changed", "server": "REPLACE_WITH_HA_SERVER"},
            {"id": "c", "type": "api-call-service", "server": "REPLACE_WITH_HA_SERVER"},
            {"id": "x", "type": "inject", "server": "other"},
        ]}
        used, unresolved = gw._inject_ha_server(flow)
        self.assertEqual(used, "server_real_123")
        self.assertEqual(unresolved, 0)
        for n in flow["nodes"]:
            if n["id"] in ("s", "c"):
                self.assertEqual(n["server"], "server_real_123")
            else:
                self.assertEqual(n["server"], "other")

    def test_no_placeholder_untouched(self):
        gw = self._fake_gw("server_real_123")
        flow = {"nodes": [{"id": "x", "type": "inject", "server": "already_real"}]}
        gw._inject_ha_server(flow)
        self.assertEqual(flow["nodes"][0]["server"], "already_real")

    def test_placeholder_unresolved_when_server_unknown(self):
        """server id 未配置（沙箱/未探测到）→ 返回 ("",1)，调用方据此前置硬拦。"""
        gw = self._fake_gw("")  # 既无配置也无默认
        flow = {"nodes": [
            {"id": "s", "type": "server-state-changed", "server": "REPLACE_WITH_HA_SERVER"},
        ]}
        used, unresolved = gw._inject_ha_server(flow)
        self.assertEqual(used, "")
        self.assertEqual(unresolved, 1)
        self.assertEqual(flow["nodes"][0]["server"], "REPLACE_WITH_HA_SERVER")

    def test_no_ha_node_no_unresolved_when_server_unknown(self):
        """无 HA 节点的 flow 即便 server 未知也不报 unresolved（不会误杀正常 flow）。"""
        gw = self._fake_gw("")
        flow = {"nodes": [{"id": "x", "type": "inject", "server": "other"}]}
        used, unresolved = gw._inject_ha_server(flow)
        self.assertEqual(used, "")
        self.assertEqual(unresolved, 0)

    def test_unresolved_msg_hint_contains_keypoints(self):
        msg = Gateway._ha_server_unresolved_msg(2)
        self.assertIn("REPLACE_WITH_HA_SERVER", msg)
        self.assertIn("nr_ha_server_id", msg)
        self.assertIn("2", msg)


if __name__ == "__main__":
    unittest.main()
