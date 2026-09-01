"""Test autoflow_apply / autoflow_apply_rollback MCP 工具（WB1-F / #694 F2）。

覆盖：
  - 两个工具都进 _DEPLOY_KNIVES（普通身份 tools/list 不可见）
  - 专家身份调用 → 参数正确透传到 gateway.apply_flow（含 agent_id 由身份注入）
  - 普通身份调用 → 被拒（mode=normal 守卫，与 modify_flow / verify_flow 同构）
  - correction_json 非法 / 非对象 → 明确报错，不打网关
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway import mcp_server as ms


def _expert():
    return type("A", (), {"mode": "expert", "agent_id": "wb1"})()


def _normal():
    return type("A", (), {"mode": "normal", "agent_id": "b"})()


class _FakeGw:
    def __init__(self):
        self.apply_calls = []
        self.rollback_calls = []

    def apply_flow(self, **kw):
        self.apply_calls.append(kw)
        return {"ok": True, "pending": True, "trace_id": "ap_x", "decision_id": "dec-1"}

    def apply_rollback(self, trace_id, agent_id="x", auto_approve=False, allow_prod=False):
        self.rollback_calls.append({"trace_id": trace_id, "agent_id": agent_id,
                                    "auto_approve": auto_approve, "allow_prod": allow_prod})
        return {"ok": True, "restored": auto_approve, "pending": not auto_approve}


def test_apply_tools_in_deploy_knives():
    assert "autoflow_apply" in ms._DEPLOY_KNIVES
    assert "autoflow_apply_rollback" in ms._DEPLOY_KNIVES


def test_apply_expert_passthrough(monkeypatch):
    fake = _FakeGw()
    monkeypatch.setattr(ms, "_gw", lambda: fake)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _expert())
    corr = {"node_patches": [{"match": {"id": "n2"}, "set": {"name": "v2"}}],
            "reason": "回读显示没触发"}
    res = json.loads(ms.autoflow_apply("C", json.dumps(corr), flow_id="f1"))
    assert res["ok"] is True and res["pending"] is True
    call = fake.apply_calls[0]
    assert call["mode"] == "C" and call["flow_id"] == "f1"
    assert call["correction"] == corr
    assert call["agent_id"] == "wb1"          # 由身份注入，不信 agent 自报
    assert call["auto_approve"] is False      # 默认不放行
    assert call["trace_id"] is None


def test_apply_second_phase_passes_trace_and_approve(monkeypatch):
    fake = _FakeGw()
    monkeypatch.setattr(ms, "_gw", lambda: fake)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _expert())
    ms.autoflow_apply("A", '{"dsl":"场景: x"}', flow_id="f1",
                      auto_approve=True, trace_id="ap_keep")
    call = fake.apply_calls[0]
    assert call["auto_approve"] is True and call["trace_id"] == "ap_keep"


def test_apply_normal_rejected(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: _normal())
    res = json.loads(ms.autoflow_apply("C", "{}", flow_id="f1"))
    assert res["ok"] is False and "普通" in res["error"]


def test_apply_bad_json_rejected(monkeypatch):
    fake = _FakeGw()
    monkeypatch.setattr(ms, "_gw", lambda: fake)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _expert())
    res = json.loads(ms.autoflow_apply("C", "{不是json", flow_id="f1"))
    assert res["ok"] is False and "合法 JSON" in res["error"]
    res2 = json.loads(ms.autoflow_apply("C", '["列表不行"]', flow_id="f1"))
    assert res2["ok"] is False and "JSON 对象" in res2["error"]
    assert fake.apply_calls == []             # 参数不合法时绝不打网关


def test_rollback_expert_passthrough(monkeypatch):
    fake = _FakeGw()
    monkeypatch.setattr(ms, "_gw", lambda: fake)
    monkeypatch.setattr(ms, "get_current_agent", lambda: _expert())
    r1 = json.loads(ms.autoflow_apply_rollback("ap_x"))
    assert r1["pending"] is True and r1["restored"] is False
    r2 = json.loads(ms.autoflow_apply_rollback("ap_x", auto_approve=True))
    assert r2["restored"] is True
    assert [c["agent_id"] for c in fake.rollback_calls] == ["wb1", "wb1"]
    # 自愈回滚必须透传 allow_prod=True（prod 护栏 opt-in，与 test_selfheal 同构）
    assert [c["allow_prod"] for c in fake.rollback_calls] == [True, True]


def test_rollback_normal_rejected(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: _normal())
    res = json.loads(ms.autoflow_apply_rollback("ap_x"))
    assert res["ok"] is False and "普通" in res["error"]
