#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB4 #3 (iss_50828738bb) 回归：动作参数字面数值越界 warning 级 lint（R27）。

仅对 api-call-service 节点、dataType=="json"（字面 JSON）、参数是字面数字且命中
已知 HA 值域白名单时告警。变量/jsonata 参数无法静态判定 → 跳过。不硬拦。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.flow_linter import lint_flow


def _r27(flow):
    return [i for i in lint_flow(flow) if i.get("rule") == "R27"]


class TestValueRangeLint(unittest.TestCase):
    def test_out_of_range_brightness_pct_warned(self):
        """brightness_pct=99999 明显越界 → R27 warning。"""
        dsl = ("场景: 越界\n触发: inject\n"
               "动作: light.turn_on(light.x, brightness_pct=99999)\n")
        flow = compile_dsl(dsl, target="staging")
        issues = _r27(flow)
        self.assertTrue(issues, "brightness_pct=99999 应触发 R27")
        self.assertTrue(all(i["level"] == "warning" for i in issues))
        self.assertIn("brightness_pct", issues[0]["message"])

    def test_out_of_range_volume_level_warned(self):
        """volume_level=5 应落在 0-1 → R27 warning。"""
        dsl = ("场景: 音量越界\n触发: inject\n"
               "动作: media_player.volume_set(media_player.x, volume_level=5)\n")
        flow = compile_dsl(dsl, target="staging")
        issues = _r27(flow)
        self.assertTrue(issues, "volume_level=5 应触发 R27")
        self.assertIn("volume_level", issues[0]["message"])

    def test_in_range_no_warning(self):
        """brightness_pct=80（合法值域内）→ 不报 R27。"""
        dsl = ("场景: 合法\n触发: inject\n"
               "动作: light.turn_on(light.x, brightness_pct=80)\n")
        flow = compile_dsl(dsl, target="staging")
        self.assertEqual(_r27(flow), [], "合法 brightness_pct 不应误报 R27")

    def test_var_param_skipped(self):
        """变量引用参数走 jsonata → 不静态判定，跳过（不误报）。"""
        dsl = ("场景: 变量亮度\n触发: inject\n"
               "变量: 亮度=70\n"
               "动作: light.turn_on(light.x, brightness_pct=亮度)\n")
        flow = compile_dsl(dsl, target="staging")
        self.assertEqual(_r27(flow), [], "变量参数应跳过 R27（无法静态判定）")

    def test_r27_is_warning_not_error(self):
        """R27 为 warning 级，不进 error 集（不阻断合法流）。"""
        dsl = ("场景: 越界但合法\n触发: inject\n"
               "动作: light.turn_on(light.x, brightness_pct=99999)\n")
        flow = compile_dsl(dsl, target="staging")
        errs = [i for i in lint_flow(flow) if i.get("level") == "error"]
        self.assertFalse(any(i.get("rule") == "R27" for i in errs),
                          "R27 不应出现在 error 级，避免阻断部署")


if __name__ == "__main__":
    unittest.main()
