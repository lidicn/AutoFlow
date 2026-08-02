#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌套条件 / 多路分支（WB4 #4, iss_bbbe45eb04）回归。

解析器原拒收「分支 体内的 分支」「否则如果 链」，只能单层 分支+否则。
现重构为上下文栈，支持：
  - 同级多路 if/elif/else（否则如果: / elif 关键字，或连续 分支:）
  - 任意层级嵌套（分支 体内再 分支；否则 体内再 分支；门/并行 体内再 分支）
缩进按层级正确 pop，同级分支归属同一 switch、否则归属 else_body。

验证维度：parse 结构正确 + compile_dsl 产出合法 flow（含嵌套 switch 节点、
连线闭合、0 error 级 lint）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import parse, compile_dsl, DSLError
from autoflow_gateway.flow_linter import lint_flow


def _switch_count(flow):
    return sum(1 for n in flow["nodes"] if n.get("type") == "switch")


def _wires_closed(flow):
    ids = {n["id"] for n in flow["nodes"]}
    for n in flow["nodes"]:
        for out in n.get("wires", []) or []:
            for t in out:
                if t and t not in ids:
                    return False, t
    return True, None


def _lint_errors(flow):
    return [i for i in lint_flow(flow) if i.get("level") == "error"]


class TestNestedConditions(unittest.TestCase):
    def test_flat_if_else_preserved(self):
        """回归：扁平 if/else 仍正确解析（不破坏既有能力）。"""
        dsl = (
            "场景: 扁平\n触发: inject\n"
            "分支: payload.a > 1\n  动作: light.turn_on(light.x)\n"
            "否则:\n  动作: light.turn_off(light.x)\n"
        )
        s = parse(dsl)
        self.assertEqual(len(s.body), 1)
        sw = s.body[0]
        self.assertEqual(len(sw.branches), 1)
        self.assertTrue(sw.else_body, "否则 应落入 else_body")

    def test_multi_branch_elif_keyword(self):
        """否则如果: 链 → 同一 switch 的多个分支 + else。"""
        dsl = (
            "场景: 多路\n触发: inject\n"
            "分支: payload.x == 1\n  动作: light.turn_on(light.a)\n"
            "否则如果: payload.x == 2\n  动作: light.turn_on(light.b)\n"
            "否则如果: payload.x == 3\n  动作: light.turn_on(light.c)\n"
            "否则:\n  动作: light.turn_off(light.a)\n"
        )
        s = parse(dsl)
        sw = s.body[0]
        self.assertEqual(len(sw.branches), 3, "否则如果 应追加为 3 个分支")
        self.assertTrue(sw.else_body)

    def test_multi_branch_repeated_branch_keyword(self):
        """连续 分支: 在同级 → 同一 switch 多分支（向后兼容写法）。"""
        dsl = (
            "场景: 连续分支\n触发: inject\n"
            "分支: payload.x == 1\n  动作: light.turn_on(light.a)\n"
            "分支: payload.x == 2\n  动作: light.turn_on(light.b)\n"
            "否则:\n  动作: light.turn_off(light.a)\n"
        )
        s = parse(dsl)
        sw = s.body[0]
        self.assertEqual(len(sw.branches), 2)

    def test_nested_branch_in_branch(self):
        """分支 体内再 分支 → 嵌套 switch 挂在父分支 body。"""
        dsl = (
            "场景: 嵌套\n触发: inject\n"
            "分支: payload.a > 1\n"
            "  动作: light.turn_on(light.a)\n"
            "  分支: payload.b > 2\n"
            "    动作: light.turn_on(light.b)\n"
            "  动作: light.turn_on(light.c)\n"   # 父分支体内的同级动作（嵌套 switch 之后）
            "否则:\n  动作: light.turn_off(light.a)\n"
        )
        s = parse(dsl)
        sw = s.body[0]
        outer_branch = sw.branches[0]
        # 父分支 body 含一个嵌套 switch + 一个动作
        nested = [st for st in outer_branch.body if st.__class__.__name__ == "Switch"]
        self.assertEqual(len(nested), 1, "父分支体应含 1 个嵌套 switch")
        # 父分支体 = 动作a + 嵌套switch + 动作c（嵌套 switch 之后的同级动作回到父分支）
        self.assertEqual(len(outer_branch.body), 3, "父分支体 = 动作a + 嵌套switch + 动作c")
        # 嵌套 switch 的分支体收到动作
        self.assertTrue(nested[0].branches[0].body, "嵌套分支体应收到内部动作")

    def test_nested_branch_in_else(self):
        """否则 体内再 分支 → 嵌套 switch 挂在父 else_body。"""
        dsl = (
            "场景: 否则嵌套\n触发: inject\n"
            "分支: payload.a > 1\n  动作: light.turn_on(light.a)\n"
            "否则:\n"
            "  动作: light.turn_off(light.a)\n"
            "  分支: payload.b > 2\n"
            "    动作: light.turn_on(light.b)\n"
        )
        s = parse(dsl)
        sw = s.body[0]
        nested = [st for st in sw.else_body if st.__class__.__name__ == "Switch"]
        self.assertEqual(len(nested), 1, "否则 体应含 1 个嵌套 switch")

    def test_switch_inside_gate(self):
        """查询(gate) 门体内再 分支 → 嵌套 switch 挂在门 body。"""
        dsl = (
            "场景: 门内嵌套\n触发: inject\n"
            "查询: light.x on\n"
            "  动作: light.turn_on(light.x)\n"
            "  分支: payload.y > 1\n"
            "    动作: light.turn_on(light.y)\n"
            "否则:\n  动作: light.turn_off(light.x)\n"
        )
        s = parse(dsl)
        gate = s.body[0]
        nested = [st for st in gate.body if st.__class__.__name__ == "Switch"]
        self.assertEqual(len(nested), 1, "门体应含 1 个嵌套 switch")

    def test_elif_first_without_if_is_lenient(self):
        """否则如果 作为首个块关键字（无前置 分支）→ 仍生成含该分支的 switch（容错）。"""
        dsl = (
            "场景: 否则如果先行\n触发: inject\n"
            "否则如果: payload.x == 1\n  动作: light.turn_on(light.a)\n"
            "否则:\n  动作: light.turn_off(light.a)\n"
        )
        s = parse(dsl)
        sw = s.body[0]
        self.assertEqual(len(sw.branches), 1)

    def test_compile_nested_valid_wiring(self):
        """嵌套场景 compile_dsl 产出合法 flow：含 2 个 switch、连线闭合、0 error lint。"""
        dsl = (
            "场景: 编译嵌套\n触发: inject\n"
            "分支: payload.a > 1\n"
            "  动作: light.turn_on(light.a)\n"
            "  分支: payload.b > 2\n"
            "    动作: light.turn_on(light.b)\n"
            "  动作: light.turn_on(light.c)\n"
            "否则:\n  动作: light.turn_off(light.a)\n"
        )
        flow = compile_dsl(dsl, target="staging")
        self.assertGreaterEqual(_switch_count(flow), 2, "应编译出至少 2 个 switch 节点")
        ok, bad = _wires_closed(flow)
        self.assertTrue(ok, f"存在悬空连线指向 {bad}")
        errs = _lint_errors(flow)
        self.assertEqual(errs, [], f"嵌套 flow 不应有 error 级 lint: {errs}")


if __name__ == "__main__":
    unittest.main()
