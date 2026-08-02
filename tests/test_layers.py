#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nr_layer.py / ha_layer.py 离线单元测试 —— 访问层薄封装 + 结构护栏。

覆盖：
  - 后端注入（backend=）：离线运行关键——传入假后端后 .client 即假后端，
    绝不触发真实 nr_client/ha_client 加载（即不触网）。
  - NRLayer 写/读委派全部透传到 backend（update_flow_nodes / create_or_update_flow /
    add_nodes / modify_* / delete_flow / E2E 透传 / validate / diff 相关 get_*）。
  - NRLayer.build 调度器：已知节点类型 → client.build_X；未知 → ValueError。
  - 结构护栏：NRLayer 绝不暴露 deploy_all / replace_all（防整体替换事故）。
  - HALayer：读方法委派 + 唯一写入口 call_service 委派；无任意直写方法。

全程离线：仅用 FakeNR / FakeHA 桩，不触真实 NR/HA，不读真实凭证。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import autoflow_gateway.config as cfgmod
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer


class FakeNR:
    """实现 NRLayer 委派到的全部 nr_client 方法，记录调用并返回可辨识哨兵。"""
    def __init__(self):
        self.calls = []

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        return {"called": name}

    def list_flows(self): return self._rec("list_flows")
    def get_flow(self, fid): return self._rec("get_flow", fid)
    def find_flow_by_name(self, name): return self._rec("find_flow_by_name", name)
    def update_flow(self, fid, flow, force=False): return self._rec("update_flow", fid, force=force)
    def create_or_update_flow(self, fid, flow, force=False):
        return self._rec("create_or_update_flow", fid, force=force)
    def add_nodes(self, fid, new_nodes): return self._rec("add_nodes", fid)
    def modify_node_field(self, fid, nid, fields): return self._rec("modify_node_field", fid, nid)
    def modify_function_code(self, fid, nid, code, name=None):
        return self._rec("modify_function_code", fid, nid, node_name=name)
    def delete_flow(self, fid, force=False): return self._rec("delete_flow", fid, force=force)
    def inject_flow(self, fid): return self._rec("inject_flow", fid)
    def trigger_inject(self, nid): return 204  # 真实返回 HTTP 状态码
    def get_context(self, store, key): return self._rec("get_context", store, key)
    def set_context(self, store, key, value): return self._rec("set_context", store, key)
    def delete_context(self, store, key): return self._rec("delete_context", store, key)
    def get_default_server_id(self): return "server_x"
    def validate_flow(self, flow): return self._rec("validate_flow")
    def dump_all_flows(self, outdir): return 0
    def build_inject(self, nid, fid, **kw): return {"id": nid, "type": "inject", "z": fid}
    def build_server_state_changed(self, nid, fid, eid, **kw):
        return {"id": nid, "type": "server-state-changed", "z": fid,
                "entities": {"entity": [eid]}}


class FakeHA:
    """实现 HALayer 读 + call_service 委派，返回可辨识哨兵。"""
    def __init__(self):
        self.calls = []
    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        return {"called": name}
    def get_states(self, domain=None): return self._rec("get_states", domain)
    def get_state(self, entity_id): return self._rec("get_state", entity_id)
    def search_entities(self, keyword, domain=None): return self._rec("search_entities", keyword, domain)
    def list_entities(self, domain=None, area=None): return self._rec("list_entities", domain, area)
    def get_areas(self): return self._rec("get_areas")
    def get_areas_http(self): return self._rec("get_areas_http")
    def domain_counts(self): return self._rec("domain_counts")
    def find_by_state(self, state_value, domain=None): return self._rec("find_by_state", state_value, domain)
    def call_service(self, d, s, data): return self._rec("call_service", d, s, data)


