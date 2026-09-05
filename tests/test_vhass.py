#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vhass 虚拟 HA 单元测试（启动真实 HTTP 服务，纯本地，无真实设备）。"""
import os
import sys
import json
import threading
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

# 确保可导入包（editable 安装或 src 路径）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.vhass import VHassStore, Handler, build_seed_from_catalog, build_seed_from_entities


def _start_server(seed_path=None):
    store = VHassStore(seed_path=seed_path)
    Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, store


def _get(port, path):
    with urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return json.loads(r.read().decode("utf-8"))


def _post(port, path, payload):
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


class TestVHassHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port, cls.store = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_health_and_config(self):
        h = _get(self.port, "/health")
        self.assertEqual(h["name"], "vhass")
        cfg = _get(self.port, "/api/config")
        self.assertEqual(cfg["location_name"], "AutoFlow Staging")

    def test_states_list_and_one(self):
        states = _get(self.port, "/api/states")
        self.assertIsInstance(states, list)
        self.assertGreater(len(states), 0)
        eid = states[0]["entity_id"]
        one = _get(self.port, f"/api/states/{eid}")
        self.assertEqual(one["entity_id"], eid)

    def test_service_turn_on_mutates(self):
        # 找一个 light 实体
        states = _get(self.port, "/api/states")
        light = next((s for s in states if s["entity_id"].startswith("light.")), None)
        self.assertIsNotNone(light)
        eid = light["entity_id"]
        _post(self.port, "/api/services/light/turn_on", {"entity_id": eid})
        after = _get(self.port, f"/api/states/{eid}")
        self.assertEqual(after["state"], "on")

    def test_trigger_injection(self):
        res = _post(self.port, "/api/trigger",
                    {"entity_id": "device_tracker.me", "state": "home"})
        self.assertEqual(res["state"], "home")

    def test_areas(self):
        areas = _get(self.port, "/api/areas")
        self.assertIsInstance(areas, dict)


class TestVHassStoreLogic(unittest.TestCase):
    def test_build_seed_and_service(self):
        seed = build_seed_from_entities([
            ("light.x", "灯", "客厅", "off", {}),
            ("switch.y", "开关", "卧室", "off", {}),
        ])
        store = VHassStore()
        store.areas = seed["areas"]
        store.entities = {e["entity_id"]: VHassStore._normalize(e) for e in seed["entities"]}
        changed = store.apply_service("light", "turn_on", {"entity_id": "light.x"})
        self.assertEqual(changed[0]["state"], "on")
        # 未知 service 用 service 名标注
        store.apply_service("switch", "weird", {"entity_id": "switch.y"})
        self.assertEqual(store.entities["switch.y"]["state"], "weird")

    def test_seed_from_catalog(self):
        cat = {"entities": {
            "light.a": {"entity_id": "light.a", "friendly_name": "客厅灯", "area": "客厅", "state": "off",
                        "capabilities": ["on_off", "brightness"]},
            "cover.b": {"entity_id": "cover.b", "friendly_name": "窗帘", "area": "卧室", "state": "closed"},
        }}
        tmp = tempfile.mktemp(suffix=".json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cat, f)
        out = tempfile.mktemp(suffix=".json")
        seed = build_seed_from_catalog(tmp, out)
        self.assertEqual(len(seed["entities"]), 2)
        self.assertIn("客厅", seed["areas"].values())
        # 能力 → attributes 推导
        light = next(e for e in seed["entities"] if e["entity_id"] == "light.a")
        self.assertIn("supported_features", light["attributes"])


if __name__ == "__main__":
    unittest.main()
