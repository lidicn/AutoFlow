#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defense.py 离线单元测试 —— 防御层（防 agent 抽风）守卫。

覆盖：
  - 受保护流判定（label 子串 / id 前缀）
  - 域分级（高危 / 安全 / 未知默认中风险）
  - 操作风险综合（删除升级 + 高危域 → 最高）
  - 写前检查 check_write：受保护 / 所有权 / 爆炸半径（正向放行 + 三类拒绝）
  - forbid_whole_replace 占位守卫（结构上网关绝不暴露 replace-all）

全程离线：DefenseLayer 仅依赖 config，不触网、不触 NR/HA。
（注：test_gateway.py 已有部分 defense 覆盖；本文件做更细的回归护栏，
 并显式验证「受保护集合/域白名单来自 config，可随配置变化」这一契约。）
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import autoflow_gateway.config as cfgmod
from autoflow_gateway.defense import DefenseLayer, DefenseError


class TestIsProtectedFlow(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.d = DefenseLayer(cfgmod.get_config())

    def test_protected_by_label_substring(self):
        # 默认 protected_flow_labels = {core, system, AutoFlow}
        self.assertTrue(self.d.is_protected_flow("x", "Core Lighting"))
        self.assertTrue(self.d.is_protected_flow("x", "my SYSTEM task"))
        self.assertTrue(self.d.is_protected_flow("x", "AutoFlow internal"))

    def test_protected_by_id_prefix(self):
        # 默认 protected_flow_id_prefixes = {core_}
        self.assertTrue(self.d.is_protected_flow("core_backup_flow", ""))

    def test_not_protected(self):
        self.assertFalse(self.d.is_protected_flow("user_flow_1", "书房夜灯"))
        self.assertFalse(self.d.is_protected_flow("abc", "随便一个流"))

    def test_protected_set_is_config_driven(self):
        # 改写 config 后行为随之变化（证明守卫读 config，而非写死常量）
        cfg = cfgmod.get_config()
        cfg.protected_flow_labels = {"secret"}
        cfg.protected_flow_id_prefixes = {"priv_"}
        d2 = DefenseLayer(cfg)
        self.assertFalse(d2.is_protected_flow("x", "Core Lighting"))  # 旧集合失效
        self.assertTrue(d2.is_protected_flow("x", "my secret flow"))
        self.assertTrue(d2.is_protected_flow("priv_x", ""))


class TestDomainRisk(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.d = DefenseLayer(cfgmod.get_config())

    def test_elevated_domains(self):
        # 默认 elevated = lock,valve,water_heater,alarm_control_panel,garage_door
        self.assertEqual(self.d.classify_domain_risk("lock"), "high")
        self.assertEqual(self.d.classify_domain_risk("valve"), "high")
        self.assertEqual(self.d.classify_domain_risk("garage_door"), "high")

    def test_safe_domains(self):
        # 默认 safe = light,switch,script,scene,notify,input_*,automation,cover,fan,climate
        self.assertEqual(self.d.classify_domain_risk("light"), "low")
        self.assertEqual(self.d.classify_domain_risk("switch"), "low")
        self.assertEqual(self.d.classify_domain_risk("climate"), "low")

    def test_unknown_domain_defaults_medium(self):
        self.assertEqual(self.d.classify_domain_risk("unknown_thing"), "medium")

    def test_classify_operation_risk_combines(self):
        # delete_flow 自身升到 medium；叠加高危域 → high
        self.assertEqual(self.d.classify_operation_risk("update_flow", ["light"]), "low")
        self.assertEqual(
            self.d.classify_operation_risk("delete_flow", ["light"]), "medium")
        self.assertEqual(
            self.d.classify_operation_risk("update_flow", ["lock"]), "high")
        self.assertEqual(
            self.d.classify_operation_risk("delete_flow", ["lock"]), "high")
        # 未知域 + 普通写 → medium
        self.assertEqual(
            self.d.classify_operation_risk("update_flow", ["weird"]), "medium")


class TestCheckWrite(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.d = DefenseLayer(cfgmod.get_config())

    def test_pass_when_own_and_small_radius(self):
        # 非受保护 + 自己拥有 + 半径内 → 不抛
        self.d.check_write(
            operation="update_flow", flow_id="f1", label="mine",
            owner_agent="agent_A", acting_agent="agent_A", flows_touched=1)

    def test_pass_system_owner_allowed(self):
        # owner=system 视为共享，允许
        self.d.check_write(
            operation="update_flow", flow_id="f1", label="mine",
            owner_agent="system", acting_agent="agent_A", flows_touched=1)

    def test_reject_protected_label(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(operation="update_flow", flow_id="x", label="core_x")

    def test_reject_protected_id_prefix(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(operation="delete_flow", flow_id="core_x", label="")

    def test_reject_other_owner(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(
                operation="update_flow", flow_id="f1", label="mine",
                owner_agent="agent_B", acting_agent="agent_A")

    def test_reject_blast_radius(self):
        # 默认 blast_radius_max_flows = 1
        with self.assertRaises(DefenseError):
            self.d.check_write(
                operation="update_flow", flow_id="f", label="x", flows_touched=5)

    def test_blast_radius_boundary_ok(self):
        # 恰好等于上限（1）应放行
        self.d.check_write(
            operation="update_flow", flow_id="f", label="x", flows_touched=1)

    def test_blast_radius_config_driven(self):
        cfg = cfgmod.get_config()
        cfg.blast_radius_max_flows = 3
        d2 = DefenseLayer(cfg)
        # 3 个 flow 在边界内放行；5 个仍拒
        d2.check_write(operation="update_flow", flow_id="f", label="x", flows_touched=3)
        with self.assertRaises(DefenseError):
            d2.check_write(operation="update_flow", flow_id="f", label="x", flows_touched=5)


class TestForbidWholeReplace(unittest.TestCase):
    def test_static_guard_returns_true(self):
        # 文档式守卫入口：提醒调用方网关任何写路径都不得 deploy_all/replace_all
        self.assertTrue(DefenseLayer.forbid_whole_replace())

    def test_defense_layer_exposes_no_replace_all(self):
        # 结构性保证：DefenseLayer 本身不提供整体替换原语
        self.assertFalse(hasattr(DefenseLayer, "replace_all"))
        self.assertFalse(hasattr(DefenseLayer, "deploy_all"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
