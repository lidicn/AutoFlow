#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B4 回归：动作目标实体数组 [a, b] 解析。

旧实现 `动作: light.turn_on([light.a, light.b], brightness_pct=80)` 经
`inner.split(",")` 切块时把 `[` 并进 parts[0]='[light.a'，`light.b]` 变成伪参数，
最终 entityId 错编为 ['[light.a']、`light.b` 被吞。

修复：`_parse_action` 识别首 token 的方括号数组（或 entity_id=[a,b] kwarg），
拆成多实体；`_emit_action` 逐个 _resolve_entity 产出 entityId=[a, b] 列表。
"""
import os
import sys
import json
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl


def _api_call_nodes(flow):
    return [n for n in flow["nodes"] if n["type"] == "api-call-service"]


def _entity_ids(flow):
    return [_n.get("entityId") for _n in _api_call_nodes(flow)]


class TestB4ArrayEntityId(unittest.TestCase):
    def test_array_target_two_entities(self):
        """[light.a, light.b] → entityId 正确拆成两实体，light.b 不再被吞。"""
        dsl = """
场景: 数组目标
触发: inject
动作: light.turn_on([light.a, light.b], brightness_pct=80)
"""
        flow = compile_dsl(dsl)
        ids = _entity_ids(flow)
        self.assertEqual(len(ids), 1, "应只有一个 api-call-service")
        self.assertEqual(ids[0], ["light.a", "light.b"],
                         "数组目标应拆成 [light.a, light.b]，不可吞掉 light.b 或残留 '['")
        self.assertNotIn("[light.a", ids[0], "首实体不得残留左括号")
        # data 数值归一仍为 number
        node = _api_call_nodes(flow)[0]
        data = json.loads(node["data"])
        self.assertEqual(data.get("brightness_pct"), 80, "brightness_pct 须为数值 80")

    def test_array_target_no_params(self):
        """[light.a, light.b] 无参数也能正确拆成两实体。"""
        dsl = """
场景: 数组目标无参
触发: inject
动作: switch.turn_on([switch.a, switch.b])
"""
        flow = compile_dsl(dsl)
        ids = _entity_ids(flow)
        self.assertEqual(ids[0], ["switch.a", "switch.b"])

    def test_single_target_backward_compat(self):
        """单实体旧写法仍产出 [单元素] 列表，向后兼容。"""
        dsl = """
场景: 单实体
触发: inject
动作: light.turn_on(light.c)
"""
        flow = compile_dsl(dsl)
        ids = _entity_ids(flow)
        self.assertEqual(ids[0], ["light.c"])

    def test_entity_id_kwarg_array(self):
        """entity_id=[a, b] kwarg 形态同样展开为多实体。"""
        dsl = """
场景: entity_id数组
触发: inject
动作: switch.turn_on(entity_id=[switch.x, switch.y])
"""
        flow = compile_dsl(dsl)
        ids = _entity_ids(flow)
        self.assertEqual(ids[0], ["switch.x", "switch.y"])

    def test_resolver_applied_per_target(self):
        """多实体逐个经实体解析器（resolver=None 时恒等，验证形态而非解析结果）。"""
        dsl = """
场景: 解析器逐实体
触发: inject
动作: light.turn_off([light.m, light.n])
"""
        flow = compile_dsl(dsl)
        ids = _entity_ids(flow)
        self.assertEqual(ids[0], ["light.m", "light.n"])


if __name__ == "__main__":
    unittest.main()
