"""调用级身份闸（F1 / #688）单元测试。

背景：原来 `_DEPLOY_KNIVES`（部署/自检刀）只在 tools/list 隐藏，tools/call 分发层无拦截，
导致 black 身份能直接调 autoflow_set_tab_state 等刀（WB42 AC11 ❌）。
本测试只验证调用级身份闸的纯逻辑（解析 + 判定 + 错误响应），不依赖真实网关/ASGI 栈。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.mcp_server import (  # noqa: E402
    _DEPLOY_KNIVES,
    _blackbox_should_block,
    _peek_tools_call,
    _send_jsonrpc_error,
)


def _evt(body: bytes, more: bool = False):
    return {"type": "http.request", "body": body, "more_body": more}


def _call(name, rid=1):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


# ── _peek_tools_call ──
def test_peek_tools_call_by_name():
    evts = [_evt(json.dumps(_call("autoflow_set_tab_state")).encode())]
    assert _peek_tools_call(evts) == ("autoflow_set_tab_state", 1)


def test_peek_tools_call_by_tool_compat():
    req = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"tool": "autoflow_deploy_raw", "arguments": {}},
    }
    evts = [_evt(json.dumps(req).encode())]
    assert _peek_tools_call(evts) == ("autoflow_deploy_raw", 5)


def test_peek_tools_call_list_is_none():
    evts = [_evt(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())]
    assert _peek_tools_call(evts) == (None, None)


def test_peek_tools_call_empty_body():
    assert _peek_tools_call([_evt(b"")]) == (None, None)


def test_peek_tools_call_chunked():
    payload = json.dumps(_call("autoflow_modify_flow")).encode()
    half = len(payload) // 2
    evts = [_evt(payload[:half], more=True), _evt(payload[half:], more=False)]
    assert _peek_tools_call(evts) == ("autoflow_modify_flow", 1)


def test_peek_tools_call_garbage():
    assert _peek_tools_call([_evt(b"not json at all")]) == (None, None)


# ── _blackbox_should_block ──
def test_block_knife():
    evts = [_evt(json.dumps(_call("autoflow_set_tab_state")).encode())]
    assert _blackbox_should_block(evts) == "autoflow_set_tab_state"


def test_block_normal_tool_passes():
    evts = [_evt(json.dumps(_call("autoflow_list_tabs")).encode())]
    assert _blackbox_should_block(evts) is None


def test_block_non_call_passes():
    evts = [_evt(json.dumps({"method": "tools/list"}).encode())]
    assert _blackbox_should_block(evts) is None


# ── _send_jsonrpc_error ──
def test_send_jsonrpc_error_envelope():
    captured = []

    async def send(msg):
        captured.append(msg)

    asyncio.run(_send_jsonrpc_error(send, 9, -32601, "hidden"))
    assert len(captured) == 2
    start, body = captured
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    assert (b"content-type", b"application/json") in start["headers"]
    assert body["type"] == "http.response.body"
    assert body["more_body"] is False
    parsed = json.loads(body["body"])
    assert parsed["jsonrpc"] == "2.0"
    assert parsed["id"] == 9
    assert parsed["error"]["code"] == -32601
    assert parsed["error"]["message"] == "hidden"
    cl = dict(start["headers"]).get(b"content-length")
    assert cl == str(len(body["body"])).encode()


def test_deploy_knives_covers_set_tab_state():
    # 防御性：确认 F1 的目标工具确实在黑名单里（防止有人误删导致漏洞复活）
    assert "autoflow_set_tab_state" in _DEPLOY_KNIVES
