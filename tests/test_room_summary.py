#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""room_summary 两级投递 + export_room_markdown 单元测试（mock 后端，不触真实 HA）。"""
import os
import sys
import json
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import autoflow_gateway.config as cfgmod
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.nr_layer import NRLayer


class FakeNR:
    def update_flow(self, fid, flow, force=False):
        return {"ok": True}
    def create_or_update_flow(self, fid, flow, force=False):
        return {"id": fid, "created": False, "raw": {"ok": True}}
    def list_flows(self):
        return []
    def get_flow(self, fid):
        return {"id": fid, "type": "tab", "nodes": []}
    def validate_flow(self, flow):
        return []
    def delete_flow(self, fid):
        return {"ok": True}
    def dump_all_flows(self, d):
        return 0
    def build_server_state_changed(self, nid, fid, eid, **kw):
        return {"id": nid, "type": "server-state-changed", "z": fid, "entities": {"entity": [eid]}}
    def build_inject(self, nid, fid, **kw):
        return {"id": nid, "type": "inject", "z": fid}
    def _get_default_server(self):
        return "server_x"


class FakeHA:
    def get_states(self, domain=None):
        return []
    def get_areas(self):
        return {"zws": "主卧室", "cf": "厨房"}
    def entity_areas(self):
        return {}
    def entity_device_ids(self):
        return {}
    def invalidate_registries(self):
        pass
    def get_state(self, entity_id):
        raise RuntimeError("not found")
    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}"}


def make_gateway():
    tmp = tempfile.mkdtemp(prefix="af_rs_test_")
    os.environ["AUTOFLLOW_DATA_DIR"] = tmp
    cfgmod.reset_config()
    cfg = cfgmod.get_config()
    return Gateway(config=cfg,
                   ha_layer=HALayer(config=cfg, backend=FakeHA()),
                   nr_layer=NRLayer(config=cfg, backend=FakeNR()))


def build_master_bedroom_catalog(gw):
    """构造一个含设备归组 + 区域的主卧室 catalog（不触真实 HA）。"""
    ents = {}
    # 设备 D1：主卧室空调（climate+switch+sensor）
    ents["climate.zws_ac"] = {"friendly_name": "主卧室空调 空调", "state": "off",
                              "domain": "climate", "area": "zws", "device_id": "D1"}
    ents["switch.zws_ac_vs"] = {"friendly_name": "主卧室空调 上下摆风", "state": "on",
                                "domain": "switch", "area": "zws", "device_id": "D1"}
    ents["sensor.zws_ac_pwr"] = {"friendly_name": "主卧室空调 电功率", "state": "2.0",
                                 "domain": "sensor", "area": "zws", "device_id": "D1"}
    # 设备 D2：主卧室床头灯（light+sensor）
    ents["light.zws_bedside"] = {"friendly_name": "主卧室床头灯 灯", "state": "unavailable",
                                 "domain": "light", "area": "zws", "device_id": "D2"}
    ents["sensor.zws_bedside_cm"] = {"friendly_name": "主卧室床头灯 颜色模式", "state": "unavailable",
                                      "domain": "sensor", "area": "zws", "device_id": "D2"}
    # 无设备绑定：主卧室人体传感器
    ents["binary_sensor.zws_motion"] = {"friendly_name": "主卧室 有人无人", "state": "off",
                                        "domain": "binary_sensor", "area": "zws"}
    # 厨房实体（应被区域过滤排除）
    ents["light.kitchen"] = {"friendly_name": "厨房灯", "state": "off",
                             "domain": "light", "area": "cf", "device_id": "DK"}
    gw.state.set_device_catalog({"entities": ents,
                                 "freshness": "2026-07-11T00:00:00+00:00"})
    gw.state.save_entity_mapping({
        "areas": {"zws": "主卧室", "cf": "厨房"},
        "room_aliases": {"主卧室": "zws", "厨房": "cf", "全屋": "__all__"},
        "mappings": {},
    })


