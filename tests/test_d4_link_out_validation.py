# -*- coding: utf-8 -*-
"""D4 (G2) · 部署期 link-out 目标校验单元测试（T-D4）。

直接验证 Gateway._validate_link_out_targets：
  - 指向真实存在的 link-in/子流程入口（如 demo_notify=b595563939283231）→ 通过
  - 指向同流内 link-in 节点 → 通过
  - 指向不存在的 id → 返回 R_LINKIN 错误（部署期即拦，避免运行时
    'Error delivering message to node:undefined'）
  - 无 link-out 节点 → 空（不影响普通流）
  - 无 NR client → fail-open 返回空（不阻塞）

该方法是 Gateway 实例方法，但只依赖 self.nr.client._json；
用轻量 stub 绑定方法即可单测，无需完整 Gateway/真实 NR。
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from autoflow_gateway.gateway import Gateway  # noqa: E402


class _StubClient:
    def __init__(self, ids):
        # 模拟 GET /flows：每个 id 一个节点；外加一个带 in 端口的子流程 def
        self._flows = [{"id": i, "type": "link in"} for i in ids]
        self._flows.append({
            "id": "sub_abc", "type": "subflow", "name": "某子流程",
            "in": [{"id": "sub_abc_in", "wires": []}], "out": [],
        })

    def _json(self, method, path):
        assert method == "GET" and path == "/flows"
        return self._flows


class _FakeGW:
    pass


# 把实例方法绑定到轻量 stub（方法仅用 self.nr.client._json）
_FakeGW._validate_link_out_targets = Gateway._validate_link_out_targets


def _make_gw(ids):
    gw = _FakeGW()
    gw.nr = types.SimpleNamespace(client=_StubClient(ids))
    return gw


def _flow(link_nodes):
    return {"id": "f1", "label": "t", "nodes": link_nodes}


class TestLinkOutValidation(unittest.TestCase):

    def test_valid_managed_tts_link_passes(self):
        gw = _make_gw(["b595563939283231", "af_anysearch_in"])
        flow = _flow([{
            "id": "lo1", "type": "link out", "name": "→ demo_notify",
            "links": ["b595563939283231"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [], f"合法 tts link 不应报错: {errs}")

    def test_valid_anysearch_link_passes(self):
        gw = _make_gw(["b595563939283231", "af_anysearch_in"])
        flow = _flow([{
            "id": "lo2", "type": "link out", "name": "→ anysearch",
            "links": ["af_anysearch_in"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [], f"合法 anysearch link 不应报错: {errs}")

    def test_subflow_in_port_id_accepted(self):
        gw = _make_gw(["b595563939283231"])
        flow = _flow([{
            "id": "lo3", "type": "link out", "name": "→ 子流程入口",
            "links": ["sub_abc_in"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [], f"子流程 in 端口 id 应被接受: {errs}")

    def test_same_flow_link_in_accepted(self):
        gw = _make_gw([])
        flow = _flow([
            {"id": "li1", "type": "link in", "name": "本流入口"},
            {"id": "lo4", "type": "link out", "name": "→ 本流", "links": ["li1"]},
        ])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [], f"同流内 link-in 应被接受: {errs}")

    def test_dangling_link_out_rejected(self):
        gw = _make_gw(["b595563939283231"])
        flow = _flow([{
            "id": "lo5", "type": "link out", "name": "→ 不存在",
            "links": ["zzz_nonexistent_id"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(len(errs), 1, f"应恰好 1 个错误: {errs}")
        self.assertEqual(errs[0]["rule"], "R_LINKIN")
        self.assertEqual(errs[0]["level"], "error")
        self.assertIn("zzz_nonexistent_id", errs[0]["message"])

    def test_no_link_out_is_noop(self):
        gw = _make_gw([])
        flow = _flow([{"id": "n1", "type": "change", "rules": []}])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [])

    def test_multiple_links_one_dangling(self):
        gw = _make_gw(["b595563939283231"])
        flow = _flow([{
            "id": "lo6", "type": "link out", "name": "混合",
            "links": ["b595563939283231", "ghost_id"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(len(errs), 1, f"仅应报 ghost_id: {errs}")
        self.assertIn("ghost_id", errs[0]["message"])

    def test_fail_open_when_no_client(self):
        gw = _FakeGW()
        gw.nr = None  # 无 NR client → fail-open
        flow = _flow([{
            "id": "lo7", "type": "link out", "links": ["whatever"],
        }])
        errs = gw._validate_link_out_targets(flow)
        self.assertEqual(errs, [], "无 client 应 fail-open 返回空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
