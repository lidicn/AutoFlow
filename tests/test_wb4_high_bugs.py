#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB4 两个 HIGH bug 的确定性回归（#2 for 持久等待 / #1 取值 entityId）。

#2 (iss_110d1054be)：`触发: X on 持续N分钟` 的 for 字段曾被静默丢弃——
   历史仅 prod 发射 server-state-changed+for，staging/e2e 降级为 inject 丢 for。
   现所有 target 统一发射 server-state-changed+for；e2e 经 _e2e_prepare_flow
   把入口原地转合成 inject 点燃。

#1 (iss_25419fa7a8)：`取值:`(api-current-state) 运行时丢 entityId，e2e 报
   ValidationError "entityId" is required。根因：合成入口 inject 的 msg.topic 在
   blockInputOverrides=False 时覆盖 entityId。修复：e2e 插桩副本强制
   blockInputOverrides=True。

全部离线（FakeNR/FakeHA 桩），不触真实 NR/HA。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.gateway import Gateway
from test_gateway import make_gateway


def _entry_trigger(flow):
    """返回没有上游连线指向它的、且类型为触发入口的节点。"""
    nodes = flow.get("nodes", [])
    incoming = set()
    for n in nodes:
        for w in (n.get("wires") or []):
            if isinstance(w, list):
                incoming.update(w)
    for n in nodes:
        if n["id"] in incoming:
            continue
        if n.get("type") in ("inject", "server-state-changed", "time", "trigger"):
            return n
    return None


class TestWB4ForPersists(unittest.TestCase):
    def test_for_emitted_for_all_targets(self):
        dsl = ("场景: 持续触发\n"
               "触发: binary_sensor.motion on 持续5分钟\n"
               "动作: light.turn_on(light.x)")
        scene = parse(dsl)
        for target in ("staging", "prod"):
            with self.subTest(target=target):
                flow = compile(scene, target=target)
                trig = _entry_trigger(flow)
                self.assertIsNotNone(trig, f"{target}: 应有触发节点")
                self.assertEqual(trig.get("type"), "server-state-changed",
                                 f"{target}: 状态触发必须编译为 server-state-changed（不再降级 inject 丢 for）")
                self.assertEqual(str(trig.get("for")), "5",
                                 f"{target}: for 应持久 5 分钟")
                self.assertEqual(trig.get("forType"), "num")
                self.assertEqual(trig.get("forUnits"), "minutes")

    def test_for_duration_conversion(self):
        for txt, expect in (("持续2小时", "120"), ("持续30秒", "0.5"),
                            ("持续10分钟", "10")):
            with self.subTest(txt=txt):
                dsl = (f"场景: t\n触发: binary_sensor.x on {txt}\n"
                       "动作: light.turn_on(light.y)")
                flow = compile(parse(dsl), target="prod")
                trig = _entry_trigger(flow)
                self.assertEqual(str(trig.get("for")), expect,
                                 f"时长『{txt}』应折算为 for={expect}")


class TestWB4E2EPrepare(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_state_entry_converted_to_inject(self):
        """staging 状态触发器现已编译为 server-state-changed；e2e 准备阶段应原地
        转合成 inject，使无 websocket 环境也能点燃（WB4 #2 的 e2e 侧）。"""
        dsl = ("场景: 状态触发e2e\n"
               "触发: binary_sensor.motion on 持续5分钟\n"
               "动作: light.turn_on(light.x)")
        flow = compile(parse(dsl))  # 默认 staging
        trig_before = _entry_trigger(flow)
        self.assertEqual(trig_before.get("type"), "server-state-changed")

        nodes, inject_ids = self.gw._e2e_prepare_flow(flow)
        self.assertTrue(inject_ids, "入口应被转为 inject，inject_ids 非空")
        # 原入口节点应已被原地改为 inject
        converted = next((n for n in nodes if n["id"] == trig_before["id"]), None)
        self.assertIsNotNone(converted)
        self.assertEqual(converted.get("type"), "inject",
                         "server-state-changed 入口应原地转为 inject")
        self.assertNotIn("for", converted,
                         "合成 inject 不应携带 for（已转交入口语义，由 NR 在真实触发时解释）")
        self.assertTrue(converted.get("topic"),
                        "合成 inject 应带 faithful 的实体 topic")

    def test_api_current_state_entity_id_protected(self):
        """`取值:`(api-current-state) 在 e2e 副本应保留 entityId 且设
        blockInputOverrides=True，防止合成入口 topic 污染（WB4 #1 修复）。"""
        dsl = ("场景: 取值保护\n"
               "触发: inject\n"
               "取值: sensor.test_lux lux\n"
               "动作: light.turn_on(light.x)")
        flow = compile(parse(dsl))
        acs_list = [n for n in flow.get("nodes", []) if n.get("type") == "api-current-state"]
        self.assertEqual(len(acs_list), 1)
        self.assertEqual(acs_list[0].get("blockInputOverrides"), False,
                         "编译产物默认 blockInputOverrides=False（by design，真实部署需可被 topic 覆盖）")

        self.gw._e2e_prepare_flow(flow)
        acs = next(n for n in flow.get("nodes", []) if n.get("type") == "api-current-state")
        self.assertEqual(acs.get("entityId"), "sensor.test_lux",
                         "e2e 副本必须保留完整 entityId，不被覆盖")
        self.assertTrue(acs.get("blockInputOverrides"),
                        "e2e 副本必须 blockInputOverrides=True，杜绝 msg.topic 污染 entityId")


if __name__ == "__main__":
    unittest.main()
