"""Test verify_flow (WB1-B / #687)：只读质量验证，绝不部署。

覆盖：
  - 好 flow → verdict=pass，deployed=False，无 lint 硬伤
  - 结构金丝雀含 mustache 占位 → verdict=warn
  - 结构金丝雀含空壳子流程 → verdict=warn
  - require_e2e=True → e2e 层 ran+passed，且全程未落 NR（deployed=False / create_or_update_flow 不被调）
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway.gateway import Gateway


@pytest.fixture
def gw():
    return Gateway()


def _good_flow():
    return {
        "id": "flow_verify_good",
        "label": "verify-good",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "1", "wires": [["n2"]]},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},
        ],
    }


def test_verify_good_flow_passes(gw, monkeypatch):
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    res = gw.verify_flow(_good_flow())
    assert res["ok"] is True
    assert res["deployed"] is False
    assert res["verdict"] == "pass"
    assert res["passed"] is True
    assert res["lint_error_count"] == 0
    # 金丝雀被跳过时：ran=False（verdict 按设计归为 pass，因无问题发现）
    assert res["gate"]["layers"]["structure_canary"]["ran"] is False


def test_verify_mustache_canary_warns(gw, monkeypatch):
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "live",
                                 "subflows": [{"id": "s1", "has_mustache_entity": True}],
                                 "any_empty_shell": False, "empty_shells": []})
    res = gw.verify_flow(_good_flow())
    assert res["verdict"] == "warn"
    sc = res["gate"]["layers"]["structure_canary"]
    assert sc["mustache_warnings"] == 1
    assert any("mustache" in n for n in res["gate"]["notes"])


def test_verify_empty_shell_canary_warns(gw, monkeypatch):
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "live",
                                 "subflows": [],
                                 "any_empty_shell": True, "empty_shells": ["sX"]})
    res = gw.verify_flow(_good_flow())
    assert res["verdict"] == "warn"
    sc = res["gate"]["layers"]["structure_canary"]
    assert sc["any_empty_shell"] is True


def test_verify_e2e_optin_runs_no_deploy(gw, monkeypatch):
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    # e2e 命中（verdict=通过），但 verify 不应落 NR
    monkeypatch.setattr(gw, "run_e2e_trace_raw",
                        lambda flow, target="staging", live=False:
                            {"e2e": True, "verdict": "通过", "reasons": []})
    deployed = []
    nr = getattr(gw, "nr", None)
    if nr is not None:
        monkeypatch.setattr(nr, "create_or_update_flow",
                            lambda *a, **k: deployed.append(1) or {"id": "x", "created": True})

    res = gw.verify_flow(_good_flow(), require_e2e=True)
    assert res["deployed"] is False
    assert deployed == []  # 确认没落 NR
    e2e = res["gate"]["layers"]["e2e_trace"]
    assert e2e["ran"] is True
    assert e2e["passed"] is True


def test_verify_bad_input(gw, monkeypatch):
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    res = gw.verify_flow({"label": "no-nodes"})
    assert res["ok"] is False
    assert res["deployed"] is False
    assert "nodes" in res["error"]
