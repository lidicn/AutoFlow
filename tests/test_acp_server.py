#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACP（Agent Client Protocol）服务端 + acp_ 令牌体系单测（工单 DEV-acp-integration）。

覆盖验收点：
  A1 GET /acp 服务说明（非 JSON-RPC）
  A2 initialize 返回 agent 元信息 + capabilities + tools
  A3 prompt SSE 流末 session_update.status=completed 且 content 含 text；无额外 result
  A4 cancel 按 sessionId 中止、下发 aborted
  A5 session.new/list/history/delete
  A6 缺/非法/非 acp 令牌 → HTTP 200 + JSON-RPC error.code=-32000
  A7 acp_ 与 af_(MCP)/WebUI 令牌隔离（acp_ 不能进 /mcp 由既有中间件保证；此处验 af_ 不能进 /acp）
  A8 delegate_to_memory_worker 未配置 → 友好提示、不报错
  A11 acp_ 令牌签发→sha256 落库→校验→吊销失效
  A9 双向联调（真实 memory-worker）留人工，本文件 skip。

测试不依赖真实 MCP/HA/NR：直接驱动 ASGI 端点 + 单测内部函数。
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__).replace("\\", "/") + "/../src")
sys.path.insert(0, os.path.dirname(__file__).replace("\\", "/"))

import pytest

from autoflow_gateway.acp_client import delegate_to_memory_worker
from autoflow_gateway.identity import AcpTokenStore
from autoflow_gateway.mcp_server import (
    AgentAuthMiddleware,
    _ACPApp,
    _ACPSession,
    _ACP_SESSIONS,
    _ACP_SESSIONS_LOCK,
    _acp_agent_run,
    _acp_extract_entity_id,
    _acp_get_or_create_session,
    _acp_handle_cancel,
    _acp_handle_session,
    _acp_new_session_id,
)


# ───────────── 测试夹具 ─────────────
def _tmp_cfg(**overrides):
    """造一个最小 config（仅 ACP 相关字段 + data_dir）。"""
    d = tempfile.mkdtemp(prefix="acp_test_")
    cfg = SimpleNamespace(
        data_dir=d,
        acp_path="/acp",
        memory_worker_acp_url=overrides.get("memory_worker_acp_url", ""),
        memory_worker_acp_token=overrides.get("memory_worker_acp_token", ""),
    )
    return cfg


def _fake_acp_store():
    return AcpTokenStore(_tmp_cfg())


def _clear_sessions():
    with _ACP_SESSIONS_LOCK:
        _ACP_SESSIONS.clear()


# ───────────── ASGI 驱动辅助 ─────────────
async def _drive(app, method, path, body=None, headers=None):
    scope = {"type": "http", "method": method, "path": path, "headers": headers or []}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    status, hdrs, chunks = None, [], []
    for m in sent:
        if m["type"] == "http.response.start":
            status = m["status"]
            hdrs = m.get("headers", [])
        elif m["type"] == "http.response.body":
            chunks.append(m.get("body", b""))
    return status, hdrs, b"".join(chunks)


def _parse_sse(body: bytes):
    """解析 SSE 帧（event: message\\ndata: <json>\\n\\n）。返回 data 解析后的 dict 列表。"""
    frames = []
    for raw in body.split(b"\n\n"):
        if not raw.strip():
            continue
        data_line = None
        for line in raw.split(b"\n"):
            line = line.decode("utf-8", "replace")
            if line.startswith("data:"):
                data_line = line.split(":", 1)[1].strip()
        if data_line:
            try:
                frames.append(json.loads(data_line))
            except Exception:
                pass
    return frames


# ───────────── A1：GET 服务说明 ─────────────
def test_acp_get_service_desc():
    app = _ACPApp(_fake_acp_store(), _tmp_cfg())
    status, _h, body = asyncio.run(_drive(app, "GET", "/acp"))
    assert status == 200
    data = json.loads(body)
    assert "Agent Client Protocol" in data["protocol"]
    assert "initialize" in data["methods"]
    assert "prompt" in data["methods"]


# ───────────── A2：initialize ─────────────
def test_acp_initialize():
    app = _ACPApp(_fake_acp_store(), _tmp_cfg())
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    status, _h, body = asyncio.run(_drive(app, "POST", "/acp", json.dumps(req).encode()))
    assert status == 200
    data = json.loads(body)
    assert data["result"]["agent"]["name"] == "AutoFlow Gateway"
    assert data["result"]["capabilities"]["streaming"] is True
    assert data["result"]["capabilities"]["sessions"] is True
    names = [t["name"] for t in data["result"]["tools"]]
    assert "delegate_to_memory_worker" in names
    assert "list_entities" in names


