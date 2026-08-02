#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy_proposal 对 raw_flow（白盒）部署前硬拦 Lint 反模式回归测试。

复现真实缺口：propose_raw 是 fail-open 落档（lint 只报告不拦），而 deploy_proposal
的 raw_flow 分支此前直接复用冻结的 flow、不跑 lint 硬拦 → 缺实体等坏 flow 静默上线、
且「重新部署」反复推送同一坏产物（见书房专注模式 pr_998aea1da：取值节点 entityId 为空）。

修复：deploy_proposal 的 raw_flow 分支部署前跑 lint_flow，对 R13/R15/R20/R17/R22/R24
硬拦（与 deploy_raw 一致）。本测试覆盖：
  A. 坏 raw_flow（api-current-state 空 entityId → R20）→ deploy_proposal 拒绝（ok=False, stage=lint）。
  B. 好 raw_flow（entityId 有效）→ deploy_proposal 正常上线（ok=True）。
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
_tmp = tempfile.mkdtemp(prefix="af_deploy_lint_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
os.environ["NR_HA_SERVER_ID"] = ""  # 走 FakeNR 的 get_default_server_id fallback

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config, get_config
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
    def get_states(self, domain=None):
        return []

    def get_areas(self):
        return {}

    def entity_areas(self):
        return {}

    def get_state(self, eid):
        raise RuntimeError("not found")

    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}"}


reset_config()
cfg = get_config()
GW = G.Gateway(
    config=cfg,
    ha_layer=HALayer(config=cfg, backend=FakeHA()),
    nr_layer=NRLayer(config=cfg, backend=FakeNR()),
)

# 坏 flow：api-current-state 的 entityId 为空（R20 应硬拦）
# 注意：生产里 agent 提交的 raw_flow 自带顶层 id（与 tab id 一致），此处照此构造。
BROKEN_FLOW = {
    "id": "t1", "label": "坏flow-空entityId",
    "nodes": [
        {"id": "t1", "type": "tab", "z": "t1", "name": "坏flow-空entityId"},
        {"id": "r1", "type": "api-current-state", "z": "t1", "name": "取值 x",
         "entityId": "", "server": "REPLACE_WITH_HA_SERVER", "halt_if": "",
         "outputs": 1, "wires": [[]]},
    ],
}

# 好 flow：entityId 有效（应通过）
OK_FLOW = {
    "id": "t2", "label": "好flow-有entityId",
    "nodes": [
        {"id": "t2", "type": "tab", "z": "t2", "name": "好flow-有entityId"},
        {"id": "r2", "type": "api-current-state", "z": "t2", "name": "取值 x",
         "entityId": "switch.lemesh_cn_x", "server": "REPLACE_WITH_HA_SERVER", "halt_if": "",
         "outputs": 1, "wires": [[]]},
    ],
}


def _reset():
    GW.nr._backend._flows.clear()
    for fid in list(GW.state.get_flow_catalog().get("flows", {}).keys()):
        GW.state.remove_flow(fid)


def test_deploy_raw_flow_blocks_empty_entity_id():
    """A. 坏 raw_flow（api-current-state 空 entityId → R20）→ deploy_proposal 拒绝。"""
    _reset()
    res = GW.propose_raw(BROKEN_FLOW, "agent_test", target="prod", run_gate=False)
    assert res["ok"], res
    pid = res["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert not dep["ok"], f"坏 raw_flow 不应部署成功：{dep}"
    assert dep.get("stage") == "lint", dep
    rules = {b.get("rule") for b in dep.get("issues", [])}
    assert "R20" in rules, f"应命中 R20：{dep.get('issues')}"
    # 未上线
    assert pid not in GW.state.get_flow_catalog().get("flows", {}), "坏 flow 不应登记"
    print("  ✓ A. deploy_proposal：坏 raw_flow（空 entityId → R20）被硬拦，拒绝上线")


def test_deploy_raw_flow_ok_with_valid_entity_id():
    """B. 好 raw_flow（entityId 有效）→ deploy_proposal 正常上线。"""
    _reset()
    res = GW.propose_raw(OK_FLOW, "agent_test", target="prod", run_gate=False)
    assert res["ok"], res
    pid = res["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", validate=False)
    assert dep["ok"], f"好 raw_flow 应部署成功：{dep}"
    assert dep["flow_id"] in GW.nr._backend._flows, "应已上线"
    print("  ✓ B. deploy_proposal：好 raw_flow（有效 entityId）正常上线")


def _run():
    test_deploy_raw_flow_blocks_empty_entity_id()
    test_deploy_raw_flow_ok_with_valid_entity_id()
    print("\ndeploy_proposal raw_flow lint 硬拦回归测试全部通过 ✅ (2/2)")


if __name__ == "__main__":
    _run()
