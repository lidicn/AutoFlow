"""WB84（P2/P3）回归测试：verify_flow 诚实性 + modify_flow 直写健壮性。

P3（verify_flow / staging 闸诚实性）：
  - 错域 service（switch.turn_on 作用于 light）必须 fail-closed 拦截，
    且不得宣称 fully_verified（修复 P3-F2 矛盾 + P3-F4 虚假 fully_verified）。
  - 合法 light.turn_on 仍放行且 fully_verified=True（G 用例不误伤）。
  - 未建模 service（light.banana）后置条件不可验证 → 降级 warn（不硬拦，P3-F1）。

P2（modify_flow node_patches 健壮性）：
  - 按 type 模糊匹配命中 >1 节点须显式 allow_bulk=True，否则 fail-closed 拒绝（P2-F-MULTI）。
  - 删不存在字段是 no-op，不虚报 changed（P2-F-REMOVENONE）。
"""
import os
import sys
import json
import tempfile
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import gateway as G


# ───────────────────────── P3：staging 闸（白箱直通口）─────────────────────────
class _StageFake:
    """最小 Gateway 替身：仅提供 run_staging_gate 所需属性。"""
    def __init__(self, data_dir):
        self.cfg = types.SimpleNamespace(data_dir=data_dir)
        self.state = None
        self.nr = None
        # 测试替身无真实 HA：方案A 播种桥（_seed_read_value_entities_from_ha）
        # 见 self.ha is None 即 no-op，维持 fail-closed 安全不变。
        self.ha = None
    _build_vhass_from_staging = G.Gateway._build_vhass_from_staging
    _gate_node_types = G.Gateway._gate_node_types
    run_staging_gate = G.Gateway.run_staging_gate
    _seed_read_value_entities_from_ha = G.Gateway._seed_read_value_entities_from_ha


def _make_stage_fake(cat_entities):
    d = tempfile.mkdtemp()
    cat_dir = os.path.join(d, "staging", "state")
    os.makedirs(cat_dir, exist_ok=True)
    # vhass build_seed_from_catalog 期望 entities 为含 entity_id 键的对象列表
    ents = [{"entity_id": eid, "friendly_name": v.get("friendly_name", eid),
             "state": v.get("state", "off"),
             "capabilities": v.get("capabilities", ["brightness"])}
            for eid, v in cat_entities.items()]
    with open(os.path.join(cat_dir, "device_catalog.json"), "w", encoding="utf-8") as f:
        json.dump({"entities": ents}, f)
    return _StageFake(d)


def _api_node(nid, domain, service, eid):
    return {"id": nid, "type": "api-call-service", "z": "f",
            "domain": domain, "service": service, "entityId": eid, "wires": [[]]}


def test_staging_domain_mismatch_blocks_and_not_fully_verified():
    # switch.turn_on 作用于 light 实体 → 错域，必须拦截且 fully_verified=False
    gw = _make_stage_fake({"light.demo": {"friendly_name": "Demo"}})
    flow = {"id": "f", "label": "t", "nodes": [_api_node("n1", "switch", "turn_on", "light.demo")]}
    expected = [{"entity_id": "light.demo", "state": "on"}]
    r = gw.run_staging_gate(dsl="", expected=expected, flow=flow, branch_aware=False)
    assert r["passed"] is False, f"错域 service 应通过 fail-closed 拦截，实则 passed={r['passed']}"
    assert r["fully_verified"] is False, "错域 service 不得宣称 fully_verified"
    # 断言项里应含 domain_mismatch 标记
    assert any(a.get("domain_mismatch") for a in r.get("assertions", [])), \
        "应含 domain_mismatch 断言项"


def test_staging_legit_light_turn_on_passes():
    gw = _make_stage_fake({"light.demo": {"friendly_name": "Demo"}})
    flow = {"id": "f", "label": "t", "nodes": [_api_node("n1", "light", "turn_on", "light.demo")]}
    expected = [{"entity_id": "light.demo", "state": "on"}]
    r = gw.run_staging_gate(dsl="", expected=expected, flow=flow, branch_aware=False)
    assert r["passed"] is True, f"合法 light.turn_on 应放行，实则 {r.get('reasons')}"
    assert r["fully_verified"] is True


