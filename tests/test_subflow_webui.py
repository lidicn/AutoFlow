#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebUI 子流程注册表端点单测（#579，离线 starlette TestClient）。

验证：
  - GET  /api/subflows        → 返回网关 seed 的 9 条 managed（5 subflow：bark_push + 4 history；4 link_out）
  - POST /api/subflows/import → 自省（离线 stub）+ 注册，列表新增 imported 一条
不触真实 NR/HA；introspect_nr_subflow 以离线 stub 替换。
"""
import os
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway import webui as webui_mod
from autoflow_gateway.subflows import SUBFLOWS

LINKOUT_KEYS = {k for k, s in SUBFLOWS.items() if (s.call or {}).get("type") == "link_out"}
SUBFLOW_KEYS = {
    "bark_push", "history_state_at", "history_occurred",
    "history_duration", "history_aggregate",
}

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError as _e:  # pragma: no cover
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


def _fake_introspect(nr, nr_subflow_id):
    """离线 stub：模拟从 NR 自省出一个子流程的『前置参数』。"""
    return {
        "ok": True,
        "nr_subflow_id": nr_subflow_id,
        "title": "我的导入子流程",
        "in_ports": 1, "out_ports": 1, "internal_node_count": 2,
        "env_requirements": [{"name": "MY_TOKEN", "type": "str"}],
        "input_schema": [
            {"name": "device", "required": True, "type": "str",
             "default": None, "enum": None, "desc": "设备"},
            {"name": "room", "required": False, "type": "str",
             "default": "default", "enum": None, "desc": "房间"},
        ],
    }


@unittest.skipUnless(_HAVE_WEB_DEPS, "需要 starlette（缺失则用系统 Python 3.13.2 或 pip install starlette）")
class TestSubflowWebUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_sfweb_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self._orig = webui_mod.introspect_nr_subflow
        webui_mod.introspect_nr_subflow = _fake_introspect

    def tearDown(self):
        webui_mod.introspect_nr_subflow = self._orig
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_returns_seeded_managed(self):
        r = self.client.get("/api/subflows")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 9)    # 5 subflow + 4 link_out
        keys = {s["key"] for s in body["subflows"]}
        self.assertEqual(keys, SUBFLOW_KEYS | LINKOUT_KEYS)
        for s in body["subflows"]:
            self.assertEqual(s["source_type"], "managed")
            self.assertEqual(s["status"], "active")
            if s["key"] in LINKOUT_KEYS:
                self.assertEqual(s["kind"], "link_out")
                self.assertTrue(s["entry_link_id"])
                self.assertIsNone(s["nr_subflow_id"])
            else:
                self.assertEqual(s["kind"], "subflow")
                self.assertTrue(s["nr_subflow_id"])

    def test_import_adds_entry(self):
        r = self.client.post("/api/subflows/import", json={
            "nr_subflow_id": "sf_dummy_99",
            "key": "my_dummy",
            "title": "导入的dummy",
            "owner": "tester",
        })
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["key"], "my_dummy")
        # 自省结果回传
        self.assertEqual(body["introspect"]["nr_subflow_id"], "sf_dummy_99")
        # 列表新增 imported 一条（共 10）
        lst = self.client.get("/api/subflows").json()
        self.assertEqual(lst["count"], 10)
        imported = [s for s in lst["subflows"] if s["key"] == "my_dummy"]
        self.assertEqual(len(imported), 1)
        m = imported[0]
        self.assertEqual(m["source_type"], "imported")
        self.assertEqual(m["status"], "active")
        self.assertEqual(m["nr_subflow_id"], "sf_dummy_99")
        self.assertEqual(m["env_requirements"], ["MY_TOKEN"])
        names = {p["name"] for p in m["input_schema"]}
        self.assertEqual(names, {"device", "room"})

    def test_set_status_managed_linkout_allowed_subflow_forbidden(self):
        # seed 已在 Gateway.__init__ 完成（9 条 managed）
        lst = self.client.get("/api/subflows").json()["subflows"]
        linkout = next(s for s in lst if s["kind"] == "link_out")
        subflow = next(s for s in lst
                       if s["kind"] == "subflow" and s["source_type"] == "managed")
        # managed subflow 实例型：系统管理，拒绝手动变更 → 403
        r1 = self.client.patch(
            f"/api/subflows/{subflow['key']}/status", json={"status": "disabled"})
        self.assertEqual(r1.status_code, 403)
        # managed link_out 型能力：允许手动启停（可管理）→ 200
        r2 = self.client.patch(
            f"/api/subflows/{linkout['key']}/status", json={"status": "disabled"})
        self.assertEqual(r2.status_code, 200, r2.text)
        # 重新启用 → 200
        r3 = self.client.patch(
            f"/api/subflows/{linkout['key']}/status", json={"status": "active"})
        self.assertEqual(r3.status_code, 200, r3.text)

    def test_import_rejects_missing_fields(self):
        # 缺 nr_subflow_id
        self.assertEqual(
            self.client.post("/api/subflows/import", json={"key": "x"}).status_code, 400)
        # 缺 key
        self.assertEqual(
            self.client.post("/api/subflows/import",
                             json={"nr_subflow_id": "sf1"}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
