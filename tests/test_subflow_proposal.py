#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1/R2 回归：子流程「agent 提案 → 人审部署注册」全链路（FakeNR，不触真实设备）。

覆盖：
  - propose_subflow 结构校验（缺字段返回 ok=False，不落档）
  - propose_subflow 落档（kind=subflow / content.type=subflow，可由 ProposalStore 取回）
  - deploy_proposal 子流程分支：原子写 NR 子流程实例 + 登记 subflow_registry（注册后可被 调用子流程 引用）
  - 冲突检测：NR 已存在同 id 子流程 → 拒绝覆盖（除非 force）
  - dry_run：返回预览、NR 零写入、注册表无新增
  - promote 守卫：子流程提案不能升格为经验 skill
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_subflow_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
os.environ["NR_HA_SERVER_ID"] = ""

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config, get_config
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.proposals import ProposalStore

from autoflow_gateway import mcp_server as MS  # noqa: F401  (确保工具注册副作用)


class FakeNR:
    """内存版 NR：按 id 存 flow + 子流程；DELETE 只删指定 id。"""
    def __init__(self):
        self._flows = {}
        self._subflows = {}  # subflow_id -> {id,type:"subflow",name,...,nodes:[...]}
        self.create_subflow_calls = []

    def create_or_update_flow(self, fid, flow, force=False, allow_prod=False):
        created = fid not in self._flows
        self._flows[fid] = {
            "id": fid, "type": "tab",
            "label": flow.get("label", ""),
            "nodes": flow.get("nodes", []),
        }
        return {"id": fid, "created": created, "raw": {"ok": True}}

    def update_flow(self, fid, flow, force=False):
        return self.create_or_update_flow(fid, flow, force=force)

    def delete_flow(self, fid, force=False):
        self._flows.pop(fid, None)
        self._subflows.pop(fid, None)
        return {"ok": True}

    def get_flow(self, fid):
        if fid in self._flows:
            return self._flows[fid]
        if fid in self._subflows:
            return self._subflows[fid]
        raise KeyError(f"flow not found: {fid}")

    def list_flows(self):
        flows = [{"id": k, "label": v["label"], "type": "tab", "nodes": v.get("nodes", [])}
                 for k, v in self._flows.items()]
        # 子流程定义也经 GET /flows 返回（与真实 NR 一致），供冲突检测枚举
        flows += [dict(s) for s in self._subflows.values()]
        return flows

    def get_default_server_id(self):
        return "server_auto"

    def validate_flow(self, flow):
        return []

    def create_subflow(self, subflow_id, name, in_ports, out_ports, nodes,
                       info="", category="subflows", env=None, allow_prod=False):
        self.create_subflow_calls.append(subflow_id)
        self._subflows[subflow_id] = {
            "id": subflow_id, "type": "subflow", "name": name, "info": info,
            "category": category, "in": in_ports or [], "out": out_ports or [],
            "env": env or [], "meta": {}, "nodes": list(nodes),
        }
        return {"id": subflow_id, "created": True}


class FakeHA:
    def __init__(self):
        self.states = []
        self.areas = {}
    def get_states(self, domain=None):
        return self.states
    def get_areas(self):
        return dict(self.areas)
    def entity_areas(self):
        return {}
    def get_state(self, eid):
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


_SF_DEF = {
    "id": "sf_my_thing",
    "nodes": [{"id": "n1", "type": "function", "func": "msg.payload='hi';return msg;", "wires": [[]]}],
    "in_ports": [{"x": 40, "y": 40, "wires": [["n1"]]}],
    "out_ports": [],
    "info": "demo subflow",
}


def _reset():
    """每个用例前清空 NR（含子流程）+ 提案库 + 子流程注册表，保证隔离。"""
    GW.nr._backend._flows.clear()
    GW.nr._backend._subflows.clear()
    GW.nr._backend.create_subflow_calls.clear()
    for fid in list(GW.state.get_flow_catalog().get("flows", {}).keys()):
        GW.state.remove_flow(fid)
    # 清空 subflow_registry（SQLite，跨用例持久，须显式清）
    for _s in list(GW.tasks.list_subflows()):
        GW.tasks.delete_subflow(_s["key"])
    _ps = ProposalStore(cfg)
    for _p in _ps.list(include_test=True):
        _ps.delete(_p.id)


def _propose_subflow(dsl_name="my_thing", name="我的子流程", definition=None, description=""):
    definition = definition if definition is not None else _SF_DEF
    return GW.propose_subflow(dsl_name, name, definition, description=description,
                              agent_id="agent_test")


def test_propose_subflow_structural_reject():
    """缺必填字段 → ok=False，不落档。"""
    _reset()
    # 缺 nodes
    r = GW.propose_subflow("x", "X", {"id": "sf_x", "in_ports": [], "out_ports": []})
    assert r["ok"] is False, r
    assert r.get("stage") == "input", r
    # 缺 id
    r = GW.propose_subflow("x", "X", {"nodes": [], "in_ports": [], "out_ports": []})
    assert r["ok"] is False, r
    # dsl_name 空
    r = GW.propose_subflow("", "X", dict(_SF_DEF))
    assert r["ok"] is False, r
    # 确认未落档任何 subflow 提案
    ps = ProposalStore(cfg)
    assert not [p for p in ps.list(include_test=True) if p.kind == "subflow"]
    print("  ✓ propose_subflow：缺字段/空名 → ok=False，不落档")


