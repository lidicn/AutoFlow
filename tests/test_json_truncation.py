#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TICKET-002 修复验证：gateway.py JSON 截断处理测试。

验收标准：
  - 包含 __truncated__ 标记的 payload 应被跳过，不用于解析 entity_id/state
  - 正常 JSON payload 不受影响
  - 所有帧都被截断时，应返回 would_block_on_schema 风格的错误
"""
import os
import sys
import json
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.debug_bridge import DebugBridge


class _FakeNR:
    """最小 NR 桩。"""
    def list_flows(self):
        return []
    def get_default_server_id(self):
        return ""
    def get_flow(self, fid):
        return None
    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        return {"id": fid, "created": True}


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """构造 Gateway 实例。"""
    monkeypatch.chdir(tmp_path)
    cfg = GatewayConfig(data_dir=str(tmp_path), env="staging")
    gw = Gateway(cfg)
    gw.nr = _FakeNR()
    gw.defense.check_write = lambda **k: None
    return gw


def _make_event(payload):
    """构造 debug 帧。"""
    return {
        "entity_id": "light.test",
        "payload": json.dumps(payload) if isinstance(payload, dict) else payload,
        "received_at": "2026-01-01T00:00:00Z"
    }


class TestJsonTruncation:
    """测试 JSON 截断处理逻辑。"""

    def test_truncated_payload_skipped(self, gw, tmp_path, monkeypatch):
        """包含 __truncated__ 的 payload 应被跳过。"""
        # 构造事件：第一个有效，第二个截断
        events = [
            _make_event({"entity_id": "light.a", "state": "on"}),
            _make_event({"__truncated__": True, "entity_id": "light.b", "state": "off"}),
        ]

        # 模拟 get_debug_read 返回
        monkeypatch.setattr(gw, 'get_debug_read', lambda **k: {"ok": True, "events": events})

        # 调用 apply_state_from_debug（会调用 get_debug_read 并解析截断标记）
        result = gw.apply_state_from_debug(
            flow_id="test-flow",
            entity_id="",  # 不指定，依赖事件解析
            state="",      # 不指定，依赖事件解析
            agent_id="test-agent"
        )

        # 应成功解析出 light.a 的状态
        assert result.get("ok") is True, f"应成功解析有效帧：{result}"

    def test_all_truncated_returns_error(self, gw, tmp_path, monkeypatch):
        """所有帧都被截断时，应返回错误。"""
        events = [
            _make_event({"__truncated__": True, "entity_id": "light.a", "state": "on"}),
            _make_event({"__truncated__": True, "entity_id": "light.b", "state": "off"}),
        ]

        monkeypatch.setattr(gw, 'get_debug_read', lambda **k: {"ok": True, "events": events})

        result = gw.apply_state_from_debug(
            flow_id="test-flow",
            entity_id="",
            state="",
            agent_id="test-agent"
        )

        # 应失败，因为无法解析 entity_id
        assert result.get("ok") is False, f"所有帧截断时应失败：{result}"
        assert "entity_id" in result.get("error", "").lower(), \
            f"错误消息应提及 entity_id：{result.get('error')}"

    def test_valid_json_unchanged(self, gw, tmp_path, monkeypatch):
        """正常 JSON payload 不受截断逻辑影响。"""
        events = [
            _make_event({"entity_id": "light.test", "state": "on", "value": 100}),
        ]

        monkeypatch.setattr(gw, 'get_debug_read', lambda **k: {"ok": True, "events": events})

        result = gw.apply_state_from_debug(
            flow_id="test-flow",
            entity_id="light.test",
            state="on",
            agent_id="test-agent"
        )

        assert result.get("ok") is True, f"正常 JSON 应成功：{result}"

    def test_invalid_json_skipped(self, gw, tmp_path, monkeypatch):
        """非法 JSON（非截断）也应被跳过。"""
        events = [
            {"payload": "not valid json"},  # 字符串，解析失败
            _make_event({"entity_id": "light.ok", "state": "on"}),
        ]

        monkeypatch.setattr(gw, 'get_debug_read', lambda **k: {"ok": True, "events": events})

        result = gw.apply_state_from_debug(
            flow_id="test-flow",
            entity_id="",
            state="",
            agent_id="test-agent"
        )

        # 应跳过非法 JSON，解析出有效的 light.ok
        assert result.get("ok") is True, f"应跳过非法 JSON：{result}"

    def test_explicit_entity_overrides_truncated(self, gw, tmp_path, monkeypatch):
        """显式传入 entity_id 时，不依赖事件解析。"""
        events = [
            _make_event({"__truncated__": True}),
        ]

        monkeypatch.setattr(gw, 'get_debug_read', lambda **k: {"ok": True, "events": events})

        result = gw.apply_state_from_debug(
            flow_id="test-flow",
            entity_id="light.explicit",
            state="on",
            agent_id="test-agent"
        )

        # 应使用显式传入的值，忽略截断事件
        assert result.get("ok") is True, f"显式 entity 应生效：{result}"