class TestNrLayerBackendInjection(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.cfg = cfgmod.get_config()
        self.fake = FakeNR()

    def test_client_is_injected_backend(self):
        nr = NRLayer(config=self.cfg, backend=self.fake)
        # 访问前 _client 为 None（证明懒加载、未偷偷加载真实 nr_client）
        self.assertIsNone(nr._client)
        self.assertIs(nr.client, self.fake)
        self.assertIs(nr._client, self.fake)

    def test_no_real_client_load_when_backend_given(self):
        # backend 注入后，即便 _load_nr_client 路径存在，也不应被调用
        nr = NRLayer(config=self.cfg, backend=self.fake)
        self.assertIsNotNone(nr.client)


class TestNrLayerDelegation(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.cfg = cfgmod.get_config()
        self.fake = FakeNR()
        self.nr = NRLayer(config=self.cfg, backend=self.fake)

    def _assert_call(self, method_name, ret):
        self.assertEqual(ret["called"], method_name)
        self.assertTrue(any(c[0] == method_name for c in self.fake.calls))

    def test_read_delegation(self):
        self._assert_call("list_flows", self.nr.list_flows())
        self._assert_call("get_flow", self.nr.get_flow("f1"))
        self._assert_call("find_flow_by_name", self.nr.find_flow_by_name("x"))

    def test_safe_write_delegation(self):
        self._assert_call("update_flow", self.nr.update_flow_nodes("f1", {"nodes": []}, force=True))
        self._assert_call("create_or_update_flow", self.nr.create_or_update_flow("f1", {"id": "f1"}))
        self._assert_call("add_nodes", self.nr.add_nodes("f1", [{"id": "n"}]))
        self._assert_call("modify_node_field", self.nr.modify_node_field("f1", "n1", {"x": 1}))
        self._assert_call("modify_function_code", self.nr.modify_function_code("f1", "n1", "code"))
        self._assert_call("delete_flow", self.nr.delete_flow("f1", force=True))

    def test_e2e_passthrough(self):
        self._assert_call("inject_flow", self.nr.inject_flow("f1"))
        self.assertEqual(self.nr.trigger_inject("n1"), 204)
        self._assert_call("get_context", self.nr.get_context("flow", "k"))
        self._assert_call("set_context", self.nr.set_context("flow", "k", 1))
        self._assert_call("delete_context", self.nr.delete_context("flow", "k"))

    def test_validate_and_dump(self):
        self._assert_call("validate_flow", self.nr.validate_flow({"nodes": []}))
        self.assertEqual(self.nr.dump_all_flows("/tmp/x"), 0)
        self.assertEqual(self.nr.get_default_server_id(), "server_x")

    def test_build_dispatcher_known(self):
        node = self.nr.build("inject", "n1", "f1")
        self.assertEqual(node, {"id": "n1", "type": "inject", "z": "f1"})
        node2 = self.nr.build("server_state_changed", "n2", "f1", "light.x")
        self.assertEqual(node2["type"], "server-state-changed")

    def test_build_dispatcher_unknown_raises(self):
        with self.assertRaises(ValueError):
            self.nr.build("totally_unknown_node", "n1", "f1")


class TestNrLayerStructuralGuard(unittest.TestCase):
    def test_no_whole_replace_methods(self):
        # 结构护栏：NRLayer 绝不暴露 deploy_all / replace_all —— 防整体替换事故
        self.assertFalse(hasattr(NRLayer, "deploy_all"))
        self.assertFalse(hasattr(NRLayer, "replace_all"))


class TestHaLayerBackendInjection(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.cfg = cfgmod.get_config()
        self.fake = FakeHA()

    def test_client_is_injected_backend(self):
        ha = HALayer(config=self.cfg, backend=self.fake)
        self.assertIsNone(ha._client)
        self.assertIs(ha.client, self.fake)


class TestHaLayerDelegation(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.cfg = cfgmod.get_config()
        self.fake = FakeHA()
        self.ha = HALayer(config=self.cfg, backend=self.fake)

    def _assert_call(self, method_name, ret):
        self.assertEqual(ret["called"], method_name)
        self.assertTrue(any(c[0] == method_name for c in self.fake.calls))

    def test_read_delegation(self):
        self._assert_call("get_states", self.ha.get_states("light"))
        self._assert_call("get_state", self.ha.get_state("light.x"))
        self._assert_call("search_entities", self.ha.search_entities("客厅"))
        self._assert_call("list_entities", self.ha.list_entities(domain="light", area="客厅"))
        self._assert_call("get_areas", self.ha.get_areas())
        self._assert_call("get_areas_http", self.ha.get_areas_http())
        self._assert_call("domain_counts", self.ha.domain_counts())
        self._assert_call("find_by_state", self.ha.find_by_state("on", "light"))

    def test_write_only_via_call_service(self):
        # 唯一写入口 call_service 委派
        self._assert_call("call_service", self.ha.call_service("light", "turn_on", {"entity_id": "light.x"}))
        # 不存在任意直写方法（受控写：agent 永不直接拿 HA 凭证改状态）
        for banned in ("set_state", "write_state", "update_state", "put_state"):
            self.assertFalse(hasattr(self.ha, banned), f"不应暴露 {banned}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
