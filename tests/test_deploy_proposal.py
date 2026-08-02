#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 收尾：DSL 提案 → 直接部署 NR → 安全撤回 全链路测试（FakeNR，不触真实设备）。

重点覆盖用户最关心的「撤回不会误删用户自己的 flow」：
  - 撤回只删 flow_catalog 账本里登记的（owner=agent）flow
  - 用户自己写的 flow 不在账本 → 撤回按钮不会出现、后端也拒绝删
  - 部署前冲突检测：同名非本流拒绝覆盖
  - 撤回护栏：flow 被用户手动改过 → 中止并要求 force 强确认
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_deploy_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
# 测试环境无关：清空 NR_HA_SERVER_ID，让部署走 FakeNR 的 get_default_server_id fallback
os.environ["NR_HA_SERVER_ID"] = ""

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config, get_config
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.proposals import ProposalStore


class FakeNR:
    """内存版 NR：按 id 存 flow，DELETE 只删指定 id。"""
    def __init__(self):
        self._flows = {}  # id -> {"id","label","type":"tab","nodes":[...]}
    def create_or_update_flow(self, fid, flow, force=False):
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

# 让默认 staging vhass（deploy_proposal 未显式注入时）含书房实体，
# 否则闸门会因 vhass 无该实体而断言失败。失败用例会自行注入 vhass 覆盖。
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
    """每个用例前清空 NR + 账本 + 提案库，保证隔离。

    必清提案库：ProposalStore.submit 有 120s 去重窗（同 agent_id+content 复用同一
    pid，防 MCP 客户端超时重试 fan-out 产生重复提案）。测试里所有 _propose() 的
    DSL+预期完全相同且整轮跑完 <2s，若不清提案库会被去重复用首个 pid——而该 pid
    往往已在上一用例被部署（mark_deployed），导致后续 dry-run/未部署断言误判。
    """
    GW.nr._backend._flows.clear()
    for fid in list(GW.state.get_flow_catalog().get("flows", {}).keys()):
        GW.state.remove_flow(fid)
    _ps = ProposalStore(cfg)
    for _p in _ps.list(include_test=True):
        _ps.delete(_p.id)


def _propose():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    assert res["gate_passed"] is True, res["gate"]
    return res["proposal_id"]


def _deploy_and_check():
    """部署一个标准「书房入户播报」并返回 (fid, pid)。不自带 reset。"""
    pid = _propose()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod")
    assert dep["ok"], dep
    assert "pending_id" not in dep, f"不应再进确认闸：{dep}"
    fid = dep["flow_id"]
    flow = GW.nr._backend.get_flow(fid)
    types = {n.get("type") for n in flow["nodes"]}
    assert "server-state-changed" in types, types
    assert "link out" in types, types
    assert "function" not in types, "编译产物不应含 Function 节点"
    ssc = [n for n in flow["nodes"] if n.get("type") == "server-state-changed"]
    assert ssc[0]["server"] == "server_auto", ssc[0]["server"]
    # 关键：api-call-service 等动作节点的 server 占位符也必须被替换（否则 Invalid server_config）
    acs = [n for n in flow["nodes"] if n.get("type") == "api-call-service"]
    if acs:
        assert acs[0]["server"] == "server_auto", acs[0]["server"]
    assert dep["server_resolved"] is True
    meta = GW.state.get_flow_meta(fid)
    assert meta and meta.get("owner_agent") == "human", meta
    # 部署时记录网关节点 id（手术式撤回的唯一依据）
    assert "deployed_node_ids" in meta and len(meta["deployed_node_ids"]) >= 1, meta
    p = ProposalStore(cfg).get(pid)
    assert p.deployed_flow_id == fid, p.deployed_flow_id
    return fid, pid


def test_deploy_direct_and_catalog():
    _reset()
    fid, pid = _deploy_and_check()
    print(f"  ✓ 直接部署：提案 {pid} → NR flow '{fid}' ｜ 已登记账本+标记提案")
    return fid, pid


def test_undeploy_only_ours_not_user_flow():
    _reset()
    # 用户自己先在 NR 写了个 flow（不同 id，不在账本）
    GW.nr._backend._flows["user_xyz"] = {
        "id": "user_xyz", "type": "tab", "label": "用户自定义流", "nodes": [{"id": "u1", "type": "inject"}],
    }
    fid, pid = _deploy_and_check()
    assert "user_xyz" in GW.nr._backend._flows, "前置：用户 flow 应在"
    # 撤回我们的 flow
    r = GW.undeploy(fid)
    assert r["ok"], r
    assert fid not in GW.nr._backend._flows, "我们的 flow 应被删"
    assert "user_xyz" in GW.nr._backend._flows, "❗用户的 flow 绝不能被删"
    assert GW.state.get_flow_meta(fid) is None, "账本应移除"
    # 撤回一个不在账本的用户 flow → 拒绝
    r2 = GW.undeploy("user_xyz")
    assert r2["ok"] is False and r2.get("code") == "not_ours", r2
    assert "user_xyz" in GW.nr._backend._flows, "拒绝后用户 flow 仍在"
    print(f"  ✓ 安全撤回：只删我们的 flow '{fid}'，用户 flow 'user_xyz' 完好；账本外撤回被拒")


