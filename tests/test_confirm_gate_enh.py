#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认闸增强离线回归（#8）：commit 进闸时须带结构化 diff + 爆炸半径 + 风险级。

不触真实 NR/HA：复用 test_gateway 的 FakeNR/FakeHA 桩。
验证：
1. commit_scene 返回 diff（node_count / node_types / entities / services / is_update）
   与 blast_radius（恒为 1 个 flow）+ risk_level。
2. 带 target_flow_id 时 diff.is_update=True 且 new_node_ids 非空（增量比对）。
3. commit_ha_service 同样带 diff（实体 / 服务）与 risk_level。
4. list_pending / get_confirm_detail 透传 diff 与 blast_radius，供确认闸渲染。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)  # 复用 test_gateway.make_gateway 假后端

from test_gateway import make_gateway  # noqa: E402

VALID_SCENE = {
    "name": "回家开灯",
    "description": "人回家打开客厅与玄关灯",
    "agent_id": "agent_A",
    "trigger": [{"type": "state_changed", "entity_id": "device_tracker.me", "state": "home"}],
    "condition": [],
    "action": [
        {"domain": "light", "service": "turn_on", "entity_id": "light.living_room"},
        {"domain": "light", "service": "turn_off", "entity_id": "light.entrance"},
    ],
    "expected_postconditions": [
        {"entity_id": "light.living_room", "attribute": "state", "op": "equals", "value": "on"},
    ],
}


class TestConfirmGateEnh(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_commit_scene_diff_and_blast(self):
        r = self.gw.commit_scene(VALID_SCENE)
        self.assertTrue(r["ok"])
        # 风险级 / 爆炸半径
        self.assertIn(r["risk_level"], ("low", "medium", "high"))
        self.assertEqual(r["blast_radius"], 1)
        # 结构化 diff
        d = r["diff"]
        self.assertFalse(d["is_update"])              # 新建
        self.assertGreater(d["node_count"], 0)
        self.assertIn("api-call-service", d["node_types"])
        self.assertIn("light.living_room", d["entities"])
        self.assertIn("light.entrance", d["entities"])
        self.assertIn("light.turn_on", d["services"])
        self.assertIn("light.turn_off", d["services"])

    def test_commit_scene_update_sets_is_update(self):
        r = self.gw.commit_scene({**VALID_SCENE, "target_flow_id": "abc123"})
        self.assertTrue(r["ok"])
        d = r["diff"]
        self.assertTrue(d["is_update"])               # 给了 target_flow_id → 更新
        self.assertGreater(len(d["new_node_ids"]), 0)  # 与空线上 flow 比对：全部为新

    def test_commit_ha_service_diff(self):
        r = self.gw.commit_ha_service("lock", "unlock", {"entity_id": "lock.front_door"}, "agent_B")
        self.assertTrue(r["ok"])
        self.assertEqual(r["risk_level"], "high")     # 锁是高风险的
        self.assertEqual(r["blast_radius"], 1)
        d = r["diff"]
        self.assertIn("lock.front_door", d["entities"])
        self.assertIn("lock.unlock", d["services"])

    def test_list_pending_exposes_diff(self):
        self.gw.commit_scene(VALID_SCENE)
        pend = self.gw.list_pending()
        self.assertEqual(len(pend), 1)
        op = pend[0]
        self.assertEqual(op["blast_radius"], 1)
        self.assertIn("diff", op["payload"])
        self.assertIn("light.living_room", op["payload"]["diff"]["entities"])

    def test_get_confirm_detail(self):
        r = self.gw.commit_scene(VALID_SCENE)
        detail = self.gw.get_confirm_detail(r["pending_id"])
        self.assertIsNotNone(detail)
        self.assertIn("diff", detail["payload"])
        self.assertIn("risk_level", detail)
        self.assertEqual(detail["blast_radius"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
