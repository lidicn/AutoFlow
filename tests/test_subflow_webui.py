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
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
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
        os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
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

    # ── #711：启停 / 删除策略 ──
    # 旧策略：managed 一律禁止启停与删除（403）→ 历史子流程在 WebUI 上完全不可操作。
    # 新策略：
    #   启停：全部允许（「禁用」是历史子流程唯一的治理手段）
    #   删除：history_* 禁止（DSL 内置原语）；managed 自建 → 连 NR 实例一起删；
    #         imported → 只取消登记，NR 上的子流程保留（网关无权删用户的东西）
    def test_set_status_allowed_for_managed_and_history(self):
        lst = self.client.get("/api/subflows").json()["subflows"]
        linkout = next(s for s in lst if s["kind"] == "link_out")
        # 历史子流程（managed subflow 实例型）现在允许禁用/启用
        for key in ("history_duration", "bark_push"):
            r = self.client.patch(f"/api/subflows/{key}/status",
                                  json={"status": "disabled"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "disabled")
            r = self.client.patch(f"/api/subflows/{key}/status",
                                  json={"status": "active"})
            self.assertEqual(r.status_code, 200, r.text)
        # link_out 型能力仍可启停
        r2 = self.client.patch(
            f"/api/subflows/{linkout['key']}/status", json={"status": "disabled"})
        self.assertEqual(r2.status_code, 200, r2.text)
        r3 = self.client.patch(
            f"/api/subflows/{linkout['key']}/status", json={"status": "active"})
        self.assertEqual(r3.status_code, 200, r3.text)
        # 不存在的 key → 404；非法状态 → 400
        self.assertEqual(self.client.patch("/api/subflows/nope/status",
                                           json={"status": "active"}).status_code, 404)
        self.assertEqual(self.client.patch("/api/subflows/bark_push/status",
                                           json={"status": "zzz"}).status_code, 400)

    def test_delete_history_forbidden(self):
        for key in ("history_state_at", "history_occurred",
                    "history_duration", "history_aggregate"):
            r = self.client.delete(f"/api/subflows/{key}")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("不可删除", r.json()["error"])
        # 仍在注册表里
        keys = {s["key"] for s in self.client.get("/api/subflows").json()["subflows"]}
        self.assertIn("history_duration", keys)

    def test_delete_imported_keeps_nr_instance(self):
        """用户导入的子流程：只取消登记，绝不删 NR 上的实例。"""
        calls = []
        self.gw.nr.delete_flow = lambda fid, force=False: (
            calls.append(fid) or {"deleted": True})
        self.client.post("/api/subflows/import", json={
            "nr_subflow_id": "sf_dummy_99", "key": "my_dummy", "owner": "tester"})
        r = self.client.delete("/api/subflows/my_dummy")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["nr_removed"])
        self.assertTrue(body["nr_kept"])
        self.assertEqual(calls, [])            # ★ 没有碰用户的 NR 子流程
        keys = {s["key"] for s in self.client.get("/api/subflows").json()["subflows"]}
        self.assertNotIn("my_dummy", keys)     # 注册表条目已移除

    def test_delete_managed_removes_nr_instance(self):
        """网关自建的子流程（bark_push）：谁建的谁收尾，NR 实例一并删除。"""
        calls = []
        self.gw.nr.delete_flow = lambda fid, force=False: (
            calls.append(fid) or {"deleted": True})
        meta = self.gw.tasks.get_subflow_meta("bark_push")
        r = self.client.delete("/api/subflows/bark_push")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["nr_removed"])
        self.assertFalse(r.json()["nr_kept"])
        self.assertEqual(calls, [meta["nr_subflow_id"]])

    def test_ensure_endpoint(self):
        """「安装到 NR」按钮：仅 history_*；其余 400；不存在 404。"""
        seen = {}
        import autoflow_gateway.subflows as sf_mod
        orig = sf_mod.ensure_history_subflow
        sf_mod.ensure_history_subflow = lambda nr, allow_prod=False: (
            seen.update(allow_prod=allow_prod) or {"exists": True, "created": False})
        try:
            # NRLayer.client 是只读 property（懒建 NRClient，不发网络请求），
            # ensure 已被 stub 掉，故不需要真实连通的 NR。
            self.assertIsNotNone(getattr(self.gw.nr, "client", None))
            r = self.client.post("/api/subflows/history_duration/ensure", json={})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["exists"])
            self.assertIs(seen["allow_prod"], True)   # 人手动触发 → 放行 prod
            # 非历史子流程 → 400
            self.assertEqual(
                self.client.post("/api/subflows/bark_push/ensure", json={}).status_code, 400)
            # 不存在 → 404
            self.assertEqual(
                self.client.post("/api/subflows/nope/ensure", json={}).status_code, 404)
        finally:
            sf_mod.ensure_history_subflow = orig

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
