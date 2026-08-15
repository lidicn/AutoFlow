"""gate_vhass_deepen 专项测试：G1 非 toggle 状态回填 / G2 重放归零 / G3 恒假分支不保守命中。

对应报告 A14（vhass 未建模服务不得伪造 state）、A15（JSONata 致重放归零 → 假过）、
A30 闸门侧（undefined-field 分支被保守视为命中 → 与编译器 R31 结论矛盾）。

设计要点：不依赖真实 NR/HA，也不依赖 device_catalog.json 的实时刷新——
G1 直接打 vhass store，G2/G3 直接打 `_vg_evaluate_active_intents` /
`Gateway.run_staging_gate(flow=...)` 白箱直通口，完全确定性。

运行：python tests/test_gate_vhass_deepen.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath("src"))
os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_gatedeep_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import vhass as V
from autoflow_gateway.flow_linter import (
    lint_flow, collect_undefined_field_refs, _collect_defined_fields,
)
from autoflow_gateway.gateway import (
    Gateway, _vg_evaluate_active_intents, _vg_dead_switch_rules,
    _vg_dead_branch_reach, _replay_zero_policy,
)


# ───────────────────────── 合成 flow 工具 ─────────────────────────
def _flow(field_declared, field_referenced, with_else=True, with_subflow=False):
    """触发 → 取值(声明 payload.<field_declared>) → switch(引用 <field_referenced>)
    → THEN: light.turn_on(light.desk) [+ 可选 link out]。

    注意：真实编译器产出的分支表达式带 `payload.` 限定（`$number(payload.光照) < 22`），
    合成 flow 必须照抄——写成裸 `光照` 时闸门的 `_vg_lookup` 查不到值，会退化成
    「无法本地求值 → 保守命中」，让正例测试变成假绿。
    field_referenced 传 `状态.光照` 这类未声明前缀即复刻 A30。
    """
    then_targets = ["svc1"]
    nodes_extra = []
    if with_subflow:
        then_targets.append("lo1")
        nodes_extra.append({"id": "lo1", "type": "link out", "name": "→ bark_push",
                            "links": [], "wires": []})
    rules = [{"t": "jsonata_exp", "v": f"$number({field_referenced}) < 22",
              "vt": "jsonata"}]
    wires = [then_targets]
    if with_else:
        rules.append({"t": "else", "v": "true", "vt": "jsonata"})
        wires.append([])
    return {"nodes": [
        {"id": "trg", "type": "server-state-changed",
         "entities": {"entity": ["binary_sensor.motion"], "substring": [], "regex": []},
         "ifState": "on", "wires": [["rd"]]},
        {"id": "rd", "type": "api-current-state", "entity_id": "sensor.lux",
         "halt_if": "", "outputs": 1,
         "outputProperties": [
             # 编译器真实产出的第一条：payload 容器归一（上游 inject/触发器的标量
             # payload 必须先变成 {}，否则后面 payload.<字段> 写入静默失败）。
             # 合成 flow 漏了它会让分支永远读不到字段 → 测试变成「保守命中」的假绿。
             {"property": "payload", "propertyType": "msg",
              "value": '$type(payload) = "object" ? payload : {}',
              "valueType": "jsonata"},
             {"property": f"payload.{field_declared}", "propertyType": "msg",
              "value": "", "valueType": "entityState"}],
         "wires": [["sw"]]},
        {"id": "sw", "type": "switch", "name": "分支", "property": "payload",
         "propertyType": "msg", "checkall": "true", "rules": rules,
         "outputs": len(rules), "wires": wires},
        {"id": "svc1", "type": "api-call-service", "domain": "light",
         "service": "turn_on", "entityId": ["light.desk"], "data": "{}",
         "wires": [[]]},
    ] + nodes_extra}


def _world(eid):
    return {"sensor.lux": "10", "binary_sensor.motion": "on"}.get(eid)


def _gw():
    return Gateway()


def _store():
    """最小 vhass store：只放测试用到的实体，避免依赖 staging 快照。"""
    st = V.VHassStore(state_path=os.path.join(_TMP, f"vh_{os.getpid()}.json"))
    st.entities = {
        "light.desk": {"entity_id": "light.desk", "state": "off", "attributes": {}},
        "sensor.lux": {"entity_id": "sensor.lux", "state": "10", "attributes": {}},
        "binary_sensor.motion": {"entity_id": "binary_sensor.motion",
                                 "state": "off", "attributes": {}},
        "climate.ac": {"entity_id": "climate.ac", "state": "off", "attributes": {}},
        "fan.living": {"entity_id": "fan.living", "state": "off", "attributes": {}},
    }
    st.persist = lambda: None          # 测试不落盘
    st.unmodeled_calls = []
    return st


# ═══════════════════ G1：vhass 非 toggle 服务状态回填 ═══════════════════
class TestG1NonToggleStateWriteback(unittest.TestCase):
    """A14 结案固化：非 toggle 服务【绝不】把 service 名当作实体新状态回填。

    分两类：
      · 已建模 → 写**真实语义终态**（climate 的 state 就是 hvac_mode）；
      · 未建模 / 建模但缺关键参数 → state 保持原样 + 留痕 + 登记 unmodeled_calls，
        闸门据此降级为「未验证」并显式 warn，而不是静默 pass。
    """

    def test_set_hvac_mode_writes_real_state_not_service_name(self):
        st = _store()
        st.apply_service("climate", "set_hvac_mode", {
            "entity_id": "climate.ac", "hvac_mode": "cool"})
        rec = st.get_state("climate.ac")
        self.assertNotEqual(rec["state"], "set_hvac_mode",
                            "service 名被伪造成 state（A14 复发）")
        self.assertEqual(rec["state"], "cool", "climate 的 state 应为 hvac_mode 真值")
        self.assertIsNone(rec["attributes"].get("_unmodeled_service"),
                          "已建模服务不应留未建模痕迹")

    def test_fan_set_percentage_is_attribute_only(self):
        """纯属性服务：state 不变、参数落 attribute，仍属已建模。"""
        st = _store()
        st.apply_service("fan", "set_percentage",
                         {"entity_id": "fan.living", "percentage": 60})
        rec = st.get_state("fan.living")
        self.assertNotEqual(rec["state"], "set_percentage")
        self.assertEqual(rec["state"], "off", "纯属性服务不得改 state")
        self.assertEqual(rec["attributes"].get("percentage"), 60)
        self.assertIsNone(rec["attributes"].get("_unmodeled_service"))

    def test_unknown_service_keeps_state_and_traces(self):
        """完全未建模的服务：state 原样 + 留痕 + 登记，供闸门降级。"""
        st = _store()
        st.apply_service("climate", "set_aux_heat",
                         {"entity_id": "climate.ac", "aux_heat": True})
        rec = st.get_state("climate.ac")
        self.assertNotEqual(rec["state"], "set_aux_heat",
                            "未建模服务名被伪造成 state（A14 复发）")
        self.assertEqual(rec["state"], "off", "未建模服务应保持原 state 不变")
        self.assertEqual(rec["attributes"].get("_unmodeled_service"),
                         "climate.set_aux_heat", "未建模服务须在属性留痕")
        self.assertIn("climate.set_aux_heat(climate.ac)", st.unmodeled_calls,
                      "未建模服务须登记 store.unmodeled_calls 供闸门降级")

    def test_modeled_but_missing_param_is_not_faked(self):
        """已建模但缺关键参数（set_hvac_mode 无 hvac_mode）→ 同样不许瞎写终态。"""
        st = _store()
        st.apply_service("climate", "set_hvac_mode", {"entity_id": "climate.ac"})
        rec = st.get_state("climate.ac")
        self.assertEqual(rec["state"], "off")
        self.assertEqual(rec["attributes"].get("_unmodeled_service"),
                         "climate.set_hvac_mode")

    def test_gate_warns_non_silently_on_unmodeled(self):
        """闸门层：有未建模服务时必须显式 warn + 归因，绝不静默 pass。"""
        st = _store()
        flow = {"nodes": [
            {"id": "trg", "type": "inject", "payload": "", "payloadType": "str",
             "wires": [["svc1"]]},
            {"id": "svc1", "type": "api-call-service", "domain": "climate",
             "service": "set_aux_heat", "entityId": ["climate.ac"],
             "data": '{"aux_heat":true}', "wires": [[]]},
        ]}
        r = _gw().run_staging_gate(
            "", [{"entity_id": "climate.ac", "state": "heat"}],
            vhass_store=st, flow=flow)
        self.assertTrue(any("未建模" in w for w in r.get("warnings", [])),
                        f"未建模服务必须显式告警，实得 warnings={r.get('warnings')}")
        self.assertFalse(r["passed"], "未建模 → 后置无法验证，不得报通过")
        fail = r["failures"][0]
        self.assertEqual(fail.get("unmodeled_service"), "climate.set_aux_heat",
                         "失败项须归因到未建模服务，而不是笼统『状态不对』")
        self.assertIn("未建模", fail.get("hint", ""))


# ═══════════════ G3：undefined-field 分支不得被保守视为命中 ═══════════════
class TestG3DeadBranchNotConservativelyHit(unittest.TestCase):

    def test_linter_indexes_undefined_rule(self):
        flow = _flow("光照", "状态.光照")
        idx = collect_undefined_field_refs(flow["nodes"])
        self.assertIn("sw", idx, "未声明字段的 switch 规则须被结构化索引")
        self.assertEqual(list(idx["sw"].keys()), [0], "应精确定位到第 0 条规则")
        self.assertIn("状态", idx["sw"][0])
        self.assertTrue([i for i in lint_flow(flow) if i.get("rule") == "R31"],
                        "同一 flow 必须同时触发 R31 告警（闸门/编译器结论同源）")

    def test_gate_does_not_activate_dead_branch(self):
        """核心：分支恒假 → THEN 体不得被激活（旧行为是保守命中 → 假过）。"""
        flow = _flow("光照", "状态.光照")
        warnings = []
        active = _vg_evaluate_active_intents(flow, _world, None, warnings)
        self.assertNotIn("svc1", active,
                         "恒假分支的 THEN 体被激活 → 与编译器 R31 结论矛盾（A30 复发）")
        self.assertTrue(any("恒假" in w for w in warnings),
                        f"须显式告警而非静默丢弃，实得 {warnings}")

    def test_correct_field_still_activates(self):
        """对照组：字段名写对时分支必须照常命中，不能误杀。"""
        flow = _flow("光照", "payload.光照")
        self.assertEqual(_vg_dead_switch_rules(flow), {},
                         "字段已声明 → 不应有恒假分支")
        active = _vg_evaluate_active_intents(flow, _world, None, [])
        self.assertIn("svc1", active, "正确条件被误杀（G3 过度收紧）")

    def test_unevaluable_but_declared_still_conservative(self):
        """只有『编译器判恒假』才不命中；纯运行期不可求值仍保守命中 + warn。"""
        flow = _flow("光照", "payload.光照")
        flow["nodes"][2]["rules"][0]["v"] = "$exists(光照) and $custom(光照)"
        warnings = []
        active = _vg_evaluate_active_intents(flow, _world, None, warnings)
        self.assertIn("svc1", active, "结构正确但复杂的 JSONata 不得被误杀")
        self.assertTrue(any("保守视为命中" in w for w in warnings))

    def test_dead_branch_downstream_targets_collected(self):
        flow = _flow("光照", "状态.光照", with_subflow=True)
        ents, subs = _vg_dead_branch_reach(flow, _vg_dead_switch_rules(flow))
        self.assertIn("light.desk", ents)
        self.assertTrue(any("bark" in s for s in subs))

    def test_gate_marks_expected_na_not_pass(self):
        """依赖恒假分支的 expected 明确标 N/A，且绝不算通过。"""
        flow = _flow("光照", "状态.光照")
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=flow)
        self.assertFalse(r["passed"], "恒假分支下的期望不得报通过")
        self.assertTrue(r.get("dead_branches"), "结果须带 dead_branches 归因")
        a = r["assertions"][0]
        self.assertTrue(a.get("na"), "该断言须标 N/A")
        self.assertIn("恒假分支", a.get("reason", ""))
        self.assertTrue(any(x.startswith("[N/A]") for x in r["reasons"]),
                        f"人读 reasons 须出现 [N/A] 标记：{r['reasons']}")

    def test_gate_marks_subflow_expected_na(self):
        flow = _flow("光照", "状态.光照", with_subflow=True)
        r = _gw().run_staging_gate(
            "", [{"subflow": "bark_push"}], vhass_store=_store(), flow=flow)
        self.assertFalse(r["passed"])
        a = r["assertions"][0]
        self.assertTrue(a.get("na"))
        self.assertIn("恒假分支", a.get("reason", ""))


# ═══════════════════ G2：JSONata 重放归零 → 不得静默通过 ═══════════════════
class TestG2ReplayZeroNoSilentPass(unittest.TestCase):

    def test_zero_replay_with_dead_branch_is_blocked(self):
        """恒假分支 + 无 else → 0 重放。expected 为空时旧实现会静默 pass。"""
        flow = _flow("光照", "状态.光照", with_else=False)
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [], "前提：本步确实 0 重放")
        self.assertTrue(r.get("replay_zero"), "须识别为重放归零")
        self.assertFalse(r["passed"],
                         "0 重放 = 什么都没验证，fail-closed 下不得报通过（A15）")
        self.assertTrue(any("重放归零" in w for w in r["warnings"]),
                        f"须显式告警，实得 {r['warnings']}")
        self.assertEqual(r["replay_zero_policy"], "fail_closed")

    def test_policy_hook_can_relax_to_warn_only(self):
        """对接 c4_replay_semantics 终裁的 hook：切 warn_only 保留放行但仍告警。"""
        flow = _flow("光照", "状态.光照", with_else=False)
        os.environ["AUTOFLOW_REPLAY_ZERO_POLICY"] = "warn_only"
        try:
            self.assertEqual(_replay_zero_policy(), "warn_only")
            r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
            self.assertTrue(r["passed"], "warn_only 策略下保留放行")
            self.assertTrue(any("重放归零" in w for w in r["warnings"]))
            self.assertTrue(any("未经行为验证" in w for w in r["warnings"]),
                            "放行也必须显式声明『未验证』，不得静默")
        finally:
            os.environ.pop("AUTOFLOW_REPLAY_ZERO_POLICY", None)
        self.assertEqual(_replay_zero_policy(), "fail_closed", "默认须回到 fail-closed")

    def test_normal_flow_not_flagged_as_replay_zero(self):
        """对照组：正常 flow 有重放 → 不得被归零逻辑误伤。"""
        flow = _flow("光照", "payload.光照")
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=flow)
        self.assertFalse(r.get("replay_zero"))
        self.assertTrue(r["passed"], f"正常条件流被误杀：{r['reasons']}")

    def test_condition_not_met_is_not_replay_zero(self):
        """对照组：条件**可求值且不成立**导致的 0 重放是正常语义，不算归零假过。"""
        flow = _flow("光照", "payload.光照")
        flow["nodes"][2]["rules"][0]["v"] = "$number(payload.光照) > 9999"
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [])
        self.assertFalse(r.get("replay_zero"),
                         "可求值的条件不成立 ≠ 闸门失能，不应触发 fail-closed")
        self.assertTrue(r["passed"])


# ═════════════ 回归护栏：R31 误报会放大成「正确分支被判死」 ═════════════
class TestR31FalsePositiveGuard(unittest.TestCase):

    def test_bare_msg_property_counts_as_declared(self):
        """取值写 msg.lux（不带 payload. 前缀）也是已声明，不得报 R31。"""
        nodes = [{"id": "rd", "type": "api-current-state",
                  "outputProperties": [{"property": "lux", "propertyType": "msg",
                                        "value": "", "valueType": "entityState"}]}]
        self.assertIn("lux", _collect_defined_fields(nodes))

    def test_change_msg_rule_counts_as_declared(self):
        nodes = [{"id": "c1", "type": "change",
                  "rules": [{"p": "payload.亮度", "pt": "msg", "to": "70"}]}]
        self.assertIn("亮度", _collect_defined_fields(nodes))


# ═════════════ W3 回归护栏：闸门诚实性（0 重放不得谎报 fully_verified） ═════════════
def _flow_typed_switch(switch_property, rule_t, rule_v, with_else=False):
    """在 _flow 骨架基础上，把 switch 换成类型化/引用未声明属性的规则，复刻 W3。"""
    f = _flow("光照", "payload.光照", with_else=with_else)
    sw = f["nodes"][2]
    sw["property"] = switch_property
    sw["propertyType"] = "msg"
    rules = [{"t": rule_t, "v": rule_v}]
    wires = [["svc1"]]
    if with_else:
        rules.append({"t": "else", "v": "true", "vt": "jsonata"})
        wires.append([])
    sw["rules"] = rules
    sw["outputs"] = len(rules)
    sw["wires"] = wires
    return f


class TestW3SwitchUnevaluableNoSilentPass(unittest.TestCase):

    def test_typed_lt_rule_is_blocked_not_fully_verified(self):
        """W3 case B：类型化 switch 规则（lt）闸门无法求值 → 0 重放不得报 fully_verified。"""
        flow = _flow_typed_switch("luminance", "lt", "22", with_else=False)
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [], "前提：本步确实 0 重放")
        self.assertTrue(r.get("replay_zero"), "须识别为重放归零（unevaluable 成因）")
        self.assertFalse(r["passed"],
                         "0 重放 + 无法求值 = 什么都没验证，fail-closed 不得报通过（W3）")
        self.assertFalse(r.get("fully_verified"),
                         "W3 核心危害：不得 0 重放却 fully_verified=True")
        self.assertEqual(r["verdict"], "拦截")
        self.assertTrue(
            any(("无法本地求值" in w or "不支持的 switch 规则类型" in w
                 or "未定义字段" in w or "恒假分支" in w or "重放归零" in w)
                for w in r["warnings"]),
            f"须显式告警，实得 {r['warnings']}")

    def test_eq_on_undeclared_property_is_blocked(self):
        """W3 case C：eq 引用未声明属性 → 0 重放不得报 fully_verified。"""
        flow = _flow_typed_switch("未声明字段", "eq", "22", with_else=False)
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [])
        self.assertTrue(r.get("replay_zero"))
        self.assertFalse(r["passed"])
        self.assertFalse(r.get("fully_verified"))
        self.assertTrue(
            any(("无法本地求值" in w or "未声明的属性" in w or "未定义字段" in w
                 or "恒假分支" in w or "重放归零" in w)
                for w in r["warnings"]),
            f"须显式告警，实得 {r['warnings']}")

    def test_known_false_jsonata_still_passes(self):
        """对照组：可求值的 JSONata 条件不成立 → 仍不触发归零（确认未误伤 A15 合法用例）。"""
        flow = _flow("光照", "payload.光照")
        flow["nodes"][2]["rules"][0]["v"] = "$number(payload.光照) > 9999"
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [])
        self.assertFalse(r.get("replay_zero"),
                         "可求值的条件不成立 ≠ 闸门失能，不应触发 fail-closed")
        self.assertTrue(r["passed"])


# ═══════════════════ W1：零出边节点（wires:[]）不得让闸崩溃 fail-open ═══════════════════
class TestW1ZeroOutEdgeNoFailOpen(unittest.TestCase):
    """白箱手写 flow 里一个尾节点写成 wires:[]（非规范 [[]]）时，
    _vg_evaluate_active_intents 的 outs[0] 下标越界会抛 IndexError → 被 verify_flow
    吞 → 闸 ran=false → A18 降级 warn（fail-open），幽灵实体反而拿到放行。
    修复后（gateway.py:880 `outs = out_wires.get(nid) or [[]]`）不再崩溃，幽灵实体被正常拦下。
    """

    @staticmethod
    def _ghost_flow(tail_wires):
        return {
            "id": "f", "label": "w1", "nodes": [
                {"id": "n1", "type": "inject", "z": "1",
                 "props": [{"p": "payload"}], "payload": "{}", "payloadType": "json",
                 "wires": [["n2"]]},
                {"id": "n2", "type": "api-call-service", "z": "1",
                 "domain": "light", "service": "turn_on",
                 "data": {"entity_id": "light.ghost_999_does_not_exist"},
                 "wires": [["n3"]]},
                {"id": "n3", "type": "debug", "z": "1", "wires": tail_wires},
            ],
        }

    def test_zero_outedge_debug_tail_does_not_fail_open_ghost(self):
        """W1a 复刻：尾节点 wires:[]（崩溃形状）→ 幽灵实体仍须被拦下（verdict=拦截）。"""
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=self._ghost_flow([]))
        self.assertNotEqual(r["verdict"], "warn",
                            "零出边节点不得让闸崩溃后 fail-open 降级 warn")
        self.assertEqual(r["verdict"], "拦截",
                         "幽灵实体必须被拦下（W1 核心安全断言）")
        self.assertFalse(r["passed"], "幽灵实体必须被拦下")

    def test_canonical_wires_still_blocks_ghost(self):
        """对照组：规范 wires:[[]] 形状行为不变，幽灵实体同样被拦（确认修复未误伤）。"""
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=self._ghost_flow([[]]))
        self.assertEqual(r["verdict"], "拦截")
        self.assertFalse(r["passed"])


# ═════════════ V-F1：保守命中 JSONata 不得谎报 fully_verified ═════════════
class TestVF1ConservativeNotFullyVerified(unittest.TestCase):
    """V-F1：复杂 JSONata 无法本地求值却『保守命中』→ 动作被重放但条件未经逻辑校验 →
    fully_verified 不得为 True（须降级『未充分验证』）。"""

    @staticmethod
    def _flow_conservative():
        rules = [{"t": "jsonata_exp", "v": "$exists(光照) and $custom(光照)",
                  "vt": "jsonata"}]
        return {"nodes": [
            {"id": "trg", "type": "server-state-changed",
             "entities": {"entity": ["binary_sensor.motion"], "substring": [], "regex": []},
             "ifState": "on", "wires": [["rd"]]},
            {"id": "rd", "type": "api-current-state", "entity_id": "sensor.lux",
             "halt_if": "", "outputs": 1,
             "outputProperties": [
                 {"property": "payload", "propertyType": "msg",
                  "value": '$type(payload) = "object" ? payload : {}', "valueType": "jsonata"},
                 {"property": "payload.光照", "propertyType": "msg",
                  "value": "", "valueType": "entityState"}],
             "wires": [["sw"]]},
            {"id": "sw", "type": "switch", "name": "分支", "property": "payload",
             "propertyType": "msg", "checkall": "true", "rules": rules,
             "outputs": len(rules), "wires": [["svc1"]]},
            {"id": "svc1", "type": "api-call-service", "domain": "light",
             "service": "turn_on", "entityId": ["light.desk"], "data": "{}",
             "wires": [[]]},
        ]}

    def test_conservative_hit_downgrades_fully_verified(self):
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=self._flow_conservative())
        self.assertIn("light.turn_on(light.desk)", r["replayed_services"],
                      "保守命中须仍走 THEN 体（结构正确流不误杀）")
        self.assertFalse(r.get("fully_verified"),
                         "V-F1 核心：条件未经校验的保守命中不得 fully_verified=True")
        self.assertEqual(r["verdict"], "未充分验证",
                         "须降级为『未充分验证』而非『放行』")
        self.assertTrue(
            any("保守" in w or "未充分验证" in w or "未覆盖层" in w
                for w in r["warnings"]),
            f"须显式告警，实得 {r['warnings']}")


# ═════════════ V-F3：尊重 checkall 语义（命中即停 vs 多输出） ═════════════
class TestVF3CheckallSemantics(unittest.TestCase):
    """V-F3：尊重节点 checkall 标志。checkall=false 时命中即停（只走第一条匹配分支），
    与真实 NR 语义对齐；默认/true 仍多输出（不变、零回归）。"""

    @staticmethod
    def _flow_multi(checkall):
        rules = [
            {"t": "eq", "v": "a", "vt": None},
            {"t": "neq", "v": "z", "vt": None},     # payload.mode="a" 时恒真 → 多输出
            {"t": "else", "v": "true", "vt": "jsonata"},
        ]
        return {"nodes": [
            {"id": "trg", "type": "inject", "props": [{"p": "payload"}],
             "payload": '{"mode": "a"}', "payloadType": "json", "wires": [["sw"]]},
            {"id": "sw", "type": "switch", "property": "payload.mode",
             "propertyType": "msg", "checkall": checkall, "rules": rules,
             "outputs": len(rules), "wires": [["svcA"], ["svcB"], []]},
            {"id": "svcA", "type": "api-call-service", "domain": "light",
             "service": "turn_on", "entityId": ["light.a"], "data": "{}", "wires": [[]]},
            {"id": "svcB", "type": "api-call-service", "domain": "light",
             "service": "turn_on", "entityId": ["light.b"], "data": "{}", "wires": [[]]},
        ]}

    def test_checkall_false_stops_at_first_match(self):
        active = _vg_evaluate_active_intents(self._flow_multi("false"), _world, None, [])
        self.assertIn("svcA", active, "第一条规则命中")
        self.assertNotIn("svcB", active, "checkall=false 须命中即停，不得激活第二条（V-F3）")

    def test_checkall_true_activates_all_matches(self):
        active = _vg_evaluate_active_intents(self._flow_multi("true"), _world, None, [])
        self.assertIn("svcA", active)
        self.assertIn("svcB", active, "checkall=true 须多输出（默认语义不变）")


# ═════════════ V-F4：function 黑箱副作用诚实降级 ═════════════
class TestVF4FunctionBlackboxHonesty(unittest.TestCase):
    """V-F4：function-only 流（黑箱副作用不可建模）且无显式 api-call-service 效果 →
    fully_verified 不得为 True（须降级『未充分验证』）。"""

    @staticmethod
    def _flow_function_only():
        return {"nodes": [
            {"id": "trg", "type": "inject", "props": [{"p": "payload"}],
             "payload": "{}", "payloadType": "json", "wires": [["fn"]]},
            {"id": "fn", "type": "function", "func": "msg.payload.x = 1; return msg;",
             "wires": [[]]},
        ]}

    def test_function_only_not_fully_verified(self):
        r = _gw().run_staging_gate(
            "", [], vhass_store=_store(), flow=self._flow_function_only())
        self.assertFalse(r.get("fully_verified"),
                         "V-F4：function 黑箱副作用不可建模，不得 fully_verified=True")
        self.assertEqual(r["verdict"], "未充分验证")
        self.assertTrue(any("function" in w for w in r["warnings"]))


# ═════════════ V-F5：change 喂未声明值 → switch 决定性假绿 ═════════════
class TestVF5ChangeUpstreamUnverified(unittest.TestCase):
    """V-F5 决定性假绿：change 从『未声明源』取值写入 payload.mode，下游 switch 读
    payload.mode 恒不命中 → 0 重放却因种子态满足后置条件而假绿。修复后 change 上游被
    回溯，payload.mode 不计入可靠字段 → switch 判定不可求值 → 重放归零 fail-closed 拦截。"""

    @staticmethod
    def _flow_change_feed(to_expr, tot):
        rules = [{"t": "eq", "v": "target", "vt": None}]
        return {"nodes": [
            {"id": "trg", "type": "inject", "props": [{"p": "payload"}],
             "payload": "{}", "payloadType": "json", "wires": [["ch"]]},
            {"id": "ch", "type": "change",
             "rules": [{"t": "set", "p": "payload.mode", "pt": "msg",
                        "to": to_expr, "tot": tot}],
             "wires": [["sw"]]},
            {"id": "sw", "type": "switch", "name": "分支", "property": "payload.mode",
             "propertyType": "msg", "checkall": "true", "rules": rules,
             "outputs": len(rules), "wires": [["svc1"]]},
            {"id": "svc1", "type": "api-call-service", "domain": "light",
             "service": "turn_on", "entityId": ["light.desk"], "data": "{}",
             "wires": [[]]},
        ]}

    def test_collect_flags_unreliable_change_source(self):
        idx = collect_undefined_field_refs(
            self._flow_change_feed("payload.undeclared_src", "msg")["nodes"])
        self.assertIn("sw", idx, "change 喂未声明值 → switch 须被判不可求值")
        self.assertTrue(idx["sw"], "须定位到具体规则")

    def test_change_fed_undeclared_is_blocked_not_false_green(self):
        flow = self._flow_change_feed("payload.undeclared_src", "msg")
        r = _gw().run_staging_gate("", [], vhass_store=_store(), flow=flow)
        self.assertEqual(r["replayed_services"], [], "前提：本步确实 0 重放")
        self.assertTrue(r.get("replay_zero"), "须识别为重放归零（change 上游未声明）")
        self.assertFalse(r["passed"], "0 重放 + 不可求值 = 什么都没验证，不得假绿（V-F5）")
        self.assertFalse(r.get("fully_verified"))
        self.assertEqual(r["verdict"], "拦截")

    def test_reliable_literal_source_still_activates(self):
        """对照组：change 从『字面量源』取值 → 字段可靠 → 正常命中，不误杀。"""
        flow = self._flow_change_feed("target", "str")
        idx = collect_undefined_field_refs(flow["nodes"])
        self.assertNotIn("sw", idx, "字面量源 → switch 不应被判不可求值")
        r = _gw().run_staging_gate(
            "", [{"entity_id": "light.desk", "state": "on"}],
            vhass_store=_store(), flow=flow)
        self.assertIn("light.turn_on(light.desk)", r["replayed_services"],
                      "可靠源 → 分支须照常命中并重放")


if __name__ == "__main__":
    unittest.main(verbosity=2)
