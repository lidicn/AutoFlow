#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB22 T1 / B1 回归：HA 服务「非法参数」静态校验（R_SERVICE_PARAM）。

精准 fail-open 黑名单：仅命中「已知非法 (service,param) 组合」才报 error；
未知 action / 未知 param / jsonata 动态参数一律跳过，绝不误伤合法写法。
该 rule 已加入 gateway._LINT_BLOCK_RULES，故黑箱 deploy_proposal 与白箱 deploy_raw
两条部署路径都会拦截（error 级 → 部署闸门硬拦）。
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


def _r_service_param(flow):
    return [i for i in lint_flow(flow) if i.get("rule") == "R_SERVICE_PARAM"]


class TestServiceParamLint(unittest.TestCase):
    def test_climate_turn_on_with_hvac_mode_blocked(self):
        """climate.turn_on 收 hvac_mode（WB22 T1 核心 FAIL）→ R_SERVICE_PARAM error。"""
        dsl = ("场景: 空调误用\n触发: inject\n"
               "动作: climate.turn_on(climate.study_ac, hvac_mode=cool)\n")
        flow = compile_dsl(dsl, target="staging")
        issues = _r_service_param(flow)
        self.assertTrue(issues, "climate.turn_on + hvac_mode 应触发 R_SERVICE_PARAM")
        self.assertEqual(issues[0]["level"], "error")
        self.assertIn("hvac_mode", issues[0]["message"])

    def test_climate_turn_on_with_temperature_blocked(self):
        """climate.turn_on 收 temperature（应走 set_temperature）→ error。"""
        dsl = ("场景: 空调误用2\n触发: inject\n"
               "动作: climate.turn_on(climate.study_ac, temperature=26)\n")
        flow = compile_dsl(dsl, target="staging")
        issues = _r_service_param(flow)
        self.assertTrue(issues, "climate.turn_on + temperature 应触发 R_SERVICE_PARAM")

    def test_climate_turn_on_no_param_clean(self):
        """climate.turn_on 不带温控参数（合法用法）→ 不报。"""
        dsl = ("场景: 合法开机\n触发: inject\n"
               "动作: climate.turn_on(climate.study_ac)\n")
        flow = compile_dsl(dsl, target="staging")
        self.assertEqual(_r_service_param(flow), [],
                         "合法的 climate.turn_on（无参数）不应误报 R_SERVICE_PARAM")

    def test_climate_set_hvac_mode_correct_service_clean(self):
        """正确的 service climate.set_hvac_mode + hvac_mode → 不报（错杀正确写法）。"""
        dsl = ("场景: 正确设温模\n触发: inject\n"
               "动作: climate.set_hvac_mode(climate.study_ac, hvac_mode=cool)\n")
        flow = compile_dsl(dsl, target="staging")
        self.assertEqual(_r_service_param(flow), [],
                         "climate.set_hvac_mode + hvac_mode 是正确 service，不应误报")

    def test_cover_open_with_position_blocked(self):
        """cover.open_cover 收 position（应走 set_cover_position）→ error。"""
        dsl = ("场景: 窗帘误用\n触发: inject\n"
               "动作: cover.open_cover(cover.curtain, position=50)\n")
        flow = compile_dsl(dsl, target="staging")
        issues = _r_service_param(flow)
        self.assertTrue(issues, "cover.open_cover + position 应触发 R_SERVICE_PARAM")

    def test_light_turn_on_unknown_param_not_blacklisted(self):
        """light.turn_on 不在黑名单 → 即使传任意 param 也不误报（fail-open）。"""
        dsl = ("场景: 灯\n触发: inject\n"
               "动作: light.turn_on(light.x, brightness_pct=80)\n")
        flow = compile_dsl(dsl, target="staging")
        self.assertEqual(_r_service_param(flow), [],
                         "light.turn_on 不在黑名单，不应误报 R_SERVICE_PARAM")

    def test_jsonata_param_skipped(self):
        """dataType=jsonata 的动态参数 → 无法静态判定，跳过（不误报）。"""
        # 手写一个 jsonata 参数的 api-call-service 节点
        flow = {"nodes": [{
            "id": "n1", "type": "api-call-service",
            "action": "climate.turn_on", "data": "hvac_mode", "dataType": "jsonata",
        }]}
        self.assertEqual(_r_service_param(flow), [],
                         "jsonata 动态参数应跳过 R_SERVICE_PARAM（无法静态判定）")

    def test_r_service_param_is_error_level(self):
        """R_SERVICE_PARAM 必须是 error 级，才能被部署闸门硬拦。"""
        dsl = ("场景: 验证err\n触发: inject\n"
               "动作: climate.turn_on(climate.study_ac, hvac_mode=cool)\n")
        flow = compile_dsl(dsl, target="staging")
        errs = [i for i in lint_flow(flow) if i.get("level") == "error"]
        self.assertTrue(any(i.get("rule") == "R_SERVICE_PARAM" for i in errs),
                         "R_SERVICE_PARAM 应出现在 error 级，确保部署闸门拦截")


if __name__ == "__main__":
    unittest.main(verbosity=2)
