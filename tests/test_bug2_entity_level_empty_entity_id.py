#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bug2 回归：实体级 api-call-service 空 entityId 必须硬拦（R20 区分实体级/域级）。

旧实现 api-call-service 缺实体只报 warning，导致实体级服务（light/switch/...）
漏填 entityId 也能过 lint 部署成坏流、运行态变红。

修复：R20 区分实体级/域级——
  - 实体级（light/switch/cover/climate/... 及 homeassistant.turn_on 等）→ 空 entityId 升为 error（硬拦）
  - 域级豁免：notify（目标在 params，entityId 恒空是设计内）、homeassistant 域级
    restart/reload/check_config 等无需实体
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow


def _r20_errors(node):
    flow = {"nodes": [node]}
    return [i for i in lint_flow(flow)
            if i.get("rule") == "R20" and i.get("level") == "error"]


class TestBug2EntityLevelEmptyEntityId(unittest.TestCase):
    def test_entity_level_empty_blocked(self):
        """light.turn_on 空 entityId → R20 error（硬拦）。"""
        node = {"id": "a", "type": "api-call-service",
                "domain": "light", "service": "turn_on", "entityId": []}
        errs = _r20_errors(node)
        self.assertTrue(errs, "实体级服务空 entityId 必须 R20 error 硬拦")

    def test_homeassistant_turn_on_empty_blocked(self):
        """homeassistant.turn_on 空 entityId（实体级）→ R20 error。"""
        node = {"id": "e", "type": "api-call-service",
                "domain": "homeassistant", "service": "turn_on", "entityId": []}
        self.assertTrue(_r20_errors(node), "homeassistant.turn_on 缺实体须硬拦")

    def test_notify_empty_exempt(self):
        """notify 空 entityId 是设计内（目标在 params）→ 豁免。"""
        node = {"id": "b", "type": "api-call-service",
                "domain": "notify", "service": "mobile_app", "entityId": []}
        self.assertFalse(_r20_errors(node), "notify 空 entityId 应豁免，不误伤")

    def test_ha_reload_empty_exempt(self):
        """homeassistant.reload_* 域级无需实体 → 豁免。"""
        node = {"id": "c", "type": "api-call-service",
                "domain": "homeassistant", "service": "reload_core_config", "entityId": []}
        self.assertFalse(_r20_errors(node), "homeassistant.reload_* 应豁免")

    def test_ha_restart_empty_exempt(self):
        """homeassistant.restart 域级无需实体 → 豁免。"""
        node = {"id": "d", "type": "api-call-service",
                "domain": "homeassistant", "service": "restart", "entityId": []}
        self.assertFalse(_r20_errors(node), "homeassistant.restart 应豁免")

    def test_current_state_empty_still_error(self):
        """api-current-state 空 entityId → 仍 error（既有行为不变）。"""
        node = {"id": "f", "type": "api-current-state", "entityId": "", "halt_if": ""}
        self.assertTrue(_r20_errors(node), "api-current-state 空实体须 error")

    def test_valid_entity_passes(self):
        """实体级服务带有效 entityId → 无 R20 error。"""
        node = {"id": "g", "type": "api-call-service",
                "domain": "switch", "service": "turn_on", "entityId": ["switch.x"]}
        self.assertFalse(_r20_errors(node), "带有效 entityId 不应报 R20")


if __name__ == "__main__":
    unittest.main()
