# -*- coding: utf-8 -*-
"""A4 · 节点默认字段字典回归测试（unittest）。

验证 dsl_engine._Emitter.add 通过 NODE_DEFAULT_FIELDS 补齐每种节点类型
「普遍存在、可安全缺省」的字段（setdefault：显式优先，只填空缺）：
  - server-state-changed：修正 output_properties(snake) → outputProperties(camel) 误用，并补 stateType
  - api-current-state：补 state_location / override_topic / override_payload

同时验证：
  - 显式发射的字段不被默认值覆盖（setdefault 语义）
  - 默认字段确实出现在编译产物中（代理「红三角/脏节点」字段集一致性）
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import (  # noqa: E402
    compile_dsl, NODE_DEFAULT_FIELDS, _Emitter,
)


def _by_type(flow, t):
    return [n for n in flow["nodes"] if n["type"] == t]


def _expected_defaults(node):
    """返回某节点「实际应具的默认字段集」。

    NODE_DEFAULT_FIELDS 给 api-current-state 注入 state_location="payload"/override_payload=True
    （门体透传语义）；但 read-state 节点(取值 / WB23 #634 修复, outputs=1)由发射点显式中和为
    state_location="data"/override_payload=False，避免节点原生输出(时间戳)与 outputProperties
    (写 payload)冲突。此处据此返回该节点真正应有的默认值，使「默认字段齐全」断言对两类节点都正确。
    """
    defaults = dict(NODE_DEFAULT_FIELDS.get(node["type"], {}))
    if node["type"] == "api-current-state" and node.get("outputs") == 1:
        defaults["state_location"] = "data"
        defaults["override_payload"] = False
    return defaults


class TestNodeDefaultFields(unittest.TestCase):

    def test_default_fields_present_on_server_state_changed(self):
        dsl = ("场景: 书房有人开灯\n"
               "触发: binary_sensor.study_motion 有人\n"
               "动作: light.turn_on(light.study_main, brightness=80)\n"
               "预期:\n  light.study_main = on\n")
        flow = compile_dsl(dsl, target="prod")
        nodes = _by_type(flow, "server-state-changed")
        self.assertTrue(nodes, "应发射 server-state-changed")
        for n in nodes:
            self.assertIn("outputProperties", n, f"server-state-changed 缺 outputProperties: {n}")
            self.assertNotIn("output_properties", n, f"仍残留蛇形键 output_properties: {n}")
            self.assertEqual(n["outputProperties"], [])
            self.assertEqual(n.get("stateType"), "str")

    def test_default_fields_present_on_api_current_state(self):
        dsl = ("场景: 查询并取值\n"
               "触发: inject\n"
               "查询: light.study_main = on\n"
               "  动作: light.turn_on(light.study_main)\n"
               "取值: light.study_main\n"
               "  动作: light.turn_off(light.study_main)\n"
               "预期:\n  light.study_main = on\n")
        flow = compile_dsl(dsl, target="staging")
        nodes = _by_type(flow, "api-current-state")
        self.assertTrue(nodes, "应发射 api-current-state")
        for n in nodes:
            self.assertIs(n.get("override_topic"), False)
            if n.get("outputs") == 1:
                # 取值（read-state，WB23 #634 修复）：_emit_read_state 显式覆盖
                # NODE_DEFAULT_FIELDS 的 state_location="payload"/override_payload=True。
                # 旧值会让节点原生输出（时间戳）与 outputProperties（写 msg.payload.<field>/
                # payload.state）同写 msg.payload → 冲突 → 节点吐时间戳而非数值（全实体类型通病，
                # gate 全盲）。修复：把节点原生状态输出改写到 msg.data（避开 payload），
                # 由 outputProperties 独家负责把实体态写入 msg.payload。
                self.assertEqual(n.get("state_location"), "data")
                self.assertIs(n.get("override_payload"), False)
            else:
                # 查询门（gate）：halt_if 驱动、2 输出，沿用 NODE_DEFAULT_FIELDS 的
                # state_location="payload"/override_payload=True（门体透传语义），保持不变。
                self.assertEqual(n.get("state_location"), "payload")
                self.assertIs(n.get("override_payload"), True)

    def test_all_defaulted_types_have_every_default_key(self):
        dsls = [
            ("场景: S1\n触发: binary_sensor.x 有人\n动作: light.turn_on(light.y)\n预期:\n  light.y = on\n", "prod"),
            ("场景: S2\n触发: inject\n查询: sensor.z = on\n  动作: light.turn_on(light.y)\n预期:\n  light.y = on\n", "staging"),
        ]
        covered = set()
        for dsl, tgt in dsls:
            flow = compile_dsl(dsl, target=tgt)
            for ntype in NODE_DEFAULT_FIELDS:
                for n in _by_type(flow, ntype):
                    covered.add(ntype)
                    for k, v in _expected_defaults(n).items():
                        self.assertIn(k, n, f"[{ntype}] 缺默认字段 {k}: {n}")
                        self.assertEqual(n[k], v, f"[{ntype}] 默认字段 {k} 值不符")
        self.assertGreaterEqual(covered, {"server-state-changed", "api-current-state"},
                                f"未覆盖到预期类型: {covered}")

    def test_explicit_field_wins_over_default(self):
        em = _Emitter("test_flow")
        exp = [{"property": "p", "propertyType": "msg", "value": "", "valueType": "entityState"}]
        nid = em.add("server-state-changed", name="x",
                     server="srv", version=6, entities={}, outputProperties=exp)
        n = em._find(nid)
        self.assertEqual(n["outputProperties"], exp)
        self.assertEqual(n.get("stateType"), "str")
        em.add("inject", name="i", props=[], repeat="", crontab="", once=False,
               payload="", payloadType="date")
        self.assertTrue(all("stateType" not in nd for nd in em.nodes if nd["type"] == "inject"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
