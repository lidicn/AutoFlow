"""Test autoflow_verify_flow MCP 工具（WB1-B / #687 B2）。

覆盖：
  - 工具已注册且加入 _DEPLOY_KNIVES（黑箱 tools/list 不可见）
  - 白箱身份调用 → 返回结构化 verdict，deployed=False
  - 黑箱身份调用 → 被拒绝（mode=black 守卫，与 F1 同构）
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway import mcp_server as ms
import autoflow_gateway.gateway as G


def _good_flow():
    return {
        "id": "flow_vf_mcp", "label": "vf-mcp",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "1", "wires": [["n2"]]},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},
        ],
    }


def _white_agent():
    return type("A", (), {"mode": "white", "agent_id": "wb1"})()


def _black_agent():
    return type("A", (), {"mode": "black", "agent_id": "b"})()


def test_verify_flow_in_deploy_knives():
    assert "autoflow_verify_flow" in ms._DEPLOY_KNIVES


def test_verify_flow_white_runs(monkeypatch):
    gw = G.Gateway()
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    monkeypatch.setattr(ms, "_gw", lambda: gw)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _white_agent())
    res = json.loads(ms.autoflow_verify_flow(json.dumps(_good_flow())))
    assert res["ok"] is True
    assert res["deployed"] is False
    assert res["verdict"] == "pass"
    assert res["passed"] is True


def test_verify_flow_black_rejected(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: _black_agent())
    res = json.loads(ms.autoflow_verify_flow(json.dumps(_good_flow())))
    assert res["ok"] is False
    assert "黑箱" in res["error"]
