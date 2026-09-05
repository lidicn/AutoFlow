# -*- coding: utf-8 -*-
"""闸门失真（gate integrity）回归套件 —— A12 / A14 / A15 / A18 / A22。

工单背景：网关的「质量闸」在多条路径上**报 pass 但其实什么都没验证**。
本套件为每个修复点各配独立回归，喂进「会触发该 skip / false-pass 的 flow」，
断言 verdict 不再 blindly pass。

刻意的测试纪律（工单验收要求）：
  · 断言对象是**真实产品代码**（gateway.run_staging_gate / Gateway._build_unified_gate /
    Gateway.verify_flow / vhass.VHassStore），不使用编译器自指生成器当基线；
  · 主体用**手写 NR 节点 JSON** 直驱闸门（白箱直通口 flow=），
    只有 A15 需要证明「编译产物 ↔ 重放器」对齐时才过一次 compile_dsl，
    且断言的是物理语义（15<20 该开、25<20 该关），不是「编译器说什么就是什么」。

跑法：python -m pytest tests/test_gate_integrity.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_gateint_")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", _TMP)

import pytest  # noqa: E402

from autoflow_gateway import gateway as G  # noqa: E402
from autoflow_gateway import vhass as VH  # noqa: E402
from autoflow_gateway.gateway import Gateway  # noqa: E402

GW = Gateway()

CANARY_SKIPPED = {"ok": True, "source": "skipped"}


@pytest.fixture
def gate(monkeypatch):
    """闸门实例：禁用「节点注册表闸」。

    该闸会去连**真实 NR** 取已装节点类型；开发机连得上、CI 连不上，
    于是同一份用例结果随环境漂移（本机把手写的 `subflow:sf_bark` 判成未注册直接拦死，
    根本走不到断言逻辑）。本套件测的是闸门**断言逻辑**，与目标 NR 装了什么无关，故禁用。
    """
    monkeypatch.setattr(GW, "_gate_node_types", lambda flow: None)
    return GW


def _store(*rows):
    """rows: (entity_id, friendly_name, area, state, attrs)"""
    s = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    s.areas = seed["areas"]
    s.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
    return s


def _inject(nid, wires, payload="", ptype="str"):
    return {"id": nid, "type": "inject", "payload": payload,
            "payloadType": ptype, "wires": [list(wires)]}


def _call(nid, domain, service, entity, wires=(), data=None):
    n = {"id": nid, "type": "api-call-service", "domain": domain,
         "service": service, "entityId": entity, "wires": [list(wires)]}
    if data:
        n["data"] = data
    return n


# ════════════════════════════════════════════════════════════════════
# A12 · 子流程断言安慰剂 → 真可达性比对 + fail-closed
# ════════════════════════════════════════════════════════════════════
# 旧行为：expected 里的 {"subflow": X} 走的是读 entity_id/state 的分支，
# 取到 (None, None) → ok = (None == None) = True → **任何**子流程期望恒 pass，
# 哪怕 flow 里根本没这个子流程。这是纯安慰剂。

def _flow_no_subflow():
    return {"id": "a12_none", "label": "a12-none", "nodes": [
        _inject("t", ["c"]),
        _call("c", "light", "turn_on", "light.a12", []),
    ]}


def _flow_subflow_behind_dead_branch():
    """子流程挂在**永不命中**的分支上：flow 里有它，但本步世界态下不可达。"""
    return {"id": "a12_dead", "label": "a12-dead", "nodes": [
        _inject("t", ["sw"], payload="off"),
        {"id": "sw", "type": "switch", "property": "payload", "propertyType": "msg",
         "rules": [{"t": "eq", "v": "on", "vt": "str"}], "wires": [["sub"]]},
        # NR 5.x 子流程**实例**的 type 带前缀：subflow:<subflow_id>
        {"id": "sub", "type": "subflow:sf_bark", "name": "bark_push", "wires": [[]]},
    ]}


def _flow_subflow_reachable():
    f = _flow_subflow_behind_dead_branch()
    f["id"] = "a12_live"
    f["nodes"][0] = _inject("t", ["sw"], payload="on")   # 让分支命中
    return f


def test_a12_subflow_assert_fails_when_flow_has_no_such_subflow(gate):
    """flow 里压根没有该子流程 → 必须拦截，并说清「没有任何匹配」。"""
    g = gate.run_staging_gate("", [{"subflow": "bark_push"}],
                              vhass_store=_store(("light.a12", "灯", "书房", "off", {})),
                              flow=_flow_no_subflow())
    assert g["passed"] is False, f"子流程断言必须真验证，不能恒 pass：{g}"
    a = [x for x in g["assertions"] if x.get("kind") == "subflow"]
    assert a and a[0]["ok"] is False
    assert "没有任何" in a[0]["reason"], a[0]


def test_a12_subflow_assert_fails_on_dead_branch(gate):
    """flow 里有该子流程但挂死分支 → 拦截，且归因为「不可达」而非「不存在」。"""
    g = gate.run_staging_gate("", [{"subflow": "bark_push"}],
                              vhass_store=_store(), flow=_flow_subflow_behind_dead_branch())
    assert g["passed"] is False, g
    a = [x for x in g["assertions"] if x.get("kind") == "subflow"][0]
    assert a["ok"] is False
    assert "不可达" in a["reason"], f"死分支应归因为不可达，实得：{a['reason']}"


def test_a12_subflow_assert_passes_when_reachable(gate):
    """对照组：分支命中、子流程真被调到 → 放行（证明不是一刀切拦死）。"""
    g = gate.run_staging_gate("", [{"subflow": "bark_push"}],
                              vhass_store=_store(), flow=_flow_subflow_reachable())
    assert g["passed"] is True, g
    assert "bark_push" in g["external_calls"], g


def test_a12_unknown_expectation_kind_fails_closed(gate):
    """认不出的期望项必须 fail-closed，绝不静默放行。"""
    g = gate.run_staging_gate("", [{"随便写的": "东西"}],
                              vhass_store=_store(), flow=_flow_no_subflow())
    assert g["passed"] is False, g
    a = [x for x in g["assertions"] if x.get("kind") == "unknown"]
    assert a and "无法识别" in a[0]["reason"], g


def test_a12_compiled_subflow_instance_type_recognized():
    """根因单测：编译产物的子流程实例 type 是 `subflow:<id>`，识别函数必须认。"""
    assert G._vg_is_external_call("subflow:abc123") is True
    assert G._vg_is_external_call("link out") is True
    assert G._vg_is_external_call("subflow") is True
    assert G._vg_is_external_call("api-call-service") is False
    assert G._vg_is_external_call(None) is False


# ════════════════════════════════════════════════════════════════════
# A14 · vhass 非 toggle 服务的状态回读
# ════════════════════════════════════════════════════════════════════
# 旧行为：未映射的服务直接把**服务名**写成 state（state="set_hvac_mode"），
# 于是后置断言比的是一个假状态，闸门看起来「验过了」。

def test_a14_state_from_data_service_writes_real_state():
    s = _store(("climate.a14", "空调", "客厅", "off", {}))
    s.apply_service("climate", "set_hvac_mode", {"entity_id": "climate.a14", "hvac_mode": "cool"})
    assert s.get_state("climate.a14")["state"] == "cool"


def test_a14_modeled_media_service_uses_fixed_state():
    """已建模的固定终态服务：play_media → playing（不是服务名字面量）。"""
    s = _store(("media_player.a14", "音箱", "客厅", "idle", {}))
    s.apply_service("media_player", "play_media",
                    {"entity_id": "media_player.a14", "media_content_id": "x"})
    assert s.get_state("media_player.a14")["state"] == "playing"


def test_a14_unmapped_service_never_forges_state():
    """未建模服务：状态**保持不变** + 留痕，绝不把服务名当状态写进去。"""
    s = _store(("remote.a14", "遥控", "客厅", "on", {}))
    s.apply_service("remote", "send_command",
                    {"entity_id": "remote.a14", "command": "power"})
    rec = s.get_state("remote.a14")
    assert rec["state"] == "on", f"未建模服务不得改写 state，实得 {rec['state']}"
    assert rec["state"] != "send_command", "state 被写成了服务名（A14 原缺陷）"
    assert rec["attributes"].get("_unmodeled_service") == "remote.send_command"
    assert any("send_command" in c for c in s.unmodeled_calls), s.unmodeled_calls


def test_a14_attr_only_service_keeps_state():
    """纯属性服务（改亮度）不动 state。"""
    s = _store(("light.a14", "灯", "书房", "on", {}))
    s.apply_service("light", "turn_on", {"entity_id": "light.a14", "brightness": 120})
    rec = s.get_state("light.a14")
    assert rec["state"] == "on"
    assert rec["attributes"].get("brightness") == 120


def test_a14_unmodeled_service_surfaces_in_gate_reasons(gate):
    """未建模导致的断言失败，必须在闸门理由里标出来，不能让人以为是 flow 写错了。"""
    s = _store(("remote.a14g", "遥控", "客厅", "on", {}))
    flow = {"id": "a14g", "label": "a14g", "nodes": [
        _inject("t", ["c"]),
        _call("c", "remote", "send_command", "remote.a14g", data={"command": "power"}),
    ]}
    g = gate.run_staging_gate("", [{"entity_id": "remote.a14g", "state": "off"}],
                              vhass_store=s, flow=flow)
    assert g["passed"] is False
    assert any("未建模" in r for r in g["reasons"]), g["reasons"]


def test_a14_unverified_blocks_fail_closed(gate):
    """未建模服务下即便断言「碰巧对上」，顶层也不得给干净 pass —— 那是没抓到反例，
    不是验证通过。保守 fail-closed：fully_verified=False → 直接硬拦（block）。"""
    s = _store(("remote.a14w", "遥控", "客厅", "on", {}))
    flow = {"id": "a14w", "label": "a14w", "nodes": [
        _inject("t", ["c"]),
        _call("c", "remote", "send_command", "remote.a14w", data={"command": "power"}),
    ]}
    g = gate.run_staging_gate("", [{"entity_id": "remote.a14w", "state": "on"}],
                              vhass_store=s, flow=flow)
    assert g["passed"] is True, g          # 闸内确实没抓到反例
    assert g["warnings"], "未建模服务必须留下未证实项"
    assert g.get("fully_verified") is False, "未建模服务 fully_verified 必须 False"
    r = Gateway._build_unified_gate(g, None, CANARY_SKIPPED)
    assert r["verdict"] == "block", f"未证实的绿灯必须硬拦[A-fail-closed]：{r}"
    assert r["passed"] is False
    assert any("fail-closed" in n or "硬拦" in n for n in r["notes"]), r["notes"]


def test_a14_fully_verified_true_with_warnings_still_warn():
    """边界：fully_verified 为真但仍有 soft warning（矛盾态）→ 降级 warn，不硬拦。"""
    g = {"passed": True, "fully_verified": True, "warnings": ["soft 提示"]}
    r = Gateway._build_unified_gate(g, None, CANARY_SKIPPED)
    assert r["verdict"] == "warn", f"fully_verified=True 的 soft warning 应降级 warn：{r}"
    assert r["passed"] is True


# ════════════════════════════════════════════════════════════════════
# A15 · JSONata 分支在重放里归零 / 走错分支
# ════════════════════════════════════════════════════════════════════
# 旧行为 a：NR switch 的 JSONata 规则类型是 `jsonata_exp`，重放器只判 `jsonata`
#          → 永不命中 → 有 else 走 else（断言反向后置条件）、无 else 直接归零（0 意图 → 闸 skip → 假过）。
# 旧行为 b：取值节点首条 outputProperty 是「payload 容器归一」(valueType=jsonata)，
#          重放器只处理 entityState → 上游标量 payload 未归一 → payload.<field> 静默丢写。

def _flow_a15_raw(with_normalizer=True, with_else=False):
    ops = []
    if with_normalizer:
        ops.append({"property": "payload", "propertyType": "msg",
                    "value": '$type(payload) = "object" ? payload : {}',
                    "valueType": "jsonata"})
    ops.append({"property": "payload.温度", "propertyType": "msg",
                "value": "", "valueType": "entityState"})
    sw_wires = [["hit"]]
    nodes = [
        # payloadType=date → msg.payload 是标量，正是触发「静默吞写」的真实场景
        _inject("t", ["read"], payload="", ptype="date"),
        {"id": "read", "type": "api-current-state", "entityId": "sensor.a15",
         "halt_if": "", "outputProperties": ops, "wires": [["sw"]]},
        _call("hit", "climate", "turn_on", "climate.a15", []),
    ]
    if with_else:
        sw_wires.append(["miss"])
        nodes.append(_call("miss", "climate", "turn_off", "climate.a15", []))
    nodes.insert(2, {
        "id": "sw", "type": "switch", "property": "payload", "propertyType": "msg",
        "rules": ([{"t": "jsonata_exp", "v": "$number(payload.温度) < 20", "vt": "jsonata"}]
                  + ([{"t": "else", "v": "true", "vt": "jsonata"}] if with_else else [])),
        "wires": sw_wires})
    return {"id": "a15", "label": "a15", "nodes": nodes}


def _active_calls(flow, world):
    nodes = {n["id"]: n for n in flow["nodes"]}
    warns = []
    act = G._vg_evaluate_active_intents(flow, lambda e: world.get(e), None, warns)
    calls = []
    for nid in act:
        nd = nodes.get(nid) or {}
        if nd.get("type") == "api-call-service":
            d, s, _t, _d = G._ha_node_call(nd)
            calls.append(f"{d}.{s}")
    return calls, warns


def test_a15_jsonata_exp_rule_is_actually_evaluated():
    """`jsonata_exp` 规则命中时必须走 THEN 体，而不是被跳过。"""
    calls, _ = _active_calls(_flow_a15_raw(with_else=False), {"sensor.a15": "15"})
    assert calls == ["climate.turn_on"], f"JSONata 分支重放归零（A15 原缺陷）：{calls}"


def test_a15_jsonata_exp_not_matched_falls_to_else():
    """反向：条件不成立时走 else，证明不是「改成恒命中」蒙混过关。"""
    calls, _ = _active_calls(_flow_a15_raw(with_else=True), {"sensor.a15": "25"})
    assert calls == ["climate.turn_off"], calls


def test_a15_jsonata_exp_matched_beats_else():
    calls, _ = _active_calls(_flow_a15_raw(with_else=True), {"sensor.a15": "15"})
    assert calls == ["climate.turn_on"], f"有 else 时走错分支（A15 原缺陷）：{calls}"


def test_a15_payload_container_normalizer_is_replayed():
    """有归一项 → payload.<field> 落地、无告警；
    去掉归一项 → 写入被吞，必须**显式告警**而不是无声继续。"""
    _c1, w1 = _active_calls(_flow_a15_raw(with_normalizer=True), {"sensor.a15": "15"})
    assert not any("静默丢弃" in w for w in w1), w1

    calls2, w2 = _active_calls(_flow_a15_raw(with_normalizer=False), {"sensor.a15": "15"})
    assert any("静默丢弃" in w for w in w2), \
        f"标量 payload 下写 payload.<field> 应被识别为丢写并告警，实得 {w2}"
    # 字段没落地 → 分支变量取不到 → 现行策略是「保守视为命中」（避免误杀结构正确的
    # flow）。这条本身是 fail-open，唯一的护栏就是上面那条告警必须存在，
    # 并由 _build_unified_gate 在 fully_verified=False 时硬拦（见 test_a15_conservative_match_blocks_fail_closed）。
    assert calls2 == ["climate.turn_on"], calls2


def test_a15_unresolvable_jsonata_is_warned_not_silent():
    """取不到数（unavailable 转不了 float）→ 保守放行但必须留 warning，不许静默。"""
    _calls, warns = _active_calls(_flow_a15_raw(with_else=True), {"sensor.a15": "unavailable"})
    assert any("无法本地求值" in w for w in warns), warns


def test_a15_conservative_match_blocks_fail_closed(gate):
    """分支只能「保守视为命中」（无法本地求值）时，闸门即便判过，
    顶层也必须硬拦（block）—— 靠一次猜测换来的绿灯正是本工单要消灭的假过。
    保守 fail-closed：fully_verified=False → block。"""
    s = _store(("sensor.a15v", "温度", "书房", "unavailable", {}),
               ("climate.a15v", "空调", "书房", "off", {}))
    flow = _flow_a15_raw(with_else=False)
    flow["nodes"][1]["entityId"] = "sensor.a15v"
    for n in flow["nodes"]:
        if n["type"] == "api-call-service":
            n["entityId"] = "climate.a15v"
    g = gate.run_staging_gate("", [{"entity_id": "climate.a15v", "state": "on"}],
                              vhass_store=s, flow=flow)
    assert g["passed"] is True, g
    assert any("无法本地求值" in w for w in g["warnings"]), g["warnings"]
    assert g.get("fully_verified") is False, "无法本地求值 fully_verified 必须 False"
    r = Gateway._build_unified_gate(g, None, CANARY_SKIPPED)
    assert r["verdict"] == "block", f"靠猜分支换来的 pass 必须硬拦[A-fail-closed]：{r}"


def test_a15_compiled_dsl_branch_matches_physical_semantics():
    """编译产物 ↔ 重放器 对齐：断言的是物理语义（15<20 该开 / 25 不该开）。"""
    from autoflow_gateway.dsl_engine import compile_dsl
    flow = compile_dsl(
        "场景: A15编译对齐\n"
        "触发: inject\n"
        "取值: sensor.a15c 温度\n"
        "分支: $number(温度) < 20\n"
        "    动作: climate.turn_on(climate.a15c)\n"
        "否则:\n"
        "    动作: climate.turn_off(climate.a15c)\n")
    assert _active_calls(flow, {"sensor.a15c": "15"})[0] == ["climate.turn_on"]
    assert _active_calls(flow, {"sensor.a15c": "25"})[0] == ["climate.turn_off"]


# ════════════════════════════════════════════════════════════════════
# A18 · vhass 闸对非 on/off 动作永不运行却报 pass
# ════════════════════════════════════════════════════════════════════
# 旧行为：① 白箱路径用错 kwarg（expected_postconditions=）→ TypeError 被 except 吞成
#          skipped，闸门**从来没跑过**；② 期望提取只认 turn_on/turn_off；
#          ③ skip 后顶层照样 pass。

def test_a18_run_staging_gate_kwarg_is_expected():
    """签名护栏：白箱调用方用的就是 expected=，别再回归成 expected_postconditions=。"""
    import inspect
    params = list(inspect.signature(GW.run_staging_gate).parameters)
    assert "expected" in params
    assert "flow" in params, "白箱直通口 flow= 必须存在，否则又得伪造假 DSL"
    assert "expected_postconditions" not in params


def test_a18_expectation_derived_for_non_onoff_service():
    """非 on/off 动作也要能推出后置条件（旧实现一个都提不出 → 闸被 skip）。"""
    exp, unver = G._auto_expected_from_nodes([
        _call("c", "climate", "set_hvac_mode", "climate.a18", data={"hvac_mode": "cool"}),
    ])
    assert {"entity_id": "climate.a18", "state": "cool"} in exp, (exp, unver)


def test_a18_unverifiable_service_reports_reason_not_silence():
    """推不出来的必须**说清为什么**，而不是悄悄当作没有期望。"""
    exp, unver = G._auto_expected_from_nodes([
        _call("c", "fan", "set_percentage", "fan.a18", data={"percentage": 40}),
    ])
    assert exp == []
    assert unver and "无法用 state 断言" in unver[0], unver


def test_a18_skipped_but_required_degrades_to_warn():
    """闸被要求跑却 skip → 后置条件一条没验 → 顶层不许 pass，降级 warn。"""
    r = Gateway._build_unified_gate(
        {"skipped": True, "reason": "staging 闸异常: boom"},
        None, CANARY_SKIPPED, staging_required=True)
    assert r["verdict"] == "warn", r
    assert any("A18" in n for n in r["notes"]), r["notes"]
    assert r["layers"]["vhass_staging"]["ran"] is False
    assert "boom" in str(r["layers"]["vhass_staging"]["detail"]), r


def test_a18_skip_detail_tells_the_truth():
    """skip 原因必须如实回传，不能再硬编码「run_gate=False / 无 HA 动作」自相矛盾。"""
    r = Gateway._build_unified_gate(
        {"skipped": True, "reason": "flow 含 HA 动作，但没有任何后置条件可自动推导"},
        None, CANARY_SKIPPED, staging_required=True)
    assert "没有任何后置条件可自动推导" in str(r["layers"]["vhass_staging"]["detail"])


def test_a18_whitebox_verify_actually_runs_gate_for_climate(monkeypatch):
    """端到端护栏：含 climate.set_hvac_mode 的 flow 过 verify_flow，
    vhass 层必须 **ran=True**（旧实现恒 skipped）且顶层不得 pass。"""
    gw = Gateway()
    monkeypatch.setattr(gw, "get_nr_subflow_integrity", lambda: CANARY_SKIPPED)
    flow = {"id": "a18wb", "label": "a18-wb", "nodes": [
        _inject("t", ["c"]),
        _call("c", "climate", "set_hvac_mode", "climate.gate_integrity_probe",
              data={"hvac_mode": "cool"}),
    ]}
    res = gw.verify_flow(flow, run_gate=True)
    layer = res["gate"]["layers"]["vhass_staging"]
    assert layer["ran"] is True, f"vhass 闸又被 skip 了（A18 原缺陷）：{res['gate']}"
    assert res["verdict"] != "pass", f"未验证/未通过却顶层 pass：{res['gate']}"


# ════════════════════════════════════════════════════════════════════
# A22 · require_e2e 被拦却顶层 pass
# ════════════════════════════════════════════════════════════════════

def test_a22_require_e2e_but_never_ran_blocks():
    r = Gateway._build_unified_gate(None, None, CANARY_SKIPPED, require_e2e=True)
    assert r["verdict"] == "block", r
    assert r["passed"] is False
    assert any("A22" in n for n in r["notes"]), r["notes"]


def test_a22_require_e2e_but_blocked_result_blocks():
    """e2e 被 PROD 写保护拦下（e2e=False）→ 不是「跳过」，是**没验成**，必须硬拦。"""
    blocked = {"e2e": False, "verdict": "拦截", "reasons": ["PROD 写保护：拒绝落 staging"]}
    r = Gateway._build_unified_gate(None, blocked, CANARY_SKIPPED, require_e2e=True)
    assert r["verdict"] == "block", r
    assert r["layers"]["e2e_trace"]["ran"] is False
    assert any("A22" in n for n in r["notes"]), r["notes"]


def test_a22_e2e_not_required_skip_still_passes():
    """对照组：没要求跑 e2e 就跳过 → 不该被误拦（避免修过头）。"""
    r = Gateway._build_unified_gate(
        {"passed": True, "verdict": "放行", "reasons": []},
        None, CANARY_SKIPPED, require_e2e=False)
    assert r["verdict"] == "pass", r


def test_a22_require_e2e_ran_and_passed_is_pass():
    """对照组：真跑了且通过 → pass。"""
    r = Gateway._build_unified_gate(
        {"passed": True, "verdict": "放行", "reasons": []},
        {"e2e": True, "verdict": "通过", "reasons": []},
        CANARY_SKIPPED, require_e2e=True)
    assert r["verdict"] == "pass", r


def test_a22_whitebox_verify_blocks_when_e2e_swallowed(monkeypatch):
    """端到端护栏：require_e2e=True 但 e2e 被吞成 e2e=False → verify 顶层必须 block。"""
    gw = Gateway()
    monkeypatch.setattr(gw, "get_nr_subflow_integrity", lambda: CANARY_SKIPPED)
    monkeypatch.setattr(gw, "run_e2e_trace_raw",
                        lambda flow, target="staging", live=False:
                            {"e2e": False, "verdict": "拦截", "reasons": ["基建不可达"]})
    flow = {"id": "a22wb", "label": "a22-wb", "nodes": [
        _inject("t", ["d"]),
        {"id": "d", "type": "debug", "wires": [[]]},
    ]}
    res = gw.verify_flow(flow, require_e2e=True)
    assert res["verdict"] == "block", res["gate"]
    assert any("A22" in n for n in res["gate"]["notes"]), res["gate"]["notes"]


# ── block / warn 不互相吞并 ────────────────────────────────────────
def test_notes_collect_both_block_and_warn():
    """命中 block 后不得吞掉 warn 理由，排障要看得到全貌。"""
    r = Gateway._build_unified_gate(
        {"skipped": True, "reason": "闸异常"}, None, CANARY_SKIPPED,
        require_e2e=True, staging_required=True)
    assert r["verdict"] == "block"
    assert any("A22" in n for n in r["notes"])
    assert any("A18" in n for n in r["notes"]), r["notes"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