# ───────────── A3：prompt SSE（delegate 意图，不依赖网关）─────────────
def test_acp_prompt_sse_completed():
    _clear_sessions()
    app = _ACPApp(_fake_acp_store(), _tmp_cfg())
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            "autoflow_gateway.acp_client.delegate_to_memory_worker",
            lambda task, context=None, cfg=None: {
                "ok": True, "session_id": "s1", "status": "completed",
                "text": "家庭记忆：书房灯常亮", "blocks": [],
            },
        )
        req = {"jsonrpc": "2.0", "id": 2, "method": "prompt",
               "params": {"messages": [{"role": "user", "content": "帮我查一下家庭记忆"}]}}
        status, _h, body = asyncio.run(_drive(app, "POST", "/acp", json.dumps(req).encode()))
    assert status == 200
    frames = _parse_sse(body)
    statuses = [f["params"]["status"] for f in frames]
    assert "running" in statuses
    assert statuses[-1] == "completed"
    last = frames[-1]
    assert any(b.get("type") == "text" for b in last["params"]["content"])
    # 流末 completed 即结束，不应再单独发 JSON-RPC result（响应体整体是 SSE，无 result 字段）
    assert all("result" not in f for f in frames if f.get("method") == "session_update")


# ───────────── A3：prompt 确定性分发到 list_entities（桩网关函数）─────────────
def test_acp_prompt_list_entities_dispatch():
    _clear_sessions()
    app = _ACPApp(_fake_acp_store(), _tmp_cfg())
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("autoflow_gateway.mcp_server.autoflow_list_entities",
                   lambda *a, **k: json.dumps({"ok": True, "entities": [{"entity_id": "light.x"}]}))
        req = {"jsonrpc": "2.0", "id": 3, "method": "prompt",
               "params": {"messages": [{"role": "user", "content": "列出书房有哪些灯"}]}}
        _status, _h, body = asyncio.run(_drive(app, "POST", "/acp", json.dumps(req).encode()))
    frames = _parse_sse(body)
    assert frames[-1]["params"]["status"] == "completed"
    # 应出现 tool_call(list_entities) + 至少一条 text
    types = [(b.get("type"), b.get("name")) for b in frames[-1]["params"]["content"]]
    assert ("tool_call", "list_entities") in types


# ───────────── A4：cancel 中止 → aborted ─────────────
def test_acp_cancel_aborts():
    _clear_sessions()
    app = _ACPApp(_fake_acp_store(), _tmp_cfg())
    sid = _acp_new_session_id()
    _acp_get_or_create_session(sid)

    async def run():
        sent = []

        async def send(msg):
            sent.append(msg)

        req = {"jsonrpc": "2.0", "id": 2, "method": "prompt",
               "params": {"sessionId": sid,
                          "messages": [{"role": "user", "content": "慢慢想"}],
                          "context": {"_acp_slow_seconds": 2}}}
        task = asyncio.create_task(app._handle_prompt(req, send))
        await asyncio.sleep(0.4)  # 让 prompt 进入运行态
        res = _acp_handle_cancel({"jsonrpc": "2.0", "id": 1, "method": "cancel",
                                  "params": {"sessionId": sid}})
        assert res["result"]["cancelled"] is True
        await task
        chunks = [m["body"] for m in sent if m["type"] == "http.response.body"]
        return _parse_sse(b"".join(chunks))

    frames = asyncio.run(run())
    assert frames[-1]["params"]["status"] == "aborted"


def test_acp_cancel_unknown_session():
    _clear_sessions()
    res = _acp_handle_cancel({"jsonrpc": "2.0", "id": 1, "method": "cancel",
                              "params": {"sessionId": "nope"}})
    assert "error" in res and res["error"]["code"] == -32001


