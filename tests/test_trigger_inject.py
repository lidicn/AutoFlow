"""G2 回归：autoflow_trigger_inject（MCP 触发 inject 表面）。

纯 mock：不启网关、不打 NR。验证三种分支：
  · 按 inject_id 精确触发
  · 按 flow_id 批量 fan-out 到所有 inject 节点（跳过非 inject）
  · flow 无 inject → 返回 warning（无触发目标）
  · 两者皆空 → 报错
"""
import os
import sys
import json
import unittest

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import mcp_server as ms


class FakeNR:
    def __init__(self, flow_nodes=None, inject_codes=None):
        self._flow = {"id": "f1", "nodes": flow_nodes or []}
        self._codes = inject_codes or {}
        self.calls = []

    def get_flow(self, fid):
        return self._flow

    def trigger_inject(self, nid):
        self.calls.append(nid)
        return self._codes.get(nid, 200)


class FakeGW:
    def __init__(self, nr):
        self.nr = nr


class TestTriggerInject(unittest.TestCase):
    def setUp(self):
        self._orig_gw = ms._gw
        self._orig_agent = ms.get_current_agent
        ms.get_current_agent = lambda: type("A", (), {"agent_id": "x"})()

    def tearDown(self):
        ms._gw = self._orig_gw
        ms.get_current_agent = self._orig_agent

    def _patch(self, nr):
        ms._gw = lambda: FakeGW(nr)

    def test_trigger_by_inject_id(self):
        nr = FakeNR(inject_codes={"i1": 200})
        self._patch(nr)
        out = json.loads(ms.autoflow_trigger_inject(inject_id="i1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["triggered"], [{"id": "i1", "status": 200}])
        self.assertEqual(nr.calls, ["i1"])

    def test_trigger_by_flow_id_fans_out(self):
        nodes = [{"id": "i1", "type": "inject", "name": "a"},
                 {"id": "x", "type": "debug"},
                 {"id": "i2", "type": "inject", "name": "b"}]
        nr = FakeNR(flow_nodes=nodes, inject_codes={"i1": 200, "i2": 202})
        self._patch(nr)
        out = json.loads(ms.autoflow_trigger_inject(flow_id="f1"))
        self.assertTrue(out["ok"])
        self.assertEqual([t["id"] for t in out["triggered"]], ["i1", "i2"])
        self.assertEqual(out["errors"], [])

    def test_trigger_no_inject_warns(self):
        nr = FakeNR(flow_nodes=[{"id": "x", "type": "debug"}])
        self._patch(nr)
        out = json.loads(ms.autoflow_trigger_inject(flow_id="f1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["triggered"], [])
        self.assertIn("warning", out)

    def test_trigger_needs_arg(self):
        nr = FakeNR()
        self._patch(nr)
        out = json.loads(ms.autoflow_trigger_inject())
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
