#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署后观测 D（tap 风格）离线回归（#9）。

observe_postconditions / observe_after_deploy 读 HA 当前状态与预期后置条件比对，
离线（FakeHA/vhass）与线上（真实 HA）通吃。验证：
1. 实际状态==预期 → ok；!= → 失败并列出差异。
2. observe_after_deploy 合并 HA 观测 + NR debug 尽力快照（无 NR 授权时
   在 nr_note 标注，不阻塞）；HA 侧始终可用。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)  # 复用 test_gateway.make_gateway 假后端

from test_gateway import make_gateway  # noqa: E402


class TestObservePost(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_observe_pass(self):
        # make_gateway 的 FakeHA：light.living_room=off
        r = self.gw.observe_postconditions(
            [{"entity_id": "light.living_room", "state": "off"}])
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["assertions"]), 1)
        self.assertTrue(r["assertions"][0]["ok"])
        self.assertEqual(r["assertions"][0]["actual"], "off")

    def test_observe_fail_reports_diff(self):
        # 预期 on，但真实状态 off → 失败且给出 actual
        r = self.gw.observe_postconditions(
            [{"entity_id": "light.living_room", "state": "on"}])
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["failures"]), 1)
        self.assertEqual(r["failures"][0]["expected"], "on")
        self.assertEqual(r["failures"][0]["actual"], "off")

    def test_observe_after_deploy_merges(self):
        # 离线 FakeHA 无 NR 授权：HA 侧工作，NR 侧给 note 不阻塞
        r = self.gw.observe_after_deploy(
            [{"entity_id": "light.living_room", "state": "off"}], flow_id="x")
        self.assertTrue(r["ok"])              # HA 侧 ok
        self.assertIsNotNone(r["ha"])
        self.assertIsNone(r["nr_debug"])     # 无 capture_debug
        self.assertIsNotNone(r["nr_note"])   # 标注需 NR 授权


if __name__ == "__main__":
    unittest.main(verbosity=2)
