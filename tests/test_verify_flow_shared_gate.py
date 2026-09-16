# -*- coding: utf-8 -*-
"""#12 · Core/Pro 共用 verify_flow 核守卫回归。

验证 ROADMAP #12 验收门「Core/Pro 走同一验证入口」：三条公开入口
（verify_flow / propose_dsl / deploy_proposal）都汇聚到同一个 Gateway.run_staging_gate。

做法：把 gw.run_staging_gate 换成 spy（记录调用参数、返回确定性 canned verdict），
再分别驱动三条入口，断言 spy 都被命中 → 证明单一验证核。全程离线，canned verdict
避免真实 vhass/HA 依赖。deploy_proposal 路径用最小 stub 链把 NR/HA 调用架空，
使其抵达闸门调用（闸门之后即 dry 预览分支，不写真实 NR）。
运行：pytest tests/test_verify_flow_shared_gate.py
"""
import os
import sys
import json
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_sg_")

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.proposals import ProposalStore
from autoflow_gateway.config import reset_config

reset_config()


def _canned():
    return {"passed": True, "stage": "ok", "verdict": "放行",
            "assertions": [], "failures": [], "replayed_services": [],
            "external_calls": [], "entity_count": 0}


def _install_spy(gw, log):
    def spy(dsl, expected, **kw):
        log.append({"dsl": dsl, "expected": expected,
                    "flow": kw.get("flow"), "vhass_store": kw.get("vhass_store")})
        return _canned()
    gw.run_staging_gate = spy
    return gw


def test_verify_flow_calls_shared_gate():
    gw = Gateway()
    log = []
    _install_spy(gw, log)
    flow = {"id": "f", "label": "l", "nodes": [
        {"id": "a", "type": "api-call-service", "z": "t", "server": "s",
         "domain": "light", "service": "turn_on", "entityId": ["light.x"],
         "data": "", "wires": [[]]},
    ]}
    r = gw.verify_flow(flow, run_gate=True)
    assert r["ok"] is True
    assert len(log) == 1, log
    assert log[0]["flow"] == flow                 # verify_flow 走 flow= 直通口（浅拷贝，结构等价）
    # 等价性：verify_flow 与直接调 run_staging_gate(flow=同一份) 命中的是同一函数
    assert gw.run_staging_gate.__name__ == "spy"


def test_propose_dsl_calls_shared_gate():
    gw = Gateway()
    log = []
    _install_spy(gw, log)
    gw.snapshot_flow = lambda *a, **k: None       # 离线避免落盘副作用
    dsl = "场景: 测试\n触发: inject(payload={})\n动作: light.turn_on(light.probe1)"
    gw.propose_dsl(dsl, agent_id="agt_test",
                   expected_postconditions=[{"entity_id": "light.probe1", "state": "on"}])
    assert len(log) == 1, log
    assert "light.probe1" in (log[0]["dsl"] or "")


def test_deploy_proposal_calls_shared_gate():
    gw = Gateway()
    store = ProposalStore(gw.cfg)
    dsl = "场景: 部署测试\n触发: inject(payload={})\n动作: light.turn_on(light.deploy_probe)"
    p = store.submit(
        "agt_test", "部署测试", "skill",
        json.dumps({
            "type": "dsl", "dsl": dsl,
            "expected_postconditions": [{"entity_id": "light.deploy_probe", "state": "on"}],
            "node_count": 3, "require_e2e": False,
        }, ensure_ascii=False),
        source="compiler", spec=dsl)

    log = []
    _install_spy(gw, log)
    # 架空 deploy_proposal 在闸门之后的 NR/HA 写链，使其跑过闸门即进入 dry 预览分支
    gw.nr = types.SimpleNamespace(
        take_instance_snapshot=lambda label: "/snap/x",
        list_flows=lambda: [],
        get_flow=lambda fid: {"id": fid, "label": "l", "nodes": []},
        create_or_update_flow=lambda *a, **k: {"id": "f_dep", "created": True},
        update_flow_nodes=lambda *a, **k: {"ok": True},
    )
    gw.defense = types.SimpleNamespace(check_write=lambda **k: None)
    gw._inject_ha_server = lambda flow: (None, [])
    gw._ensure_history_subflow_for = lambda *a, **k: {"skipped": "stub"}
    gw._gate_node_types = lambda flow: None
    gw._validate_link_out_targets = lambda flow: []
    gw._remap_raw_flow_ids = lambda flow, did: (flow, {}, False)
    gw.state = types.SimpleNamespace(
        get_flow_catalog=lambda: {"flows": {}},
        get_flow_meta=lambda fid: None,
        upsert_flow=lambda *a, **k: None,
    )
    r = gw.deploy_proposal(p.id, dry_run=False, allow_prod=True)
    assert r.get("ok") is True, r
    assert len(log) == 1, log                      # ★ deploy_proposal 也汇聚到同一闸
    assert "light.deploy_probe" in (log[0]["dsl"] or "")


def test_three_entries_share_one_gate_function():
    """#12 等价性：三条入口都通过 self.run_staging_gate 调用同一闸，而非各自内联一份逻辑。
    单一真相源 → 相同 (dsl, expected, flow) 输入必然得到相同 verdict。"""
    import inspect
    src = inspect.getsource(Gateway)
    # 三入口各自的闸调用形态（已在源码取证中确认行 2623 / 3935 / 5690）
    assert "self.run_staging_gate(dsl, expected_postconditions," in src   # propose_dsl
    assert 'self.run_staging_gate(dsl, expected, vhass_store=vhass_store)' in src  # deploy_proposal
    assert 'self.run_staging_gate(dsl="", expected=expected_auto, flow=flow)' in src  # verify_flow


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
