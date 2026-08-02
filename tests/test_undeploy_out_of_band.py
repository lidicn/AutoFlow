#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""undeploy 带外删除（用户手动删 NR tab/节点）回归测试 —— P5 修复。

复现真实 bug：用户在 WebUI 之外手动删掉网关部署的 tab / 网关节点后，
账本仍标记 deployed，但 NR 侧已不存在。此时必须：
  1. undeploy 返回 ok=True（清账本，不硬报错、不卡死）；
  2. 之后 deploy_proposal 仍可再次重部署（自愈），WebUI「重新部署到 NR」按钮可用。

覆盖三种带外删除形态：
  A. 整个 tab 被手动删（get_flow 抛错 → already_gone）。
  B. tab 残留但网关节点被手动删（活 flow 无本网关节点 → 视为 already_gone）。
  C. NR 删除调用本身失败（如半残/权限）→ 账本仍清理，undeploy 不报错。
  D. 带外删后 deploy_proposal 可重部署（自愈闭环）。
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
_tmp = tempfile.mkdtemp(prefix="af_undeploy_oob_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
os.environ["NR_HA_SERVER_ID"] = ""  # 走 FakeNR 的 get_default_server_id fallback

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config, get_config
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.proposals import ProposalStore


class FakeNR:
    """可模拟「用户手动在 NR UI 删 tab/节点」的带外删除桩。"""
    def __init__(self):
        self._flows = {}
        self._deleted_oob = set()        # 被用户手动删掉的 flow id（带外）
        self.delete_always_fail = False  # 模拟 NR 删除调用本身失败（半残/权限）

    def create_or_update_flow(self, fid, flow, force=False):
        created = fid not in self._flows
        self._flows[fid] = {"id": fid, "type": "tab",
                            "label": flow.get("label", ""), "nodes": flow.get("nodes", [])}
        self._deleted_oob.discard(fid)
        return {"id": fid, "created": created, "raw": {"ok": True}}

    def update_flow(self, fid, flow, force=False):
        return self.create_or_update_flow(fid, flow, force=force)

    def delete_flow(self, fid, force=False):
        if self.delete_always_fail:
            raise RuntimeError(f"DELETE /flow/{fid} -> 500 (模拟 NR 删除失败)")
        if fid in self._deleted_oob:
            raise RuntimeError(f"DELETE /flow/{fid} -> 404 (已被手动删除)")
        self._flows.pop(fid, None)
        return {"ok": True}

    def get_flow(self, fid):
        if fid in self._deleted_oob or fid not in self._flows:
            raise KeyError(f"flow not found: {fid}")
        return self._flows[fid]

    def list_flows(self):
        return [{"id": k, "label": v["label"], "type": "tab", "nodes": v.get("nodes", [])}
                for k, v in self._flows.items() if k not in self._deleted_oob]

    def get_default_server_id(self):
        return "server_auto"

    def validate_flow(self, flow):
        return []

    def simulate_out_of_band_delete(self, fid):
        """用户手动在 NR UI 删掉 tab（带外）。"""
        self._deleted_oob.add(fid)
        self._flows.pop(fid, None)


class FakeHA:
    def __init__(self):
        self.states = [
            {"entity_id": "light.study_main", "state": "off",
             "attributes": {"friendly_name": "书房主灯"},
             "last_changed": "2026-07-09T10:00:00+00:00",
             "last_updated": "2026-07-09T10:00:00+00:00"},
            {"entity_id": "binary_sensor.study_door", "state": "off",
             "attributes": {"friendly_name": "书房门"},
             "last_changed": "2026-07-09T10:00:00+00:00",
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


def _vhass():
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities((
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    ))
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


GW._build_vhass_from_staging = _vhass

DSL = """场景: 书房入户播报
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""


def _reset():
    GW.nr._backend._flows.clear()
    GW.nr._backend._deleted_oob.clear()
    GW.nr._backend.delete_always_fail = False
    for fid in list(GW.state.get_flow_catalog().get("flows", {}).keys()):
        GW.state.remove_flow(fid)


def _propose_and_deploy():
    res = GW.propose_dsl(DSL, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=_vhass())
    assert res["ok"], res
    pid = res["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], dep
    return pid, dep["flow_id"]


def test_undeploy_after_out_of_band_tab_delete():
    """A. 整个 tab 被手动删 → undeploy 返回 already_gone + 账本清理。"""
    _reset()
    pid, fid = _propose_and_deploy()
    ps = ProposalStore(cfg)
    assert ps.get(pid).deployed_flow_id == fid
    # 用户手动在 NR 删掉 tab（带外）
    GW.nr._backend.simulate_out_of_band_delete(fid)
    r = GW.undeploy(fid)
    assert r["ok"], r
    assert r["action"] == "already_gone", r
    # 账本 + 提案标记清理
    assert fid not in GW.state.get_flow_catalog().get("flows", {})
    assert ps.get(pid).deployed_flow_id is None
    print("  ✓ A. undeploy：tab 带外删除 → already_gone + 清账本 + 提案解部署")


def test_undeploy_partial_node_delete():
    """B. tab 残留但网关节点被手动删 → 视为 already_gone，不撞 delete_flow。"""
    _reset()
    pid, fid = _propose_and_deploy()
    # 模拟：tab 还在，但网关节点被手动删（活 flow 里找不到账本登记的节点 id）
    live = GW.nr._backend._flows[fid]
    live["nodes"] = [n for n in live["nodes"] if n.get("type") == "tab"]
    r = GW.undeploy(fid)
    assert r["ok"], r
    assert r["action"] == "already_gone", r
    ps = ProposalStore(cfg)
    assert ps.get(pid).deployed_flow_id is None
    print("  ✓ B. undeploy：网关节点被手动删（tab 残余）→ already_gone，不报 NR 删除失败")


def test_undeploy_nr_delete_fails_but_ledger_cleared():
    """C. NR 删除调用本身失败（半残/权限）→ 账本仍清理，undeploy 不硬报错。"""
    _reset()
    pid, fid = _propose_and_deploy()
    GW.nr._backend.delete_always_fail = True  # 模拟 NR 删除调用永远失败
    r = GW.undeploy(fid)
    assert r["ok"], r
    assert r.get("nr_warning"), r  # 应带 NR 侧警告
    ps = ProposalStore(cfg)
    assert ps.get(pid).deployed_flow_id is None
    # 账本已清（即便 NR 侧 flow 仍在，自愈时 deploy_proposal 会覆盖）
    assert fid not in GW.state.get_flow_catalog().get("flows", {})
    print("  ✓ C. undeploy：NR 删除失败 → 仍清账本 + 返回 ok（不卡死）")


def test_redeploy_after_out_of_band_delete():
    """D. 带外删后 deploy_proposal 仍能重部署（自愈闭环）。"""
    _reset()
    pid, fid = _propose_and_deploy()
    GW.nr._backend.simulate_out_of_band_delete(fid)
    # 直接重部署（无需先 undeploy，与 WebUI「重新部署到 NR」等价）
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], dep
    assert dep["flow_id"] == fid, dep  # 同 id 重建
    assert fid in GW.nr._backend._flows, "重部署后 NR 应有该 flow"
    ps = ProposalStore(cfg)
    assert ps.get(pid).deployed_flow_id == fid
    print("  ✓ D. deploy_proposal：带外删后重部署成功（自愈闭环）")


def _run():
    test_undeploy_after_out_of_band_tab_delete()
    test_undeploy_partial_node_delete()
    test_undeploy_nr_delete_fails_but_ledger_cleared()
    test_redeploy_after_out_of_band_delete()
    print("\nundeploy 带外删除回归测试全部通过 ✅ (4/4)")


if __name__ == "__main__":
    _run()
