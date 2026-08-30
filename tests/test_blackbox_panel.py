"""blackbox_panel 专项测试：A29 area 过滤透明化 / A30 conditional_brightness 模板字段 / A31 get_flow 提案回查。

运行：python tests/test_blackbox_panel.py
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath("src"))
os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_blackbox_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import dsl_engine as E
from autoflow_gateway.flow_linter import lint_flow
from autoflow_gateway.gateway import Gateway


class TestP2ConditionalBrightnessTemplate(unittest.TestCase):
    """A30：官方模板 conditional_brightness 此前引用未声明的 `状态.光照` → 分支恒假。
    修复后：加 `取值: {{lux}} 光照` 绑定照度传感器状态，分支改引用 `光照`。"""

    def _render(self, subs):
        raw = open("src/autoflow_gateway/templates/conditional_brightness.md",
                   encoding="utf-8").read()
        parts = raw.split("---", 2)
        body = parts[2] if len(parts) == 3 else raw

        def repl(m):
            name = m.group(1).strip()
            default = m.group(2)
            return str(subs.get(name, default or ""))

        return re.sub(r"\{\{(\w+)(?:\|([^}]*))?\}\}", repl, body)

    def test_template_compiles_without_r31_undefined_field(self):
        subs = {
            "room": "书房", "sensor": "binary_sensor.motion", "light": "light.desk",
            "lux": "sensor.lux_illuminance", "day_brightness": "100",
            "night_brightness": "30", "night_start": "22",
        }
        dsl = self._render(subs)
        scene = E.parse(dsl)
        flow = E.compile(scene)
        r31 = [i for i in lint_flow(flow) if i.get("rule") == "R31"]
        self.assertEqual(len(r31), 0, f"R31 未定义字段告警应清零: {r31}")
        sw = [n for n in flow["nodes"] if n.get("type") == "switch"]
        self.assertTrue(sw, "应有分支 switch 节点")
        # WB90 F11(89a616a) 后落点改为 msg 根：分支引用的是经 `取值:` 桥接的
        # msg.光照（而非未定义的 状态.光照、也不是 F11 前的 payload.光照）
        rule_vals = " ".join(str(r.get("v", "")) for r in sw[0].get("rules", []))
        self.assertIn("msg.光照", rule_vals)
        self.assertNotIn("payload.光照", rule_vals)

    def test_old_template_would_have_failed(self):
        """回归护栏：若有人把分支改回 `状态.光照`，编译器必须报未定义标签。
        WB85 F5a 起为编译期 fail-closed（DSLError/C_LABEL_UNDEFINED），
        强度高于旧 R31 lint 告警——guard 意图不变，断言随契约升级。"""
        with self.assertRaises(E.DSLError) as ctx:
            E.compile(E.parse("""场景: x
触发: binary_sensor.motion 有人
变量: night_start = 22
分支: 状态.光照 < night_start
  动作: light.turn_on(light.x, brightness=100)
否则
  动作: light.turn_on(light.x, brightness=30)
"""))
        self.assertEqual(ctx.exception.code, "C_LABEL_UNDEFINED")


class TestP3GetFlowProposal(unittest.TestCase):
    """A31：propose_dsl 返回的 af_scene_* 是编译提案逻辑 id，未部署到 NR；
    get_flow 应明确识别为提案而非裸 404。"""

    def test_proposal_id_returns_structured_hint(self):
        gw = Gateway()
        res = gw.get_flow("af_scene_my_scene")
        self.assertFalse(res.get("ok"))
        self.assertTrue(res.get("proposal"), "af_scene_* 应识别为提案 id")
        self.assertIn("hint", res)
        self.assertIn("proposal", res)


class TestP1AreaWarning(unittest.TestCase):
    """A29：area 传入但区域注册表不可解析时，不得静默全量返回，
    响应须带 area_warning 明确告知过滤未生效。"""

    def test_resolve_area_unknown_returns_hint(self):
        gw = Gateway()
        target, hint = gw._resolve_area("书房")
        self.assertIsNone(target)
        self.assertIsNotNone(hint, "未识别房间应返回 hint")

    def test_list_entities_area_warning_when_unresolved(self):
        gw = Gateway()
        ents = {"light.a": {"friendly_name": "书房灯", "domain": "light",
                           "area": "", "state": "off"}}
        gw.state.get_device_catalog = lambda: {"entities": ents, "freshness": "now"}
        gw.state.get_room_aliases = lambda: {}
        gw.state.get_area_index = lambda: {}
        res = gw.list_entities(area="书房")
        self.assertIn("area_warning", res)
        self.assertIsNotNone(res["area_warning"],
                             "未识别区域应给出 area_warning，而非假装过滤生效")

    def test_resolve_entity_area_warning_when_unresolved(self):
        gw = Gateway()
        ents = {"light.a": {"friendly_name": "书房灯", "domain": "light",
                           "area": "", "state": "off"}}
        gw.state.get_device_catalog = lambda: {"entities": ents, "freshness": "now"}
        gw.state.get_room_aliases = lambda: {}
        gw.state.get_area_index = lambda: {}
        res = gw.resolve_entity("书房灯", area="书房")
        self.assertIn("area_warning", res)
        self.assertIsNotNone(res["area_warning"])


if __name__ == "__main__":
    unittest.main()
