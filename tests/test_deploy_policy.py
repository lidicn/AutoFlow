#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4-A 部署策略（按提案来源分流部署）单元测试（FakeNR，不触真实设备）。

覆盖：
  1. Gateway.proposal_requires_review 静态方法的策略语义（review_all / compiler_auto / 未知）。
  2. set_deploy_policy / get_deploy_policy 运行时读写 + 未知值 fail-safe 拒绝。
  3. deploy_proposal 实际回显 requires_review，且随当前策略 + 提案来源变化。

设计约束（用户拍板）：compiler_auto 只打「可信」徽章、不绕过 WebUI 人工点击，
实际部署始终由人类在 WebUI 触发且仍过 staging 闸门——本测试只验证「徽章信号」
正确，绝不验证「无人值守部署」（那本就不存在）。
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_deploy_policy_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
os.environ["NR_HA_SERVER_ID"] = ""  # 走 FakeNR 的 get_default_server_id fallback

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config, get_config, set_deploy_policy, get_deploy_policy
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.proposals import ProposalStore


class FakeNR:
    def __init__(self):
        self._flows = {}
    def create_or_update_flow(self, fid, flow, force=False, allow_prod=False):
        created = fid not in self._flows
        self._flows[fid] = {"id": fid, "type": "tab",
                            "label": flow.get("label", ""), "nodes": flow.get("nodes", [])}
        return {"id": fid, "created": created, "raw": {"ok": True}}
    def update_flow(self, fid, flow, force=False):
        return self.create_or_update_flow(fid, flow, force=force)
    def delete_flow(self, fid, force=False):
        self._flows.pop(fid, None)
        return {"ok": True}
    def get_flow(self, fid):
        if fid not in self._flows:
            raise KeyError(f"flow not found: {fid}")
        return self._flows[fid]
    def list_flows(self):
        return [{"id": k, "label": v["label"], "type": "tab", "nodes": v.get("nodes", [])}
                for k, v in self._flows.items()]
    def get_default_server_id(self):
        return "server_auto"
    def validate_flow(self, flow):
        return []


class FakeHA:
    def __init__(self):
        self.states = [
            {"entity_id": "light.study_main", "state": "off",
             "attributes": {"friendly_name": "书房主灯"}, "last_changed": "2026-07-09T10:00:00+00:00",
             "last_updated": "2026-07-09T10:00:00+00:00"},
            {"entity_id": "binary_sensor.study_door", "state": "off",
             "attributes": {"friendly_name": "书房门"}, "last_changed": "2026-07-09T10:00:00+00:00",
             "last_updated": "2026-07-09T10:00:00+00:00"},
        ]
        self.areas = {"shu_fang": "书房"}
    def get_states(self, domain=None):
        return self.states
    def get_areas(self):
        return dict(self.areas)
    def entity_areas(self):
        return {"light.study_main": "书房", "binary_sensor.study_door": "书房"}
    def get_state(self, eid):
        for s in self.states:
            if s["entity_id"] == eid:
                return s
        raise RuntimeError("not found")
    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}", "data": data}


reset_config()
cfg = get_config()
GW = G.Gateway(
    config=cfg,
    ha_layer=HALayer(config=cfg, backend=FakeHA()),
    nr_layer=NRLayer(config=cfg, backend=FakeNR()),
)
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("light.study_main", "light.study_main")
GW.state.add_mapping("binary_sensor.study_door", "binary_sensor.study_door")

_orig_build = GW._build_vhass_from_staging
def _fake_build_vhass():
    return _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
GW._build_vhass_from_staging = _fake_build_vhass

DSL = """场景: 书房入户播报
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


def _reset():
    GW.nr._backend._flows.clear()
    for fid in list(GW.state.get_flow_catalog().get("flows", {}).keys()):
        GW.state.remove_flow(fid)


def _propose_compiler():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    return res["proposal_id"]


def _propose_raw():
    """直接往 ProposalStore 插一条 source=raw 的 raw_flow 提案（绕过白盒编译）。"""
    ps = ProposalStore(cfg)
    content = json.dumps({
        "type": "raw_flow",
        "flow": {"id": "rw_flow_" + os.urandom(3).hex(), "label": "手写流", "nodes": []},
        "target": "prod",
    }, ensure_ascii=False)
    p = ps.submit("agent_test", "手写流", "skill", content,
                  tags=[], source="raw", spec="label=手写流 nodes=0")
    return p.id


def test_proposal_requires_review_semantics():
    f = G.Gateway.proposal_requires_review
    # review_all：所有来源都需人审
    assert f("review_all", "compiler") is True
    assert f("review_all", "raw") is True
    assert f("review_all", "unknown") is True
    # compiler_auto：仅 compiler 产物免审（打「可信」徽章）；raw/unknown 仍须审
    assert f("compiler_auto", "compiler") is False
    assert f("compiler_auto", "raw") is True
    assert f("compiler_auto", "unknown") is True
    # 未知策略 → fail-safe 回退需人审
    assert f("bogus_policy", "compiler") is True
    print("  ✓ proposal_requires_review：review_all 全审 / compiler_auto 仅 compiler 免审 / 未知 fail-safe")


def test_deploy_policy_get_set_roundtrip_and_failsafe():
    # 默认（无运行时文件）→ review_all
    assert get_deploy_policy(cfg) == "review_all"
    # 写入 compiler_auto → 读回一致
    set_deploy_policy(cfg, "compiler_auto")
    assert get_deploy_policy(cfg) == "compiler_auto"
    # 写回 review_all
    set_deploy_policy(cfg, "review_all")
    assert get_deploy_policy(cfg) == "review_all"
    # 未知值 → 抛 ValueError（fail-safe 拒绝）
    try:
        set_deploy_policy(cfg, "auto_pilot")
        raise AssertionError("未知部署策略应被拒绝，却写入成功")
    except ValueError:
        pass
    # 拒绝后当前值不变（仍为 review_all）
    assert get_deploy_policy(cfg) == "review_all"
    print("  ✓ set/get_deploy_policy：runtime 读写一致 + 未知值 fail-safe 拒绝")


def test_deploy_proposal_echoes_requires_review():
    # review_all + 编译器提案 → 需人审
    set_deploy_policy(cfg, "review_all")
    _reset()
    pid = _propose_compiler()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], dep
    assert dep["deploy_policy"] == "review_all", dep
    assert dep["requires_review"] is True, dep

    # compiler_auto + 编译器提案 → 免审（标「可信」）
    set_deploy_policy(cfg, "compiler_auto")
    _reset()
    pid = _propose_compiler()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], dep
    assert dep["deploy_policy"] == "compiler_auto", dep
    assert dep["requires_review"] is False, dep

    # compiler_auto + 原生手写(raw)提案 → 仍须审
    _reset()
    pid = _propose_raw()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], dep
    assert dep["requires_review"] is True, dep

    # 复位，避免污染其他测试
    set_deploy_policy(cfg, "review_all")
    print("  ✓ deploy_proposal 回显 requires_review：随策略+来源正确变化（绝不无人值守）")


def _run():
    test_proposal_requires_review_semantics()
    test_deploy_policy_get_set_roundtrip_and_failsafe()
    test_deploy_proposal_echoes_requires_review()
    print(f"\nP4-A 部署策略测试全部通过 ✅  (3/3)")


if __name__ == "__main__":
    _run()