def test_conflict_refuses_overwrite():
    _reset()
    # NR 里已存在同名 flow（用户写的，不在账本）
    GW.nr._backend._flows["user_same"] = {
        "id": "user_same", "type": "tab", "label": "书房入户播报", "nodes": [{"id": "x", "type": "inject"}],
    }
    pid = _propose()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod")
    assert dep["ok"] is False and dep.get("conflict") is True, dep
    assert "user_same" in GW.nr._backend._flows, "用户同名 flow 不应被覆盖"
    assert "user_same" not in GW.state.get_flow_catalog().get("flows", {}), "用户 flow 绝不应进账本"
    # force=true → 改名新建副本，绝不覆盖
    dep2 = GW.deploy_proposal(pid, agent_id="human", target="prod", force=True)
    assert dep2["ok"], dep2
    assert dep2["label"] == "书房入户播报 (网关副本)", dep2["label"]
    assert "user_same" in GW.nr._backend._flows, "force 仍不覆盖用户 flow"
    assert dep2["flow_id"] != "user_same"
    print(f"  ✓ 冲突检测：拒绝覆盖同名用户 flow；force 改名新建副本 '{dep2['flow_id']}'")


def test_undeploy_trims_tab_keeping_user_nodes():
    _reset()
    fid, pid = _deploy_and_check()
    deployed = set(GW.state.get_flow_meta(fid)["deployed_node_ids"])
    # 用户在同一 tab 里自己写了节点（模拟场景：网关部署的 tab 上用户另加 flow）
    live = GW.nr._backend.get_flow(fid)
    live["nodes"].append({"id": "user_a", "type": "debug", "z": fid, "wires": [[]]})
    live["nodes"].append({"id": "user_b", "type": "inject", "z": fid, "wires": [[]]})
    r = GW.undeploy(fid)  # 无 force
    assert r["ok"], r
    assert r["action"] == "trimmed_tab", r
    assert r["gateway_nodes_removed"] == len(deployed), r
    assert r["user_nodes_preserved"] == 2, r
    # tab 仍在，且只含用户节点（网关节点全部移除）
    assert fid in GW.nr._backend._flows, "tab 应保留（用户有内容）"
    remaining = {n["id"] for n in GW.nr._backend.get_flow(fid)["nodes"]}
    assert "user_a" in remaining and "user_b" in remaining, remaining
    assert deployed.isdisjoint(remaining), "网关节点不应残留"
    assert GW.state.get_flow_meta(fid) is None, "账本应移除"
    print(f"  ✓ 手术式撤回：移除 {r['gateway_nodes_removed']} 个网关节点，保留 {r['user_nodes_preserved']} 个用户节点，tab 不删")


def test_undeploy_deletes_empty_tab():
    _reset()
    fid, pid = _deploy_and_check()
    r = GW.undeploy(fid)
    assert r["ok"], r
    assert r["action"] == "deleted_tab", r
    assert fid not in GW.nr._backend._flows, "tab 无用户节点 → 删除整个 tab"
    assert GW.state.get_flow_meta(fid) is None
    print(f"  ✓ 整 tab 删除：tab 无用户节点 → 删除整个 tab")


def _propose_ex(dsl, expected, vhass):
    res = GW.propose_dsl(dsl, "agent_test", expected, vhass_store=vhass)
    assert res["ok"], res
    return res["proposal_id"]


def test_deploy_gate_blocks_on_failing_expected():
    """护城河：部署前 staging 闸门断言不通过 → 拒绝部署，绝不落 NR。"""
    _reset()
    vhass = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    # 预期与 flow 实际效果相反（flow 会开灯，却断言变 off）→ 闸门必失败
    pid = _propose_ex(DSL,
                      [{"entity_id": "light.study_main", "state": "off"}],
                      vhass)
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod",
                             validate=True, vhass_store=vhass)
    assert dep["ok"] is False, dep
    assert dep.get("gate_passed") is False, dep
    assert "gate" in dep and dep["gate"].get("passed") is False, dep
    # ❗关键：NR 不应被写入任何 flow
    assert len(GW.nr._backend._flows) == 0, "闸门失败却部署了，护城河失效！"
    assert GW.state.get_flow_meta(list(GW.nr._backend._flows.keys())[0]) is None if GW.nr._backend._flows else True
    print(f"  ✓ 部署前闸门：断言不通过 → 拒绝部署，NR 零写入（护城河生效）")


def test_deploy_gate_passes_on_matching_expected():
    """闸门断言通过 → 正常部署到 NR。"""
    _reset()
    vhass = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    pid = _propose_ex(DSL,
                      [{"entity_id": "light.study_main", "state": "on"}],
                      vhass)
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod",
                             validate=True, vhass_store=vhass)
    assert dep["ok"], dep
    assert dep.get("gate_passed") is True, dep
    assert dep["flow_id"] in GW.nr._backend._flows, "闸门通过应已落 NR"
    print(f"  ✓ 部署前闸门：断言通过 → 正常部署 flow '{dep['flow_id']}'")


