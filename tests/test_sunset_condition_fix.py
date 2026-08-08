"""#73 工单 sunset-condition-fix 回归：黑箱模式下「switch 直接读实体 ID 路径」静默失败。

覆盖：
- R34 命中：switch.property = `msg.sun.sun` / `msg.weather.xxx` / `msg.sensor.xxx`（首段为已知 HA 域）
  → error，提示改用 api-current-state + switch(payload)。
- R34 不命中：正确链路（api-current-state + switch(payload)）、合法 NR 根路径
  （payload.lux / data.state / 单段 payload）均不应误报。
- 节点级 property 与规则级 property 两种写法都要拦。
- 附带 skills/autoflow.md 含新规则文本 sanity grep（方案 A 落地证据）。
"""
import os
import re
import unittest

from autoflow_gateway.flow_linter import lint_flow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _switch_flow(property: str, property_type: str = "msg",
                 rule_property: str = None, rules=None) -> dict:
    """构造仅含一个 switch 的最小 flow。rule_property 非 None 时在规则级覆盖 property。"""
    if rules is None:
        rules = [{"t": "eq", "v": "below_horizon", "vt": "str"}]
    if rule_property is not None:
        rules = [dict(r, property=rule_property) for r in rules]
    node = {
        "id": "sw1", "type": "switch", "z": "flow1",
        "name": "日落检查", "property": property, "propertyType": property_type,
        "checkall": True, "outputs": len(rules), "rules": rules,
        "wires": [[ "n_out" ] for _ in rules],
    }
    return {"nodes": [node, {"id": "n_out", "type": "debug", "z": "flow1", "wires": []}]}


def _r34(flow: dict):
    return [i for i in lint_flow(flow) if i.get("rule") == "R34"]


class TestR34EntityPathInSwitch(unittest.TestCase):
    def test_buggy_sun_sun_node_level(self):
        """工单原始 bug：switch.property = msg.sun.sun → R34 error。"""
        issues = _r34(_switch_flow("sun.sun"))
        self.assertTrue(issues, "sun.sun 应触发 R34")
        self.assertEqual(issues[0]["level"], "error")
        self.assertIn("api-current-state", issues[0]["message"])
        self.assertIn("payload", issues[0]["message"])

    def test_buggy_weather_rule_level(self):
        """规则级 property = weather.weather_home（首段为 HA 域）→ R34。"""
        issues = _r34(_switch_flow("payload", rule_property="weather.weather_home"))
        self.assertTrue(issues, "weather.xxx 规则级应触发 R34")
        self.assertEqual(issues[0]["level"], "error")

    def test_buggy_sensor_path(self):
        """sensor.<id> 路径 → R34。"""
        issues = _r34(_switch_flow("sensor.living_room_temperature"))
        self.assertTrue(issues, "sensor.xxx 应触发 R34")

    def test_correct_api_current_state_then_payload(self):
        """正确链路：api-current-state 输出 msg.payload，switch 读 payload → 无 R34。"""
        flow = {
            "nodes": [
                {"id": "acs", "type": "api-current-state", "z": "flow1",
                 "entityId": "sun.sun", "wires": [["sw1"], ["sw1"]]},
                {"id": "sw1", "type": "switch", "z": "flow1",
                 "property": "payload", "propertyType": "msg",
                 "rules": [{"t": "eq", "v": "below_horizon", "vt": "str"}],
                 "wires": [["n_out"]]},
                {"id": "n_out", "type": "debug", "z": "flow1", "wires": []},
            ]
        }
        self.assertEqual(_r34(flow), [], "api-current-state + switch(payload) 不应报 R34")

    def test_legit_payload_subpath(self):
        """合法 NR 根路径 payload.lux（首段 payload 非 HA 域）→ 无 R34。"""
        self.assertEqual(_r34(_switch_flow("payload.lux")), [])

    def test_legit_data_state(self):
        """api-current-state 也会写 msg.data；data.state 路径 → 无 R34。"""
        self.assertEqual(_r34(_switch_flow("data.state")), [])

    def test_single_segment_payload(self):
        """单段 property=payload（无点）→ 无 R34。"""
        self.assertEqual(_r34(_switch_flow("payload")), [])

    def test_jsonata_property_not_flagged(self):
        """propertyType=jsonata 的 switch 走 R30 路径，不应被 R34 误伤。"""
        flow = _switch_flow("payload", property_type="jsonata",
                            rules=[{"t": "jsonata", "v": "sun.sun = \"below_horizon\""}])
        self.assertEqual(_r34(flow), [], "jsonata switch 不应触发 R34")

    def test_message_actionable(self):
        """R34 信息须同时点明「恒假静默失败」与修复方向，便于 agent 自纠。"""
        msg = _r34(_switch_flow("sun.sun"))[0]["message"]
        self.assertIn("undefined", msg)
        self.assertIn("api-current-state", msg)

    def test_exact_workorder_repro(self):
        """工单原文那份错误配置（节点级 + 规则级都写 sun.sun）→ 至少 1 条 R34 error。"""
        buggy = {
            "nodes": [{
                "id": "rw7e3fb9f120c9_001", "type": "switch", "name": "日落检查",
                "property": "sun.sun", "propertyType": "msg",
                "rules": [{"t": "eq", "v": "below_horizon", "vt": "str", "property": "sun.sun"}],
            }]
        }
        issues = _r34(buggy)
        self.assertTrue(issues, "工单原始配置必须触发 R34")
        self.assertEqual(issues[0]["level"], "error")


class TestSkillRulePresent(unittest.TestCase):
    def test_skill_contains_sunset_rule(self):
        """方案 A 落地证据：skills/autoflow.md 含禁止 msg.<实体ID> 的规则与 R34 引用。"""
        skill_path = os.path.join(REPO_ROOT, "skills", "autoflow.md")
        self.assertTrue(os.path.exists(skill_path), "skills/autoflow.md 缺失")
        text = open(skill_path, encoding="utf-8").read()
        self.assertIn("api-current-state", text)
        self.assertIn("R34", text)
        # 明确禁止 switch 读 msg.<实体ID>
        self.assertTrue(
            re.search(r"msg\.sun\.sun|msg\.<实体", text),
            "skill 应明确点名禁止 msg.sun.sun / msg.<实体ID> 这类 switch 读法",
        )


if __name__ == "__main__":
    unittest.main()
