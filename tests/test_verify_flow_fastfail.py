"""Test verify_flow · will-pass 静态快检（议题七 B / v2.3.0 任务 3.3）。

覆盖：
  - 含 HA 动作 + 静态硬伤（R17 悬空连线）的流 → verify_flow 在 lint 阶段即秒回 block，
    绝不触发昂贵的 vhass 重放（run_staging_gate 不被调用）。
  - 干净含 HA 动作的流 → 不秒回，vhass 重放正常触发（确认未误杀正常路径）。
  - 纯 schema 致命项（S5 空 flow） → 同样秒回 block。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway.gateway import Gateway


@pytest.fixture
def gw():
    return Gateway()


def _ha_flow_with_dangling_wire():
    """api-call-service 节点（HA 动作）+ 悬空连线（wires 指向不存在的 ghost）→ R17。"""
    return {
        "id": "flow_ha_r17",
        "label": "ha-with-r17",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "1", "wires": [["svc"]]},
            {"id": "svc", "type": "api-call-service", "z": "1",
             "wires": [["ghost"]],   # ghost 不存在 → R17 硬伤
             "domain": "light", "service": "turn_on",
             "entityId": ["light.study_desk"], "data": {}},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},
        ],
    }


def _clean_ha_flow():
    """合法的 inject → api-call-service → debug，无静态硬伤（含 server 占位，schema 不致命）。"""
    return {
        "id": "flow_ha_good",
        "label": "ha-good",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "1", "wires": [["svc"]]},
            {"id": "svc", "type": "api-call-service", "z": "1",
             "wires": [["n2"]],
             "server": "REPLACE_WITH_HA_SERVER",
             "domain": "light", "service": "turn_on",
             "entityId": ["light.study_desk"], "data": {}},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},
        ],
    }


def test_verify_flow_fast_fail_on_static_block(gw, monkeypatch):
    calls = []
    monkeypatch.setattr(gw, "run_staging_gate",
                        lambda *a, **k: calls.append(1) or
                        {"passed": True, "verdict": "通过", "reasons": []})
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    res = gw.verify_flow(_ha_flow_with_dangling_wire(), run_gate=True)
    assert res["ok"] is True
    assert res["verdict"] == "block"
    assert res["passed"] is False
    assert res["fast_fail"] is True
    assert res["fast_fail_stage"] == "static_lint"
    assert any("R17" in r for r in res["fast_fail_reasons"])
    # 核心断言：静态已判死，vhass 重放绝不能触发（秒回省 token）
    assert calls == []


def test_verify_flow_no_fast_fail_on_clean_ha_flow(gw, monkeypatch):
    calls = []
    monkeypatch.setattr(gw, "run_staging_gate",
                        lambda *a, **k: calls.append(1) or
                        {"passed": True, "verdict": "通过", "reasons": []})
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    res = gw.verify_flow(_clean_ha_flow(), run_gate=True)
    assert res.get("fast_fail") is not True
    # 干净流必须跑 vhass 重放（确认未误杀正常验证路径）
    assert calls != []
    assert res["verdict"] in ("pass", "warn", "block")


def test_verify_flow_fast_fail_on_empty_flow_schema(gw, monkeypatch):
    """S5 空 flow（nodes 空数组）→ schema 致命项，也应秒回。"""
    calls = []
    monkeypatch.setattr(gw, "run_staging_gate",
                        lambda *a, **k: calls.append(1) or
                        {"passed": True, "verdict": "通过", "reasons": []})
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    res = gw.verify_flow({"id": "empty", "label": "empty", "nodes": []})
    assert res["verdict"] == "block"
    assert res["fast_fail"] is True
    assert any("schema:" in r for r in res["fast_fail_reasons"])
    assert calls == []