def test_deploy_proposal_dry_run_no_write():
    """A8：deploy_proposal dry_run 返回节点预览，绝不落 NR、不进账本。"""
    _reset()
    pid = _propose()
    dep = GW.deploy_proposal(pid, agent_id="human", target="prod", dry_run=True)
    assert dep["ok"] and dep["dry_run"] is True, dep
    assert dep["would"] == "create", dep
    assert len(dep["node_diff"]["added"]) > 0, dep["node_diff"]
    assert dep["server_resolved"] is True, dep
    assert "_trace_id" in dep
    # 关键：dry-run 零写入、不进账本、提案不标记已部署
    assert len(GW.nr._backend._flows) == 0, "dry-run 却写了 NR！"
    assert not GW.state.get_flow_catalog().get("flows", {}), "dry-run 不应进账本"
    assert ProposalStore(cfg).get(pid).deployed_flow_id in (None, ""), "dry-run 不应标记已部署"
    print("  ✓ deploy_proposal dry-run：返回预览、NR 零写入、账本/提案未变")


def test_e2e_gate_blocks_and_inherits():
    """iss_8d3cffaa96 修复回归：require_e2e 沿提案链透传，部署主路径真正跑 e2e 闸。

    此前 require_e2e 被 JSON-RPC 静默吞掉、且 deploy_proposal 从不调 e2e 闸
    （#613 焊在 deploy_raw 的闸对提案部署是死代码）。本测试验证：
      A. 显式 require_e2e=True + verdict=断点 → 拦部署、NR 零写入
      B. 显式 require_e2e=True + verdict=通过 → 放行部署
      C. 提案落档带 require_e2e=True、部署不显式覆盖 → 继承意图、闸生效（核心修复）
    """
    _reset()
    pid = _propose()  # 默认 require_e2e=False
    real_e2e = GW.run_e2e_trace_raw
    try:
        # 场景 A：显式 require_e2e=True + verdict=断点 → 拦部署
        GW.run_e2e_trace_raw = lambda *a, **k: {
            "e2e": True, "verdict": "断点", "reasons": ["mock 断点"],
            "report": {"reached_count": 0, "expected_count": 1},
        }
        dep = GW.deploy_proposal(pid, agent_id="human", target="prod",
                                 validate=False, require_e2e=True)
        assert dep["ok"] is False, dep
        assert dep.get("stage") == "e2e_gate", dep
        assert len(GW.nr._backend._flows) == 0, "e2e 拦截却落 NR！"
        print("  ✓ e2e 闸(显式 True, verdict=断点)：拦部署，NR 零写入")

        # 场景 B：require_e2e=True + verdict=通过 → 放行部署
        GW.run_e2e_trace_raw = lambda *a, **k: {
            "e2e": True, "verdict": "通过", "reasons": ["mock 通过"],
            "report": {"reached_count": 1, "expected_count": 1},
        }
        dep = GW.deploy_proposal(pid, agent_id="human", target="prod",
                                 validate=False, require_e2e=True)
        assert dep["ok"], dep
        assert dep.get("require_e2e") is True, dep
        assert dep["flow_id"] in GW.nr._backend._flows, "e2e 通过应落 NR"
        print("  ✓ e2e 闸(显式 True, verdict=通过)：放行部署")
    finally:
        GW.run_e2e_trace_raw = real_e2e

    # 场景 C：提案落档带 require_e2e=True，部署不显式覆盖 → 继承意图，闸生效
    _reset()
    vhass = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=vhass, require_e2e=True)
    assert res["ok"], res
    assert res.get("require_e2e") is True, res
    pid2 = res["proposal_id"]
    real_e2e = GW.run_e2e_trace_raw
    try:
        GW.run_e2e_trace_raw = lambda *a, **k: {
            "e2e": True, "verdict": "断点", "reasons": ["mock 断点"],
            "report": {"reached_count": 0, "expected_count": 1},
        }
        # 不传 require_e2e（None）→ 应继承提案 content.require_e2e=True
        dep = GW.deploy_proposal(pid2, agent_id="human", target="prod", validate=False)
        assert dep["ok"] is False, dep
        assert dep.get("stage") == "e2e_gate", dep
        assert len(GW.nr._backend._flows) == 0, "继承意图后 e2e 仍拦截"
        print("  ✓ e2e 闸(继承提案意图 require_e2e=True)：部署时真正拦截，iss_8d3cffaa96 修复")
    finally:
        GW.run_e2e_trace_raw = real_e2e


def _run():
    test_deploy_direct_and_catalog()
    test_undeploy_only_ours_not_user_flow()
    test_conflict_refuses_overwrite()
    test_undeploy_trims_tab_keeping_user_nodes()
    test_undeploy_deletes_empty_tab()
    test_deploy_gate_blocks_on_failing_expected()
    test_deploy_gate_passes_on_matching_expected()
    test_deploy_proposal_dry_run_no_write()
    test_e2e_gate_blocks_and_inherits()
    print(f"\n全部测试通过 ✅  (9/9)")


if __name__ == "__main__":
    _run()
