# -*- coding: utf-8 -*-
"""#11 · 三句话人话摘要（proposal_summary）离线回归。

验证 Gateway.proposal_summary 把一条提案派生为「意图 / 验证 / 影响设备」三句话卡：
- dsl 提案（含后置条件断言）：影响设备从 DSL + 预期条件提取；验证描述断言条数。
- dsl 提案（无断言）：验证描述诚实降级「无后置条件断言」。
- raw_flow 提案（含阻断级 Lint）：影响设备从 flow 节点提取；验证描述阻断规则。
- 不存在的提案：ok=False。

全程离线：ProposalStore 落在临时 data_dir。
运行：pytest tests/test_proposal_summary.py
"""
import os
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_ps_")

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.proposals import ProposalStore
from autoflow_gateway.config import reset_config

reset_config()


def _gw():
    return Gateway()


def test_summary_dsl_with_assertions():
    gw = _gw()
    store = ProposalStore(gw.cfg)
    dsl = "场景: 夜灯\n触发: inject(payload={})\n动作: light.turn_on(light.bedroom_lamp)"
    p = store.submit(
        "agt_x", "夜灯", "skill",
        json.dumps({
            "type": "dsl", "dsl": dsl,
            "expected_postconditions": [{"entity_id": "light.bedroom_lamp", "state": "on"}],
            "node_count": 5, "require_e2e": False,
        }, ensure_ascii=False),
        source="compiler", spec=dsl)
    r = gw.proposal_summary(p.id)
    assert r["ok"] is True
    assert r["type"] == "dsl"
    assert "light.bedroom_lamp" in r["impacted_devices"]
    assert "1 条后置条件断言" in r["verification"]
    assert len(r["plain"]) == 3
    assert r["intent"] == dsl


def test_summary_dsl_no_assertions():
    gw = _gw()
    store = ProposalStore(gw.cfg)
    dsl = "场景: x\n触发: inject(payload={})\n动作: switch.turn_on(switch.foo)"
    p = store.submit(
        "agt_x", "x", "skill",
        json.dumps({
            "type": "dsl", "dsl": dsl,
            "expected_postconditions": [],
            "node_count": 3, "require_e2e": False,
        }, ensure_ascii=False),
        source="compiler", spec=dsl)
    r = gw.proposal_summary(p.id)
    assert "无后置条件断言" in r["verification"]
    assert "switch.foo" in r["impacted_devices"]


def test_summary_dsl_with_e2e():
    gw = _gw()
    store = ProposalStore(gw.cfg)
    dsl = "场景: y\n触发: inject(payload={})\n动作: fan.turn_on(fan.hall)"
    p = store.submit(
        "agt_x", "y", "skill",
        json.dumps({
            "type": "dsl", "dsl": dsl,
            "expected_postconditions": [{"entity_id": "fan.hall", "state": "on"}],
            "node_count": 3, "require_e2e": True,
        }, ensure_ascii=False),
        source="compiler", spec=dsl)
    r = gw.proposal_summary(p.id)
    assert "实机 E2E 验证" in r["verification"]


def test_summary_raw_flow_with_blocking():
    gw = _gw()
    store = ProposalStore(gw.cfg)
    flow = {"id": "f", "label": "raw", "nodes": [
        {"id": "n1", "type": "api-call-service", "entityId": "light.kitchen",
         "domain": "light", "service": "turn_on", "wires": [[]]},
    ]}
    p = store.submit(
        "agt_x", "raw", "skill",
        json.dumps({
            "type": "raw_flow", "flow": flow, "label": "raw", "node_count": 1,
            "blocked": True, "blocking_rules": ["R20"],
            "lint_error_count": 1, "lint_warning_count": 0, "require_e2e": False,
        }, ensure_ascii=False),
        source="raw", spec="raw")
    r = gw.proposal_summary(p.id)
    assert r["type"] == "raw_flow"
    assert "light.kitchen" in r["impacted_devices"]
    assert "阻断级" in r["verification"]
    assert "R20" in r["verification"]


def test_summary_subflow_no_gate():
    gw = _gw()
    store = ProposalStore(gw.cfg)
    p = store.submit(
        "agt_x", "子流程", "subflow",
        json.dumps({
            "type": "subflow", "dsl_name": "my_sub", "name": "我的子流程",
            "definition": {"id": "sub_my", "nodes": [{"id": "n", "type": "function"}]},
        }, ensure_ascii=False),
        source="raw", spec="我的子流程")
    r = gw.proposal_summary(p.id)
    assert r["type"] == "subflow"
    assert "原子注册子流程" in r["verification"]


def test_summary_missing_proposal():
    gw = _gw()
    r = gw.proposal_summary("pr_nonexistent")
    assert r["ok"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
