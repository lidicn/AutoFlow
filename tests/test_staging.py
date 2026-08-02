#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""staging 集成测试：启动 vhass，让 gateway(staging) 指向它，验证全链路不触真实 HA。"""
import os
import sys
import json
import threading
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.vhass import VHassStore, Handler
from autoflow_gateway.config import GatewayConfig, reset_config
from autoflow_gateway.gateway import Gateway


def _start_vhass():
    store = VHassStore()  # demo 数据（含客厅/卧室实体）
    Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


class TestStagingTwin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vhttpd, cls.vport = _start_vhass()
        cls.tmp = tempfile.mkdtemp(prefix="af_staging_")
        # 构造 staging 网关，HASS_SERVER 指向 vhass
        reset_config()
        cfg = GatewayConfig()
        cfg.env = "staging"
        cfg.data_dir = cls.tmp
        cfg.hass_server = f"http://127.0.0.1:{cls.vport}"
        cfg.hass_token = "staging-token"
        cfg.nr_url = "http://127.0.0.1:1"  # 不测 NR，占位
        cfg.make_dirs()
        cls.gw = Gateway(config=cfg)
        # 先刷新 catalog（所有测试共享），验证 staging 全链路
        cls.refresh = cls.gw.refresh_catalog(full=True)

    @classmethod
    def tearDownClass(cls):
        cls.vhttpd.shutdown()
        reset_config()

    def test_refresh_from_vhass(self):
        res = self.gw.refresh_catalog(full=True)
        self.assertTrue(res["ok"])
        self.assertGreater(res["entity_total"], 0)
        # vhass 支持 /api/areas，area 应可用
        self.assertTrue(res["area_available"])

    def test_discover_works(self):
        disc = self.gw.discover(keyword="客厅")
        self.assertGreater(len(disc["entities"]), 0)
        self.assertTrue(any("客厅" in (e.get("area") or "") for e in disc["entities"]))

    def test_get_detail_reads_vhass(self):
        # 取一个已知实体
        states = json.loads(urlopen(f"http://127.0.0.1:{self.vport}/api/states").read())
        eid = states[0]["entity_id"]
        det = self.gw.get_detail(eid)
        self.assertTrue(det["ok"])
        self.assertEqual(det["detail"]["state"], states[0]["state"])

    def test_call_service_via_gateway(self):
        # 通过网关 ha 层调用 vhass 服务（staging 写路径，不触真实设备）
        light = None
        states = json.loads(urlopen(f"http://127.0.0.1:{self.vport}/api/states").read())
        for s in states:
            if s["entity_id"].startswith("light."):
                light = s["entity_id"]
                break
        self.assertIsNotNone(light)
        r = self.gw.ha.call_service("light", "turn_on", {"entity_id": light})
        self.assertTrue(r)  # HAClient 返回非空
        # 验证 vhass 状态已变更
        after = json.loads(urlopen(f"http://127.0.0.1:{self.vport}/api/states/{light}").read())
        self.assertEqual(after["state"], "on")


if __name__ == "__main__":
    unittest.main()