# ───────────── A5：session.new/list/history/delete ─────────────
def test_acp_sessions_crud():
    _clear_sessions()
    r = _acp_handle_session("session.new", {"id": 1, "params": {}})
    sid = r["result"]["sessionId"]
    assert sid
    lst = _acp_handle_session("session.list", {"id": 2, "params": {}})
    assert any(s["sessionId"] == sid for s in lst["result"]["sessions"])
    hist = _acp_handle_session("session.history", {"id": 3, "params": {"sessionId": sid}})
    assert hist["result"]["sessionId"] == sid
    d = _acp_handle_session("session.delete", {"id": 4, "params": {"sessionId": sid}})
    assert d["result"]["deleted"] is True
    miss = _acp_handle_session("session.history", {"id": 5, "params": {"sessionId": sid}})
    assert "error" in miss


# ───────────── A6：鉴权失败 → 200 + -32000 ─────────────
class _CaptureApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


def test_acp_auth_missing_token():
    store = AcpTokenStore(_tmp_cfg())  # 占位（/acp 不用 af_ store）
    acp_store = _fake_acp_store()
    mw = AgentAuthMiddleware("/mcp", "/mcp-white", "/mcp-admin", store,
                             acp_path="/acp", acp_store=acp_store)
    downstream = _CaptureApp()
    wrapped = mw.wrap(downstream)
    status, _h, body = asyncio.run(_drive(wrapped, "POST", "/acp", b'{"method":"initialize"}', headers=[]))
    assert status == 200
    data = json.loads(body)
    assert data["error"]["code"] == -32000
    assert downstream.called is False


def test_acp_auth_non_acp_token_rejected():
    """af_ 令牌（MCP 身份码）不能进 /acp → 仍 -32000（A7 隔离）。"""
    store = AcpTokenStore(_tmp_cfg())
    acp_store = _fake_acp_store()
    mw = AgentAuthMiddleware("/mcp", "/mcp-white", "/mcp-admin", store,
                             acp_path="/acp", acp_store=acp_store)
    downstream = _CaptureApp()
    wrapped = mw.wrap(downstream)
    status, _h, body = asyncio.run(_drive(
        wrapped, "POST", "/acp", b'{"method":"initialize"}',
        headers=[(b"authorization", b"Bearer af_bogus_identity")]))
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32000
    assert downstream.called is False


def test_acp_auth_valid_token_forwards():
    store = AcpTokenStore(_tmp_cfg())
    acp_store = _fake_acp_store()
    rec, code = acp_store.create_token("memory-worker-1")
    mw = AgentAuthMiddleware("/mcp", "/mcp-white", "/mcp-admin", store,
                             acp_path="/acp", acp_store=acp_store)
    downstream = _CaptureApp()
    wrapped = mw.wrap(downstream)
    status, _h, body = asyncio.run(_drive(
        wrapped, "POST", "/acp", b'{"method":"initialize"}',
        headers=[(b"authorization", f"Bearer {code}".encode())]))
    assert downstream.called is True  # 合法 acp_ 令牌放行到下游 _ACPApp


# ───────────── A7 / A11：acp_ 令牌生命周期 + 隔离 ─────────────
def test_acp_token_store_lifecycle_and_isolation():
    s = _fake_acp_store()
    rec, code = s.create_token("mw")
    assert code.startswith("acp_")
    # 校验通过
    assert s.resolve_by_token(code)["token_id"] == rec["token_id"]
    # 吊销后失效
    assert s.revoke_token(rec["token_id"]) is True
    assert s.resolve_by_token(code) is None
    # 非 acp 前缀（含 MCP 的 af_）一律拒
    assert s.resolve_by_token("af_something") is None
    assert s.resolve_by_token("") is None
    assert s.resolve_by_token("Bearer acp_x") is None  # 不应带 Bearer 前缀


# ───────────── A8：delegate 未配置友好提示 ─────────────
def test_delegate_unconfigured_friendly():
    cfg = _tmp_cfg(memory_worker_acp_url="", memory_worker_acp_token="")
    res = delegate_to_memory_worker("查一下家庭记忆", cfg=cfg)
    assert res["ok"] is False
    assert "MEMORY_WORKER_ACP_URL" in res["error"]
    assert "MEMORY_WORKER_ACP_TOKEN" in res["error"]


# ───────────── A9：双向联调（需真实 memory-worker，留人工）─────────────
@pytest.mark.skip(reason="A9 需 NAS 上真实 memory-worker 实例，留人工双向 delegate 联调（同 REG-S 纪律）")
def test_acp_bidirectional_real():
    raise AssertionError("需真实 memory-worker")


if __name__ == "__main__":
    import os as _os
    _os.chdir(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    sys.exit(pytest.main([__file__, "-v"]))