class TestRoomSummaryTwoLevel(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()
        build_master_bedroom_catalog(self.gw)

    def test_default_is_device_overview(self):
        r = self.gw.room_summary("主卧室")
        self.assertTrue(r["ok"])
        self.assertEqual(r["view"], "devices")
        self.assertEqual(r["device_count"], 2)        # D1 + D2
        self.assertEqual(r["ungrouped_count"], 1)     # binary_sensor
        self.assertEqual(r["total_entities"], 6)      # 主卧室 6 个（厨房排除）
        # 设备级总览不含逐实体明细
        self.assertNotIn("entities", r["devices"][0])
        self.assertIn("name", r["devices"][0])
        self.assertIn("domains", r["devices"][0])
        self.assertIn("key_states", r["devices"][0])
        # 体积约束：默认总览应很小（<5KB）
        self.assertLess(len(json.dumps(r, ensure_ascii=False)), 5000)

    def test_overview_uses_key_domain_for_repr_and_states(self):
        r = self.gw.room_summary("主卧室")
        by_id = {d["device_id"]: d for d in r["devices"]}
        # D1 代表名应取关键域(climate)实体，而非 sensor
        self.assertEqual(by_id["D1"]["name"], "主卧室空调 空调")
        # key_states 含 climate/switch 的当前 state
        self.assertEqual(by_id["D1"]["key_states"]["climate.zws_ac"], "off")
        self.assertEqual(by_id["D1"]["key_states"]["switch.zws_ac_vs"], "on")

    def test_drill_device_returns_full_entities(self):
        r = self.gw.room_summary("主卧室", device="D1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["entity_count"], 3)
        eids = {e["entity_id"] for e in r["entities"]}
        self.assertEqual(eids, {"climate.zws_ac", "switch.zws_ac_vs", "sensor.zws_ac_pwr"})

    def test_drill_unknown_device_errors(self):
        r = self.gw.room_summary("主卧室", device="NOPE")
        self.assertFalse(r["ok"])

    def test_view_full_backward_compat(self):
        r = self.gw.room_summary("主卧室", view="full")
        self.assertEqual(r["view"], "full")
        self.assertEqual(r["device_count"], 2)
        # full 模式 devices[] 含 entities 明细
        self.assertIn("entities", r["devices"][0])
        total = sum(len(d["entities"]) for d in r["devices"]) + r["ungrouped_count"]
        self.assertEqual(total, 6)

    def test_unknown_area_errors(self):
        r = self.gw.room_summary("不存在的房间")
        self.assertFalse(r["ok"])

    def test_pagination_of_device_overview(self):
        # 造更多设备验证 limit/offset 分页
        ents = dict(self.gw.state.get_device_catalog()["entities"])
        for i in range(40):
            ents[f"switch.zws_extra_{i}"] = {"friendly_name": f"主卧室额外{i}", "state": "off",
                                             "domain": "switch", "area": "zws", "device_id": f"DX{i}"}
        self.gw.state.set_device_catalog({"entities": ents})
        p1 = self.gw.room_summary("主卧室", limit=10, offset=0)
        self.assertEqual(len(p1["devices"]), 10)
        self.assertTrue(p1["truncated"])
        self.assertEqual(p1["next_offset"], 10)
        p2 = self.gw.room_summary("主卧室", limit=10, offset=10)
        self.assertEqual(len(p2["devices"]), 10)
        self.assertEqual(p2["next_offset"], 20)


class TestExportRoomMarkdown(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()
        build_master_bedroom_catalog(self.gw)

    def test_export_full_room(self):
        r = self.gw.export_room_markdown("主卧室")
        self.assertTrue(r["ok"])
        self.assertEqual(r["entity_count"], 6)
        md = r["markdown"]
        # 含分类概览 + 各域明细表头
        self.assertIn("## 分类概览", md)
        self.assertIn("climate", md)
        self.assertIn("light", md)
        self.assertIn("binary_sensor", md)
        # 含 entity_id 与友好名
        self.assertIn("climate.zws_ac", md)
        self.assertIn("主卧室空调 空调", md)
        # 含可能状态（affordance，格式为 on/off/unavailable/unknown）
        self.assertIn("on/off/unavailable/unknown", md)
        # 厨房实体被区域过滤排除
        self.assertNotIn("light.kitchen", md)
        self.assertGreater(r["size_bytes"], 0)

    def test_export_single_domain_smaller(self):
        r = self.gw.export_room_markdown("主卧室", domain="light")
        self.assertTrue(r["ok"])
        self.assertEqual(r["entity_count"], 1)
        self.assertIn("light.zws_bedside", r["markdown"])
        self.assertNotIn("climate.zws_ac", r["markdown"])

    def test_export_returns_domains_and_per_domain(self):
        r = self.gw.export_room_markdown("主卧室")
        self.assertTrue(r["ok"])
        # domains 列出全部可下钻域；per_domain 计数与实体分布一致
        self.assertIn("climate", r["domains"])
        self.assertIn("light", r["domains"])
        self.assertEqual(r["per_domain"]["climate"], 1)
        self.assertEqual(r["per_domain"]["light"], 1)
        # 弱客户端救命绳：domains 可驱动逐域下钻
        self.assertIsInstance(r["domains"], list)
        self.assertGreater(len(r["domains"]), 0)

    def test_export_single_domain_overview_only_that_domain(self):
        # 指定 domain 时概览只列该域（缩小体积，避免 16 域全列）
        r = self.gw.export_room_markdown("主卧室", domain="light")
        md = r["markdown"]
        self.assertIn("## 分类概览", md)
        self.assertIn("light", md)
        # 不应出现其它域的概览行
        self.assertNotIn("climate", md.split("## 分类概览")[1].split("### ")[0].replace("light", ""))

    def test_export_domain_limit_offset_paginates(self):
        # 注入 40 个 sensor，验证单域内 limit/offset 分页
        ents = dict(self.gw.state.get_device_catalog()["entities"])
        for i in range(40):
            ents[f"sensor.zws_s_{i}"] = {"friendly_name": f"主卧室传感器{i}", "state": "1",
                                         "domain": "sensor", "area": "zws", "device_id": f"DS{i}"}
        self.gw.state.set_device_catalog({"entities": ents})
        p1 = self.gw.export_room_markdown("主卧室", domain="sensor", limit=15, offset=0)
        self.assertEqual(p1["entity_count"], 15)
        self.assertTrue(p1["truncated"])
        self.assertEqual(p1["next_offset"], 15)
        p2 = self.gw.export_room_markdown("主卧室", domain="sensor", limit=15, offset=15)
        self.assertEqual(p2["entity_count"], 15)
        self.assertEqual(p2["next_offset"], 30)
        # 概览只列 sensor
        self.assertIn("sensor", p1["markdown"])
        self.assertNotIn("climate", p1["markdown"].split("## 分类概览")[1])

    def test_export_unknown_area_errors(self):
        r = self.gw.export_room_markdown("火星")
        self.assertFalse(r["ok"])


class TestDiscoverDefaultLimit(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()
        # 构造 35 个主卧室 sensor，验证默认 limit=30 分页
        ents = {}
        for i in range(35):
            ents[f"sensor.zws_s_{i}"] = {"friendly_name": f"主卧室传感器{i}", "state": "1",
                                         "domain": "sensor", "area": "zws", "device_id": f"DS{i}"}
        self.gw.state.set_device_catalog({"entities": ents})
        self.gw.state.save_entity_mapping({
            "areas": {"zws": "主卧室"}, "room_aliases": {"主卧室": "zws"}, "mappings": {},
        })

    def test_default_limit_30_paginates(self):
        r = self.gw.discover(area="主卧室")   # 不传 limit → 默认 30
        self.assertEqual(r["returned"], 30)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["next_offset"], 30)
        self.assertEqual(r["matched_count"], 35)


class TestExportRoomAll(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()
        ents = {}
        for i in range(35):
            ents[f"sensor.zws_s_{i}"] = {"friendly_name": f"主卧室传感器{i}", "state": "1",
                                         "domain": "sensor", "area": "zws", "device_id": f"DS{i}"}
        for i in range(10):
            ents[f"switch.zws_sw_{i}"] = {"friendly_name": f"主卧室开关{i}", "state": "off",
                                          "domain": "switch", "area": "zws", "device_id": f"DS{i}"}
        self.gw.state.set_device_catalog({"entities": ents})
        self.gw.state.save_entity_mapping({
            "areas": {"zws": "主卧室"}, "room_aliases": {"主卧室": "zws"}, "mappings": {},
        })

    def test_export_all_assembles_and_writes(self):
        r = self.gw.export_room_markdown_all("主卧室")
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 45)  # 35 sensor + 10 switch
        # 摘要极小：不把大内容塞进上下文
        self.assertLess(len(json.dumps(r, ensure_ascii=False)), 2000)
        # 落盘成功，文件含两个域的明细
        self.assertTrue(os.path.isfile(r["path"]))
        md = open(r["path"], encoding="utf-8").read()
        self.assertIn("sensor", md)
        self.assertIn("switch", md)


class TestAreaMatch(unittest.TestCase):
    """_area_match 区域归属：信任显式区域、父子子串、禁用反向子串误归。"""
    def _m(self, area, fn=""):
        return {"area": area, "friendly_name": fn}

    def test_exact_area_matches(self):
        self.assertTrue(Gateway._area_match(self._m("主卧室"), "主卧室", "主卧室"))

    def test_parent_in_child_substring(self):
        # query 主卧室 应含 主卧室浴室（父→子）
        self.assertTrue(Gateway._area_match(self._m("主卧室浴室"), "主卧室", "主卧室"))

    def test_generic_bedroom_not_in_master_bedroom(self):
        # 反向子串禁用：area="卧室"(通用卧室) 是 "主卧室" 子串，但绝不可误归主卧室
        self.assertFalse(Gateway._area_match(self._m("卧室"), "主卧室", "主卧室"))
        self.assertFalse(Gateway._area_match(self._m("房间"), "主卧室", "主卧室"))

    def test_other_area_excluded(self):
        self.assertFalse(Gateway._area_match(self._m("机房"), "主卧室", "主卧室"))

    def test_empty_area_falls_back_to_friendly_name(self):
        # 无区域：退化用 friendly_name 子串（优雅降级）
        self.assertTrue(Gateway._area_match(self._m("", "主卧室路由 MiAiSoundbox"), "主卧室", "主卧室"))
        self.assertFalse(Gateway._area_match(self._m("", "书房路由 X"), "主卧室", "主卧室"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