def test_propose_subflow_stores():
    """正常落档：kind=subflow / content.type=subflow，可被 ProposalStore 取回。"""
    _reset()
    r = _propose_subflow()
    assert r["ok"], r
    assert r.get("proposal_id"), r
    pid = r["proposal_id"]
    p = ProposalStore(cfg).get(pid)
    assert p is not None and p.kind == "subflow", (p.kind if p else None)
    import json as _json
    c = _json.loads(p.content)
    assert c["type"] == "subflow", c
    assert c["dsl_name"] == "my_thing", c
    assert c["definition"]["id"] == "sf_my_thing", c
    print(f"  ✓ propose_subflow：落档提案 {pid}（kind=subflow, content.type=subflow）")


def test_deploy_subflow_registers():
    """人审部署：写 NR 子流程实例 + 登记 subflow_registry。"""
    _reset()
    pid = _propose_subflow()["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human")
    assert dep["ok"], dep
    assert dep.get("subflow_id") == "sf_my_thing", dep
    assert dep.get("dsl_name") == "my_thing", dep
    assert dep.get("registered") is True, dep
    # 第 1 步：NR 子流程已建
    assert "sf_my_thing" in GW.nr._backend._subflows, "NR 子流程未创建"
    assert GW.nr._backend.create_subflow_calls == ["sf_my_thing"], GW.nr._backend.create_subflow_calls
    # 第 2 步：subflow_registry 已登记，key=dsl_name
    subs = GW.tasks.list_subflows()
    keys = {s["key"] for s in subs}
    assert "my_thing" in keys, keys
    reg = [s for s in subs if s["key"] == "my_thing"][0]
    assert reg["nr_subflow_id"] == "sf_my_thing", reg
    assert reg["status"] == "active", reg
    # 提案已标记已部署
    assert ProposalStore(cfg).get(pid).deployed_flow_id == "sf_my_thing"
    print(f"  ✓ deploy_proposal(subflow)：写 NR 子流程 + 登记 subflow_registry（key=my_thing）")


def test_deploy_subflow_conflict():
    """NR 已存在同 id 子流程 → 拒绝覆盖；force=true 仍走覆盖重建。"""
    _reset()
    # 预置一个同 id 子流程（模拟用户/网关已有）
    GW.nr._backend._subflows["sf_my_thing"] = {
        "id": "sf_my_thing", "type": "subflow", "name": "旧版", "nodes": [],
    }
    pid = _propose_subflow()["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human")
    assert dep["ok"] is False and dep.get("conflict") is True, dep
    assert "sf_my_thing" in GW.nr._backend._subflows
    assert GW.nr._backend.create_subflow_calls == [], "冲突不应写 NR"
    # force=true → 重建（覆盖式 append）
    dep2 = GW.deploy_proposal(pid, agent_id="human", force=True)
    assert dep2["ok"], dep2
    assert GW.nr._backend.create_subflow_calls == ["sf_my_thing"], dep2
    print("  ✓ 子流程冲突检测：拒绝覆盖同 id；force 重建")


def test_deploy_subflow_dry_run():
    """dry_run：返回预览、NR 零写入、注册表无新增。"""
    _reset()
    pid = _propose_subflow()["proposal_id"]
    dep = GW.deploy_proposal(pid, agent_id="human", dry_run=True)
    assert dep["ok"] and dep["dry_run"] is True, dep
    assert dep["subflow_id"] == "sf_my_thing", dep
    assert dep["dsl_name"] == "my_thing", dep
    assert GW.nr._backend.create_subflow_calls == [], "dry-run 不应写 NR"
    assert "my_thing" not in {s["key"] for s in GW.tasks.list_subflows()}, "dry-run 不应登记"
    assert ProposalStore(cfg).get(pid).deployed_flow_id in (None, ""), "dry-run 不应标记已部署"
    print("  ✓ deploy_proposal(subflow) dry-run：预览、NR 零写入、注册表未变")


def test_promote_subflow_rejected():
    """子流程提案不能升格为经验 skill。"""
    _reset()
    pid = _propose_subflow()["proposal_id"]
    try:
        ProposalStore(cfg).promote(pid)
        raise AssertionError("子流程提案不应被 promote 通过")
    except ValueError as e:
        assert "部署" in str(e) or "子流程" in str(e), str(e)
    print("  ✓ promote 守卫：子流程提案不能升格为经验 skill")


def _run():
    test_propose_subflow_structural_reject()
    test_propose_subflow_stores()
    test_deploy_subflow_registers()
    test_deploy_subflow_conflict()
    test_deploy_subflow_dry_run()
    test_promote_subflow_rejected()
    print("\n全部子流程提案测试通过 ✅  (6/6)")


if __name__ == "__main__":
    _run()
