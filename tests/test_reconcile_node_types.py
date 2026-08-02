"""安装即对账回归测试（WB5 方案 a/b + node_gate 收口）。

验证网关需求报告「单一真值源 / 安装即对账」的落地：
- R28：节点类型已在 NR 注册但网关无字段级规则 → warning 点名（fail-open 变可见）
- R29：flow 引用的子流程 hash 已漂移（升级） → warning 提示重部署刷新
- node_gate 仍对未注册类型硬拦，R28 不双报
"""
import json
import types
import unittest

from autoflow_gateway.gateway import Gateway


class FakeNRClient:
    """最小 NR 客户端桩：提供 get_installed_node_types + _request(/flows)。"""

    def __init__(self, types=None, subflows=None):
        self._types = set(types or set())
        self._subflows = subflows or []  # list of {"id":..., "name":...}

    def get_installed_node_types(self):
        return set(self._types)

    def _request(self, method, endpoint):
        if endpoint == "/flows":
            payload = [
                {"type": "subflow", "id": s["id"], "name": s["name"]}
                for s in self._subflows
            ]
        else:
            payload = []
        return types.SimpleNamespace(text=json.dumps(payload))


def _gw(client):
    gw = Gateway()
    gw.nr = types.SimpleNamespace(client=client)
    return gw


class TestR28UnknownTypeWarning(unittest.TestCase):
    def test_registered_custom_node_warns(self):
        gw = _gw(FakeNRClient(types={"inject", "my-custom-node"}))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "my-custom-node", "z": "t", "wires": []}]}
        issues = gw.validate_flow_schema(flow)
        r28 = [i for i in issues if i.get("rule") == "R28"]
        self.assertEqual(len(r28), 1)
        self.assertEqual(r28[0]["level"], "warning")
        self.assertIn("my-custom-node", r28[0]["message"])

    def test_known_node_no_warn(self):
        gw = _gw(FakeNRClient(types={"function", "inject"}))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "function", "z": "t", "wires": []}]}
        issues = gw.validate_flow_schema(flow)
        self.assertEqual([i for i in issues if i.get("rule") == "R28"], [])

    def test_unregistered_left_to_gate(self):
        # 未注册类型不在此报 R28（交给 node_gate 硬拦），避免重复噪音
        gw = _gw(FakeNRClient(types={"inject"}))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "ghost-node", "z": "t", "wires": []}]}
        issues = gw.validate_flow_schema(flow)
        self.assertEqual([i for i in issues if i.get("rule") == "R28"], [])


class TestR29SubflowDrift(unittest.TestCase):
    def test_drift_warns(self):
        gw = _gw(FakeNRClient(types=set(),
                              subflows=[{"id": "newhash", "name": "bark_push"}]))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "subflow:oldhash", "name": "bark_push",
             "z": "t", "wires": [[]]}]}
        issues = gw.validate_flow_schema(flow)
        r29 = [i for i in issues if i.get("rule") == "R29"]
        self.assertEqual(len(r29), 1)
        self.assertIn("升级", r29[0]["message"])
        self.assertIn("oldhash", r29[0]["message"])

    def test_current_hash_no_warn(self):
        gw = _gw(FakeNRClient(types=set(),
                              subflows=[{"id": "newhash", "name": "bark_push"}]))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "subflow:newhash", "name": "bark_push",
             "z": "t", "wires": [[]]}]}
        issues = gw.validate_flow_schema(flow)
        self.assertEqual([i for i in issues if i.get("rule") == "R29"], [])

    def test_missing_no_double_report(self):
        # 子流程真缺失（name 也匹配不到）→ node_gate 硬拦，R29 不报
        gw = _gw(FakeNRClient(types=set(),
                              subflows=[{"id": "x", "name": "other"}]))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "subflow:oldhash", "name": "bark_push",
             "z": "t", "wires": [[]]}]}
        issues = gw.validate_flow_schema(flow)
        self.assertEqual([i for i in issues if i.get("rule") == "R29"], [])


class TestNodeGateStillBlocks(unittest.TestCase):
    def test_unregistered_type_raises(self):
        # 确认 node_gate 仍对未注册类型硬拦（反驳「deploy_raw 绕过 node_gate」误述）
        gw = _gw(FakeNRClient(types={"inject"}))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "ghost-node", "z": "t", "wires": []}]}
        with self.assertRaises(RuntimeError):
            gw._gate_node_types(flow)

    def test_registered_type_passes(self):
        gw = _gw(FakeNRClient(types={"inject", "function"}))
        flow = {"id": "f1", "nodes": [
            {"id": "n1", "type": "function", "z": "t", "wires": []}]}
        # 不抛即通过（node_gate 仅拦未知类型）
        gw._gate_node_types(flow)


if __name__ == "__main__":
    unittest.main()
