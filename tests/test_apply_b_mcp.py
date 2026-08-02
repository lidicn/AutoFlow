"""Test autoflow_apply_state_from_debug MCP 工具（CB7 / #692 B 段胶水入口）。

参照 tests/test_apply_mcp.py 模式，覆盖：
  - 工具进 _DEPLOY_KNIVES（黑箱 tools/list 不可见）
  - 白箱身份调用 → 参数正确透传到 gateway.apply_state_from_debug（agent_id 由身份注入）
  - 黑箱身份调用 → 被拒（mode=black 守卫）
  - get_current_agent() 为 None → 被拒（无身份码）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway import mcp_server as ms


def _white():
    return type("A", (), {"mode": "white", "agent_id": "wb1"})()


def _black():
    return type("A", (), {"mode": "black", "agent_id": "b"})()


class _FakeGw:
    def __init__(self):
        self.calls = []

    def apply_state_from_debug(self, **kw):
        self.calls.append(kw)
        return {"ok": True, "pending": True, "trace_id": "ap_x"}


def test_b_tool_in_deploy_knives():
    assert "autoflow_apply_state_from_debug" in ms._DEPLOY_KNIVES


def test_b_white_passthrough(monkeypatch):
    fake = _FakeGw()
    monkeypatch.setattr(ms, "_gw", lambda: fake)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _white())
    res = json.loads(ms.autoflow_apply_state_from_debug(
        flow_id="f1", node_id="n0", entity_id="light.study", state="on", reason="回读显示该开"))
    assert res["ok"] is True and res["pending"] is True
    call = fake.calls[0]
    assert call["flow_id"] == "f1" and call["node_id"] == "n0"
    assert call["entity_id"] == "light.study" and call["state"] == "on"
    assert call["reason"] == "回读显示该开"
    assert call["agent_id"] == "wb1"          # 由身份注入，不信 agent 自报
    assert call["auto_approve"] is False      # 默认不放行
    assert call["trace_id"] is None


def test_b_black_rejected(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: _black())
    res = json.loads(ms.autoflow_apply_state_from_debug(flow_id="f1", entity_id="light.x", state="on"))
    assert res["ok"] is False and "黑箱" in res["error"]


def test_b_no_agent_rejected(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: None)
    res = json.loads(ms.autoflow_apply_state_from_debug(flow_id="f1", entity_id="light.x", state="on"))
    assert res["ok"] is False and "未识别" in res["error"]
