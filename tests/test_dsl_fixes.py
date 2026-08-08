"""DSL 编译器 5 处真实 bug 修复的回归测试（WORKORDER_DEV_dsl_fixes）。

覆盖 B1 cron 时间范围 / B2 否则如果 前缀污染 / B3 变量作用域三重 /
B4 attribute=state 误杀 / B5 提取字段 R31 误报。

所有用例跑【产品真码】编译路径（dsl_engine.compile_dsl + flow_linter.lint_flow），
不做"把修复只写进测试生成器"的自欺。PM 复现脚本要点见工单交付段。
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import dsl_engine as d
from autoflow_gateway import flow_linter as fl


# 测试用的实体解析器（与 PM 复现脚本一致）
def _resolver(name):
    return name


def _attr_resolver(entity):
    return {"temperature", "hvac_mode"}


# 合法动作模板（domain.service(target) 形式）
_ACT = "light.turn_on(light.a)"


class _ResolverMixin:
    """在每个用例前后保存/恢复全局实体解析器，避免用例间相互污染。"""

    def setUp(self):
        self._saved_res = d._entity_resolver
        self._saved_attr = d._entity_attributes_resolver
        d.set_entity_resolver(_resolver)
        d.set_entity_attributes_resolver(_attr_resolver)

    def tearDown(self):
        d._entity_resolver = self._saved_res
        d._entity_attributes_resolver = self._saved_attr


# ───────────────────────── B1 cron 时间范围 ─────────────────────────
class TestB1CronRange(_ResolverMixin, unittest.TestCase):
    def test_invalid_times_raise(self):
        """B1：25:99 / 24:00 / 23:00-26:00 编译期抛 C_TIME_RANGE。"""
        for bad in ["定时 25:99", "定时 24:00", "定时 23:00-26:00"]:
            with self.subTest(bad=bad):
                with self.assertRaises(d.DSLError) as ctx:
                    d._parse_trigger(bad, 1)
                self.assertEqual(ctx.exception.code, "C_TIME_RANGE")

    def test_valid_times_crontab(self):
        """B1：07:30 正常，crontab='30 7 * * *'。"""
        tr = d._parse_trigger("定时 07:30", 1)
        self.assertEqual(tr.cron, "30 7 * * *")

    def test_cross_midnight_range(self):
        """B1：23:00-02:00 跨夜范围接受，crontab='0-0 23-2 * * *'（复用既有 range 生成风格）。"""
        tr = d._parse_trigger("定时 23:00-02:00", 1)
        self.assertEqual(tr.cron, "0-0 23-2 * * *")

    def test_valid_time_full_compile(self):
        """B1 集成：合法定时整条编译不抛错，inject 节点 crontab 正确。"""
        flow = d.compile_dsl(
            "场景: 晨\n触发: 定时 07:30\n动作: light.turn_on(light.a)\n"
        )
        inj = [n for n in flow["nodes"] if n.get("type") == "inject"]
        self.assertTrue(inj, "应存在 inject 节点")
        self.assertEqual(inj[0].get("crontab"), "30 7 * * *")

    def test_invalid_time_full_compile_raises(self):
        """B1 集成：非法定时整条编译抛 C_TIME_RANGE（带行号）。"""
        with self.assertRaises(d.DSLError) as ctx:
            d.compile_dsl("场景: x\n触发: 定时 25:99\n动作: light.turn_on(light.a)\n")
        self.assertEqual(ctx.exception.code, "C_TIME_RANGE")


# ───────────────────────── B2 否则如果 前缀 ─────────────────────────
class TestB2ElifPrefix(_ResolverMixin, unittest.TestCase):
    def test_elif_no_prefix_leak(self):
        """B2：分支/否则如果/否则 → 3 条规则无 '如果:' 前缀污染。"""
        flow = d.compile_dsl(
            "场景: 温控\n触发: 注入\n"
            "分支: $number(温度)>30\n  动作: light.turn_on(light.a)\n"
            "否则如果: $number(温度)<20\n  动作: light.turn_off(light.a)\n"
            "否则:\n  动作: light.toggle(light.a)\n"
        )
        sw = [n for n in flow["nodes"] if n.get("type") == "switch"]
        self.assertEqual(len(sw), 1)
        rules = sw[0]["rules"]
        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[0]["v"], "$number(温度)>30")
        self.assertEqual(rules[1]["v"], "$number(温度)<20")
        self.assertEqual(rules[2]["t"], "else")
        for r in rules:
            self.assertNotIn("如果:", r.get("v") or "")


# ───────────────────────── B3 变量作用域三重 ─────────────────────────
class TestB3VarScope(_ResolverMixin, unittest.TestCase):
    def test_b3a_dash_var_rejected(self):
        """B3(a)：变量名含破折号编译期抛 C_VAR_NAME。"""
        with self.assertRaises(d.DSLError) as ctx:
            d.compile_dsl(
                "场景: x\n触发: 注入\n"
                "变量: 奇怪名-带破折号=5\n"
                "条件: $number(奇怪名-带破折号)>0\n  动作: light.turn_on(light.a)\n"
            )
        self.assertEqual(ctx.exception.code, "C_VAR_NAME")

    def test_b3b_condition_binds_flow_var(self):
        """B3(b)：条件 JSONata 里的裸变量名绑定到 flow 上下文。"""
        flow = d.compile_dsl(
            "场景: 阈\n触发: 注入\n"
            "变量: 阈值_上限=5\n"
            "条件: $number(阈值_上限)>0\n  动作: light.turn_on(light.a)\n"
        )
        sw = [n for n in flow["nodes"] if n.get("type") == "switch"]
        got = None
        for s in sw:
            for r in s.get("rules", []):
                if r.get("vt") == "jsonata":
                    got = r.get("v")
        self.assertIsNotNone(got)
        self.assertIn("flow.阈值_上限", got)

    def test_b3b_no_r31_for_defined_var(self):
        """B3(b)+B3(c)：绑定后不触发 R31 误报。"""
        flow = d.compile_dsl(
            "场景: 阈\n触发: 注入\n"
            "变量: 阈值_上限=5\n"
            "条件: $number(阈值_上限)>0\n  动作: light.turn_on(light.a)\n"
        )
        issues = fl.lint_flow(flow)
        r31 = [i for i in issues if i.get("rule") == "R31"]
        self.assertEqual(len(r31), 0, f"不应有 R31 误报，实际：{r31}")


# ───────────────────────── B4 attribute=state ─────────────────────────
class TestB4AttrState(_ResolverMixin, unittest.TestCase):
    def test_state_accepted(self):
        """B4：attribute=state 编译期接受（实体主状态恒有效）。"""
        # 不抛即过
        d._validate_subflow_attribute(
            "history_state_at", {"entity": "climate.x", "attribute": "state"}, 1
        )

    def test_typo_rejected_with_resolver(self):
        """B4：拼写错属性（解析器已知属性集）仍拒。"""
        with self.assertRaises(d.DSLError) as ctx:
            d._validate_subflow_attribute(
                "history_state_at", {"entity": "climate.x", "attribute": "temprature"}, 1
            )
        self.assertEqual(ctx.exception.code, "C_SUBFLOW_ATTR_UNKNOWN")


# ───────────────────────── B5 提取字段 R31 ─────────────────────────
class TestB5ExtractField(_ResolverMixin, unittest.TestCase):
    def test_extract_field_no_r31(self):
        """B5：提取 字段在 分支 中引用，lint 无 R31。"""
        flow = d.compile_dsl(
            "场景: 耗电\n触发: 注入\n"
            "提取: 耗电=payload.value\n"
            "分支: $number(耗电)>8\n  动作: light.turn_on(light.a)\n"
        )
        issues = fl.lint_flow(flow)
        r31 = [i for i in issues if i.get("rule") == "R31"]
        self.assertEqual(len(r31), 0, f"不应有 R31 误报，实际：{r31}")

    def test_undefined_field_still_flagged(self):
        """B5 回归护栏：未声明字段仍触发 R31（证明不是把 R31 关掉了）。"""
        flow = d.compile_dsl(
            "场景: neg\n触发: 注入\n"
            "分支: $number(未定义字段)>8\n  动作: light.turn_on(light.a)\n"
        )
        issues = fl.lint_flow(flow)
        r31 = [i for i in issues if i.get("rule") == "R31"]
        self.assertEqual(len(r31), 1, "未定义字段应恰好 1 条 R31")


if __name__ == "__main__":
    unittest.main(verbosity=2)