def test_staging_unmodeled_service_downgraded_to_warn():
    # vhass 未建模 light.banana → 后置条件不可验证 → 降级 warn（不硬拦），fully_verified=False
    gw = _make_stage_fake({"light.demo": {"friendly_name": "Demo"}})
    flow = {"id": "f", "label": "t", "nodes": [_api_node("n1", "light", "banana", "light.demo")]}
    expected = [{"entity_id": "light.demo", "state": "on"}]
    r = gw.run_staging_gate(dsl="", expected=expected, flow=flow, branch_aware=False)
    # 未建模：不计入硬失败（passed=True），但 fully_verified 诚实=False
    assert r["passed"] is True, f"未建模 service 应降级 warn 而非硬拦，实则 {r.get('reasons')}"
    assert r["fully_verified"] is False, "未建模 service 不得宣称 fully_verified"


# ───────────────────────── P2：modify_flow node_patches ─────────────────────────
class _NRStub:
    def __init__(self, base):
        self._base = base
        self.deployed = None
    def get_flow(self, flow_id):
        return self._base
    def get_default_server_id(self):
        return None
    def create_or_update_flow(self, flow_id, target, force=True, allow_prod=False):
        self.deployed = target
        return {"id": flow_id}


class _GWStub:
    def __init__(self, base):
        self.nr = _NRStub(base)
        self.state = None
        self.agent_id = "test"
        self.cfg = types.SimpleNamespace(nr_ha_server_id="")
    _gate_node_types = G.Gateway._gate_node_types
    _inject_ha_server = G.Gateway._inject_ha_server
    modify_flow = G.Gateway.modify_flow


def test_node_patches_type_only_multi_hits_refused_without_allow_bulk():
    # 流内含 3 个 function 节点；按 type=function 模糊匹配命中 3 → 须拒绝（脚枪防护）
    base = {"id": "f", "label": "t", "nodes": [
        {"id": "a", "type": "function", "name": "fa", "z": "f", "wires": [[]]},
        {"id": "b", "type": "function", "name": "fb", "z": "f", "wires": [[]]},
        {"id": "c", "type": "function", "name": "fc", "z": "f", "wires": [[]]},
    ]}
    gw = _GWStub(base)
    r = gw.modify_flow("f", node_patches=[{"match": {"type": "function"}, "set": {"name": "X"}}])
    assert r["ok"] is False, f"type 模糊命中多节点应拒绝，实则 {r}"
    assert r.get("ambiguous_hits") == 3
    assert gw.nr.deployed is None, "被拒后不应部署"


def test_node_patches_type_only_multi_hits_ok_with_allow_bulk():
    base = {"id": "f", "label": "t", "nodes": [
        {"id": "a", "type": "function", "name": "fa", "z": "f", "wires": [[]]},
        {"id": "b", "type": "function", "name": "fb", "z": "f", "wires": [[]]},
    ]}
    gw = _GWStub(base)
    r = gw.modify_flow("f", allow_prod=True,
                       node_patches=[{"match": {"type": "function"}, "set": {"name": "X"},
                                      "allow_bulk": True}])
    assert r["ok"] is True, f"allow_bulk=True 应放行，实则 {r}"
    assert r["changed_nodes"] == 2
    assert gw.nr.deployed is not None


def test_node_patches_remove_nonexistent_not_counted():
    # 删不存在字段是 no-op → changed=0 → 因无实际改动而中止（不部署、不虚报）
    base = {"id": "f", "label": "t", "nodes": [
        {"id": "a", "type": "function", "name": "fa", "z": "f", "wires": [[]]},
    ]}
    gw = _GWStub(base)
    r = gw.modify_flow("f", allow_prod=True,
                       node_patches=[{"match": {"id": "a"}, "remove": ["__nonexistent_field__"]}])
    assert r["ok"] is False, f"删不存在字段应判定无改动而中止，实则 {r}"
    assert gw.nr.deployed is None


def test_node_patches_real_remove_counted():
    base = {"id": "f", "label": "t", "nodes": [
        {"id": "a", "type": "function", "name": "fa", "z": "f", "wires": [[]], "junk": 1},
    ]}
    gw = _GWStub(base)
    r = gw.modify_flow("f", allow_prod=True,
                       node_patches=[{"match": {"id": "a"}, "remove": ["junk"]}])
    assert r["ok"] is True, f"删真实字段应成功，实则 {r}"
    assert r["changed_nodes"] == 1
    assert "junk" not in gw.nr.deployed["nodes"][0]


if __name__ == "__main__":
    test_staging_domain_mismatch_blocks_and_not_fully_verified()
    test_staging_legit_light_turn_on_passes()
    test_staging_unmodeled_service_downgraded_to_warn()
    test_node_patches_type_only_multi_hits_refused_without_allow_bulk()
    test_node_patches_type_only_multi_hits_ok_with_allow_bulk()
    test_node_patches_remove_nonexistent_not_counted()
    test_node_patches_real_remove_counted()
    print("ALL WB84 P3/P2 REGRESSION TESTS PASSED ✅")
