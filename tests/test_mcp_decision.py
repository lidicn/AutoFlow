#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1/B2/B3 回归：MCP 决策回路「取回」半环闭环测试。

背景：autoflow_request_decision 能发起决策，但纯 MCP 直连 agent 此前没有工具
取回人类选择——回路在 MCP 通道断开。B1/B2 新增 autoflow_get_decision /
autoflow_list_decisions 两把取回刀；B3 补全轮询协议文档。

本测试覆盖：
  1. request → get 未决：刚发起时 status=="pending"、chosen_text 为 None；
  2. request → resolve（WebUI 点选回灌）→ get 已决：status=="resolved"、chosen_text 正确；
  3. list 按 status 过滤：pending 不含已决项，resolved 只含已决项，缺省返回全部；
  4. get 未知 id → ok=False 且提示「不存在」；
  5. DecisionStore 持久层独立单测：create/resolve/get/list/幂等拦截。

全程离线：monkeypatch 掉 Bark 催办与 ds_bridge 回灌，数据落临时目录，不触真实 HA/NR。
"""
import os
import sys
import json
import types
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway import config as cfgmod          # noqa: E402
from autoflow_gateway.gateway import Gateway           # noqa: E402
from autoflow_gateway.ha_layer import HALayer          # noqa: E402
from autoflow_gateway.nr_layer import NRLayer          # noqa: E402
from autoflow_gateway.decision_store import DecisionStore  # noqa: E402
import autoflow_gateway.mcp_server as ms               # noqa: E402


# ── 假后端（与 test_gateway.py 同款，避免真实网络）──
class _FakeNR:
    def update_flow(self, *a, **k):
        return {"ok": True}
    def create_or_update_flow(self, *a, **k):
        return {"id": a[0] if a else "x", "created": False, "raw": {"ok": True}}
    def list_flows(self):
        return []
    def get_flow(self, fid):
        return {"id": fid, "type": "tab", "nodes": []}
    def validate_flow(self, *a, **k):
        return []
    def delete_flow(self, *a, **k):
        return {"ok": True}
    def dump_all_flows(self, *a, **k):
        return 0
    def build_server_state_changed(self, nid, fid, eid, **kw):
        return {"id": nid, "type": "server-state-changed", "z": fid, "entities": {"entity": [eid]}}
    def build_inject(self, nid, fid, **kw):
        return {"id": nid, "type": "inject", "z": fid}
    def _get_default_server(self):
        return "server_x"


class _FakeHA:
    def __init__(self):
        self.states = []
        self.areas = {}
    def get_states(self, domain=None):
        return self.states
    def get_areas(self):
        return self.areas
    def entity_area_map(self):
        return {}
    def entity_device_ids(self):
        return {}
    def invalidate_registries(self):
        pass
    def get_state(self, entity_id):
        raise RuntimeError("not found")
    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}", "data": data}


def _make_gateway():
    tmp = tempfile.mkdtemp(prefix="af_mcp_dec_")
    os.environ["AUTOFLLOW_DATA_DIR"] = tmp
    cfgmod.reset_config()
    cfg = cfgmod.get_config()
    return Gateway(
        config=cfg,
        ha_layer=HALayer(config=cfg, backend=_FakeHA()),
        nr_layer=NRLayer(config=cfg, backend=_FakeNR()),
    )


class TestMCPDecisionLoop(unittest.TestCase):
    def setUp(self):
        self.gw = _make_gateway()
        # 离线桩：屏蔽 Bark 催办与 ds_bridge 回灌（均触网络）
        self.gw._bark_push = lambda *a, **k: None
        self.gw._wait_ds_bridge_idle = lambda *a, **k: {"ok": True}
        self.gw._fire_ds_bridge = lambda *a, **k: {"ok": True}
        # 把 MCP 工具指向本测试的安全网关实例
        self._orig_gw = ms._gw
        ms._gw = lambda: self.gw

    def tearDown(self):
        ms._gw = self._orig_gw

    # ── 1. request → get（未决）──
    def test_request_then_get_pending(self):
        out = json.loads(ms.autoflow_request_decision("今晚开空调吗？", ["开", "不开"]))
        self.assertTrue(out["ok"], f"request 应成功: {out}")
        did = out["decision_id"]
        self.assertTrue(did.startswith("dec_"), "应返回 dec_ 前缀的 decision_id")

        got = json.loads(ms.autoflow_get_decision(did))
        self.assertTrue(got["ok"])
        self.assertEqual(got["status"], "pending", "刚发起应为 pending")
        self.assertIsNone(got["chosen_text"], "未决时 chosen_text 应为 None")
        self.assertEqual(got["options"], ["开", "不开"])

    # ── 2. request → resolve（WebUI 点选）→ get（已决，闭环）──
    def test_closed_loop_resolve_then_get(self):
        out = json.loads(ms.autoflow_request_decision("选哪个？", ["A", "B", "C"]))
        did = out["decision_id"]

        # WebUI 工作区点选第 2 项（idx=1）→ 走完整 resolve_decision 路径
        res = self.gw.resolve_decision(did, 1)
        self.assertIn("ok", res, "resolve_decision 应返回字典")

        got = json.loads(ms.autoflow_get_decision(did))
        self.assertTrue(got["ok"])
        self.assertEqual(got["status"], "resolved", "已选应为 resolved")
        self.assertEqual(got["chosen_idx"], 1)
        self.assertEqual(got["chosen_text"], "B", "应取回人类选择的文本 B")

    # ── 3. list 按 status 过滤 ──
    def test_list_filters_by_status(self):
        a = json.loads(ms.autoflow_request_decision("问题甲", ["x", "y"]))["decision_id"]
        b = json.loads(ms.autoflow_request_decision("问题乙", ["u", "v"]))["decision_id"]
        # 仅解决 a
        self.gw.decisions.resolve(a, 0)

        pending = json.loads(ms.autoflow_list_decisions(status="pending"))
        pending_ids = {d["id"] for d in pending["decisions"]}
        self.assertNotIn(a, pending_ids, "pending 列表不应含已决项 a")
        self.assertIn(b, pending_ids, "pending 列表应含未决项 b")

        resolved = json.loads(ms.autoflow_list_decisions(status="resolved"))
        resolved_ids = {d["id"] for d in resolved["decisions"]}
        self.assertIn(a, resolved_ids, "resolved 列表应含已决项 a")
        self.assertNotIn(b, resolved_ids, "resolved 列表不应含未决项 b")

        all_d = json.loads(ms.autoflow_list_decisions())
        all_ids = {d["id"] for d in all_d["decisions"]}
        self.assertTrue({a, b}.issubset(all_ids), "缺省 list 应包含全部决策")

    # ── 4. get 未知 id ──
    def test_get_unknown_id_returns_error(self):
        got = json.loads(ms.autoflow_get_decision("dec_does_not_exist"))
        self.assertFalse(got["ok"])
        self.assertIn("不存在", got["error"], "未知 id 应提示不存在")


class TestDecisionStoreUnit(unittest.TestCase):
    """持久层独立单测，不依赖 Gateway / MCP 工具。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_ds_")
        self.store = DecisionStore(config=types.SimpleNamespace(data_dir=self.tmp))

    def test_create_get_pending(self):
        rec = self.store.create("去哪吃饭？", ["火锅", "日料"])
        did = rec["id"]
        self.assertEqual(rec["status"], "pending")
        got = self.store.get(did)
        self.assertIsNotNone(got)
        self.assertEqual(got["options"], ["火锅", "日料"])
        self.assertIsNone(got["chosen_text"])

    def test_resolve_sets_chosen_text(self):
        did = self.store.create("选?", ["甲", "乙"])["id"]
        rec = self.store.resolve(did, 1)
        self.assertEqual(rec["status"], "resolved")
        self.assertEqual(rec["chosen_idx"], 1)
        self.assertEqual(rec["chosen_text"], "乙")
        # 回读一致
        self.assertEqual(self.store.get(did)["chosen_text"], "乙")

    def test_resolve_idempotent_guarded(self):
        did = self.store.create("选?", ["a"])["id"]
        self.store.resolve(did, 0)
        with self.assertRaises(ValueError):
            self.store.resolve(did, 0)  # 已 resolved 不能重复

    def test_resolve_out_of_range(self):
        did = self.store.create("选?", ["a", "b"])["id"]
        with self.assertRaises(ValueError):
            self.store.resolve(did, 5)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.store.get("dec_nope"))

    def test_list_limit_and_status(self):
        for i in range(3):
            self.store.create(f"q{i}", [f"o{i}"])
        all_rows = self.store.list()
        self.assertEqual(len(all_rows), 3)
        limited = self.store.list(limit=2)
        self.assertEqual(len(limited), 2)
        # 全部 pending，按 status 过滤仍应返回
        self.assertEqual(len(self.store.list(status="pending")), 3)


if __name__ == "__main__":
    unittest.main()
