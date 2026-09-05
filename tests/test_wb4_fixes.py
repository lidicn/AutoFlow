#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB4 审计报告确定性修复回归（#508 R25 误报 / #509 条件:+否则 引导）。

直接可修的两项（决定性复核结论，非凭记忆）：

#508 (iss_8065903167, medium, R25 误报)
  └─ flow_linter._lint_comment_relay 的 (a) 分支用 len(wires)（外层列表数）判断
     comment 是否带 outgoing wires，wires=[[]] 外层长度=1 → 误报『带 1 条』。
     修复：统计内层真实 target 数。回归见 test_bug3_comment_wire_lint.py。

#509 (iss_752cee0066, low, 文档/引导不一致)
  └─ 『条件:』是场景级前置条件（无否则、被大量测试依赖，不能改别名），而 dsl_help
     仅写『(可选)』，且 否则 解析报错未提示改用 分支:。
     修复：(1) dsl_help 『条件』条目澄清；(2) 否则 解析报错在场景含 条件: 时加引导。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import DSLError, parse


class TestWB4ConditionElseGuidance(unittest.TestCase):
    def test_condition_else_raises_with_branch_hint(self):
        """场景用了『条件:』后又写『否则:』（无 分支 块）→ 报错且引导改用 分支:。"""
        dsl = (
            "场景: 条件加否则误用\n"
            "触发: inject\n"
            "条件: payload.x > 1\n"
            "否则:\n"
            "  动作: light.turn_on(light.x)\n"
        )
        with self.assertRaises(DSLError) as ctx:
            parse(dsl)
        msg = str(ctx.exception)
        self.assertIn("分支", msg,
                      "场景含『条件:』时，否则报错必须引导改用『分支:』语法")
        self.assertIn("场景级前置条件", msg,
                      "应提示 条件: 本身无否则分支")

    def test_plain_else_without_condition_no_special_hint(self):
        """对照：仅『否则:』无 分支/条件 → 报错，但不含『场景级前置条件』特例提示。"""
        dsl = (
            "场景: 裸否则\n"
            "触发: inject\n"
            "否则:\n"
            "  动作: light.turn_on(light.x)\n"
        )
        with self.assertRaises(DSLError) as ctx:
            parse(dsl)
        msg = str(ctx.exception)
        self.assertIn("否则 必须出现在 分支", msg)
        self.assertNotIn("场景级前置条件", msg,
                         "无 条件: 时不应出现 条件 特例提示")

    def test_branch_else_still_works(self):
        """正向对照：『分支: ... 否则:』语法应正常编译，不报错。"""
        dsl = (
            "场景: 正确分支否则\n"
            "触发: inject\n"
            "分支: payload.x > 1\n"
            "  动作: light.turn_on(light.x)\n"
            "否则:\n"
            "  动作: light.turn_off(light.x)\n"
        )
        # parse 成功且场景 body 含 Switch（含 else 分支），不应抛错
        scene = parse(dsl)
        self.assertTrue(scene.body, "分支否则场景应成功解析出 body")
        switch = scene.body[0]
        self.assertTrue(switch.else_body,
                        "否则 分支应被解析为 Switch.else_body 而非报错")


if __name__ == "__main__":
    unittest.main()
