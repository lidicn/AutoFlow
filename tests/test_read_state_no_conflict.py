#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4 误判澄清回归（WB22 T4a 实证）：

WB22 报告声称 `取值:` 编译产物 outputProperties=['payload.state','payload.state'] 冲突，
导致后续「提取」恒 undefined。实测（_diag_compile.py）证明产物为
  outputProperties=[{payload.<field>,entityState},{payload.state,entityState}]
——两个**不同**字段，且 valueType=entityState 取真实数值（D2 已修 override_payload 真 bug）。
故 T4a 系测试者误录 payload.温度→payload.state，编译器无 bug。本测试锁定该正确形态，
防未来回归把 取值 改出字段冲突。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl


def _read_state_node(flow):
    return next((n for n in flow.get("nodes", [])
                 if n.get("type") == "api-current-state"), None)


class TestReadStateNoConflict(unittest.TestCase):
    def test_read_state_uses_entity_state_not_timestamp(self):
        """取值 节点：两个 outputProperties 字段不同、均为 entityState 取数。"""
        dsl = ("场景: 取值验证\n触发: inject\n"
               "取值: sensor.study_temperature 温度\n")
        flow = compile_dsl(dsl, target="staging")
        node = _read_state_node(flow)
        self.assertIsNotNone(node, "应编译出 api-current-state 节点")
        ops = node.get("outputProperties", [])
        # #712 起为 3 条：[0] payload 容器保底（jsonata） + 具名字段 + state 别名
        self.assertEqual(len(ops), 3,
                         "具名字段取值应写 3 个 outputProperty（容器保底 + 字段 + state 别名）")
        self.assertEqual(ops[0].get("property"), "payload",
                         "首项须为 payload 容器保底，且排在字段写入之前")
        self.assertEqual(ops[0].get("valueType"), "jsonata",
                         "容器保底用 jsonata 条件表达式：已是对象则保留，否则重置为 {}")
        state_ops = ops[1:]
        props = [o.get("property") for o in state_ops]
        self.assertIn("payload.温度", props, "应包含具名字段 payload.温度")
        self.assertIn("payload.state", props, "应包含 payload.state 别名")
        self.assertEqual(len(set(props)), 2, "两个字段必须不同（WB22 误称两者都是 payload.state）")
        for o in state_ops:
            self.assertEqual(o.get("valueType"), "entityState",
                             "valueType 必须是 entityState（取真实数值，非时间戳）")

    def test_read_state_no_field_writes_payload(self):
        """无字段取值：整条 msg.payload = state（单 outputProperty, payload）。"""
        dsl = ("场景: 整值\n触发: inject\n取值: sensor.study_temperature\n")
        flow = compile_dsl(dsl, target="staging")
        node = _read_state_node(flow)
        self.assertIsNotNone(node)
        ops = node.get("outputProperties", [])
        # 无字段取值整体覆盖 msg.payload（顶层单段路径，setObjectProperty 恒成功），
        # 不存在标量吞写问题 → 不应画蛇添足加容器保底。
        self.assertEqual(len(ops), 1, "无字段取值应只有 1 个 outputProperty")
        self.assertEqual(ops[0].get("property"), "payload")
        self.assertEqual(ops[0].get("valueType"), "entityState")

    def test_read_state_guards_scalar_payload(self):
        """WB72 缺陷#3 / #712：具名字段取值必须先把 msg.payload 归一为对象。

        NR `RED.util.setObjectProperty` 写 `payload.<field>` 时逐段下钻，中间值若既非
        object 也非 undefined（典型：inject 默认 payloadType="date" → payload 是时间戳
        数值）就 `return false` —— **静默失败**，字段从未写入、下游读 undefined、链路
        无声断裂且静态校验全盲。故须在字段写入前插 payload 容器保底。
        """
        dsl = ("场景: 双取值\n触发: inject\n"
               "取值: sensor.study_illuminance lux\n"
               "取值: sensor.study_temperature comp\n")
        flow = compile_dsl(dsl, target="staging")
        reads = [n for n in flow.get("nodes", [])
                 if n.get("type") == "api-current-state" and n.get("outputs") == 1]
        self.assertEqual(len(reads), 2, "应编译出 2 条取值节点")
        for node in reads:
            ops = node.get("outputProperties", [])
            guard = ops[0]
            self.assertEqual(guard.get("property"), "payload")
            self.assertEqual(guard.get("propertyType"), "msg")
            self.assertEqual(guard.get("valueType"), "jsonata")
            # 条件保留语义：已是对象则原样返回（多次取值可累积字段，互不覆盖），
            # 否则重置为空对象。若退化成无条件 {}，第二条取值会抹掉第一条的字段。
            expr = guard.get("value", "")
            self.assertIn("$type(payload)", expr, f"须按 payload 实际类型分支：{expr}")
            self.assertIn("? payload :", expr, f"已是对象须原样保留（防互相覆盖）：{expr}")
            self.assertIn("{}", expr, f"非对象须重置为空对象：{expr}")
            # 顺序即执行序：保底必须排在所有 payload.<sub> 子路径写入之前，否则无效。
            sub_idx = [i for i, o in enumerate(ops)
                       if str(o.get("property", "")).startswith("payload.")]
            self.assertTrue(sub_idx, "具名字段取值应有 payload.<field> 写入")
            self.assertLess(0, min(sub_idx), "payload 容器保底必须排在子字段写入之前")


if __name__ == "__main__":
    unittest.main(verbosity=2)
