#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACP 协议错误码 + 令牌隔离 + 签发 API + 客户端 SSE 解析（工单 DEV-acp-integration 补充覆盖）。

本文件补 test_acp_server.py 未覆盖的验收面：
  §6 错误码  -32700(解析) / -32600(非法请求) / -32601(方法不存在) / -32602(参数非法)
  A7 反向隔离 acp_ 令牌不能进 /mcp（af_ 不能进 /acp 已在 test_acp_server.py 覆盖）
  A11 签发 API /api/acp/tokens：明文仅一次、落库 sha256、可吊销、可真删
  §7 客户端 SSE 行解析；以及「对端鉴权失败返回 HTTP 200+JSON 而非 SSE」不得静默假成功

全部离线：ASGI 直驱 + monkeypatch urlopen，不碰真实 HA/NR/memory-worker。
"""
import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__).replace("\\", "/") + "/../src")
sys.path.insert(0, os.path.dirname(__file__).replace("\\", "/"))

import pytest

from autoflow_gateway import acp_client
from autoflow_gateway.identity import AcpTokenStore
from autoflow_gateway.mcp_server import AgentAuthMiddleware, _ACPApp


# ───────────── 夹具 ─────────────
def _tmp_cfg(**overrides):
    d = tempfile.mkdtemp(prefix="acp_proto_")
    return SimpleNamespace(
        data_dir=d,
        acp_path="/acp",
        memory_worker_acp_url=overrides.get("memory_worker_acp_url", ""),
        memory_worker_acp_token=overrides.get("memory_worker_acp_token", ""),
    )


async def _drive(app, method, path, body=None, headers=None):
    scope = {"type": "http", "method": method, "path": path, "headers": headers or []}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    status, chunks = None, []
    for m in sent:
        if m["type"] == "http.response.start":
            status = m["status"]
        elif m["type"] == "http.response.body":
            chunks.append(m.get("body", b""))
    return status, b"".join(chunks)


def _post_acp(raw_body: bytes):
    """向 _ACPApp 直投一段原始 body（绕过中间件，专测 JSON-RPC 分发层）。"""
    cfg = _tmp_cfg()
    app = _ACPApp(AcpTokenStore(cfg), cfg)
    status, body = asyncio.run(_drive(app, "POST", "/acp", body=raw_body))
    return status, json.loads(body)


# ───────────── §6 错误码 ─────────────
def test_error_32700_parse_error():
    """包体不是合法 JSON → -32700。"""
    status, resp = _post_acp(b"{not json at all")
    assert status == 200
    assert resp["error"]["code"] == -32700


def test_error_32600_invalid_request():
    """合法 JSON 但缺 method 字段 → -32600。"""
    status, resp = _post_acp(json.dumps({"jsonrpc": "2.0", "id": 7}).encode())
    assert status == 200
    assert resp["error"]["code"] == -32600
    assert resp["id"] == 7  # 有 id 时须回显，便于对端配对


def test_error_32600_non_object_payload():
    """顶层是数组（本端不支持批量）→ -32600，且 id 为 null 不得崩。"""
    status, resp = _post_acp(json.dumps([{"jsonrpc": "2.0", "method": "initialize"}]).encode())
    assert status == 200
    assert resp["error"]["code"] == -32600


def test_error_32601_method_not_found():
    status, resp = _post_acp(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "no_such_method"}).encode())
    assert status == 200
    assert resp["error"]["code"] == -32601


def test_error_32601_unknown_session_submethod():
    status, resp = _post_acp(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "session.frobnicate"}).encode())
    assert status == 200
    assert resp["error"]["code"] == -32601


@pytest.mark.parametrize("params,why", [
    ("i am a string", "params 非对象"),
    ({"messages": "should be a list"}, "messages 非数组"),
    ({"sessionId": 12345}, "sessionId 非字符串"),
])
def test_error_32602_invalid_params(params, why):
    """prompt 参数结构非法 → -32602，且必须在开 SSE 流之前拒（不能吐半截流）。"""
    status, resp = _post_acp(json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "prompt", "params": params}).encode())
    assert status == 200, why
    assert resp["error"]["code"] == -32602, why


def test_error_32600_unsupported_http_verb():
    cfg = _tmp_cfg()
    app = _ACPApp(AcpTokenStore(cfg), cfg)
    status, body = asyncio.run(_drive(app, "DELETE", "/acp"))
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32600


# ───────────── A7：acp_ 令牌不能进 /mcp（反向隔离）─────────────
def test_acp_token_rejected_on_mcp_endpoint():
    """acp_ 令牌拿去连 /mcp → 走 af_ 身份码校验，必然 401；不得被 ACP 分支误放行。"""
    cfg = _tmp_cfg()
    acp_store = AcpTokenStore(cfg)
    _rec, code = acp_store.create_token("peer-mw")

    class _DenyAllAgentStore:
        """模拟 AgentStore：任何 acp_ 令牌都不是合法 af_ 身份码。"""
        def resolve_by_code(self, tok):
            assert not tok.startswith("acp_") or True  # 记录：确实被拿来当 af_ 解析
            return None

    reached = {"inner": False}

    async def inner(scope, receive, send):
        reached["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = AgentAuthMiddleware("/mcp", "/mcp-white", "/mcp-admin", _DenyAllAgentStore(),
                             acp_path="/acp", acp_store=acp_store)
    app = mw.wrap(inner)
    status, body = asyncio.run(_drive(
        app, "POST", "/mcp", body=b"{}",
        headers=[(b"authorization", f"Bearer {code}".encode())]))
    assert status == 401, "acp_ 令牌必须被 /mcp 拒绝（三套令牌互不认领）"
    assert reached["inner"] is False, "鉴权失败不得把请求透到内层 app"


def test_acp_endpoint_ignores_af_store():
    """/acp 分支只查 acp_store，绝不落到 AgentStore（否则隔离被打穿）。"""
    cfg = _tmp_cfg()
    acp_store = AcpTokenStore(cfg)
    _rec, code = acp_store.create_token("peer-mw")

    class _BoomAgentStore:
        def resolve_by_code(self, tok):
            raise AssertionError("/acp 不得调用 AgentStore.resolve_by_code")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    mw = AgentAuthMiddleware("/mcp", "/mcp-white", "/mcp-admin", _BoomAgentStore(),
                             acp_path="/acp", acp_store=acp_store)
    app = mw.wrap(inner)
    status, body = asyncio.run(_drive(
        app, "POST", "/acp", body=b"{}",
        headers=[(b"x-acp-token", code.encode())]))
    assert status == 200
    assert json.loads(body).get("ok") is True


# ───────────── A11：令牌落库 sha256 + 明文仅一次 ─────────────
def test_token_stored_as_sha256_never_plaintext():
    cfg = _tmp_cfg()
    store = AcpTokenStore(cfg)
    rec, code = store.create_token("peer-mw", notes="memory-worker 对等")
    db = os.path.join(cfg.data_dir, "autoflow.db")
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT token_id, token_hash FROM acp_tokens").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][1] == hashlib.sha256(code.encode()).hexdigest(), "必须落 sha256"
    # 明文不得出现在库里任何字段
    blob = open(db, "rb").read()
    assert code.encode() not in blob, "令牌明文绝不能落盘"
    # 列表接口不回明文
    listed = store.list_tokens()
    assert "acp_token" not in listed[0] and code not in json.dumps(listed)


def test_token_revoke_then_delete():
    cfg = _tmp_cfg()
    store = AcpTokenStore(cfg)
    rec, code = store.create_token("peer-mw")
    tid = rec["token_id"]
    assert store.resolve_by_token(code) is not None
    assert store.revoke_token(tid) is True
    assert store.resolve_by_token(code) is None, "吊销后必须立即失效"
    assert store.revoke_token("acp_nonexistent") is False
    assert store.delete_token(tid) is True
    assert store.list_tokens() == []
    assert store.delete_token(tid) is False


# ───────────── A11：签发 API（WebUI 面，JWT 保护之下）─────────────
def _webui_app(monkeypatch, tmpdir):
    """构建 WebUI ASGI（禁用 WebUI token → 本机放行，专测 ACP 令牌端点自身）。"""
    monkeypatch.delenv("AF_WEBUI_TOKEN", raising=False)
    from autoflow_gateway import webui as webui_mod
    from autoflow_gateway.config import GatewayConfig

    cfg = GatewayConfig()
    cfg.data_dir = tmpdir
    # 关掉 WebUI 令牌（否则 build 时自动生成并要求携带），只验 ACP 端点逻辑
    monkeypatch.setattr(webui_mod, "_bootstrap_webui_token", lambda c: None)
    monkeypatch.setattr(webui_mod, "_resolve_webui_token", lambda c: None)
    monkeypatch.setattr(webui_mod, "_is_loopback", lambda scope: True)
    return webui_mod.build_webui_asgi(cfg)


def test_acp_token_api_issue_list_revoke(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="acp_api_")
    app = _webui_app(monkeypatch, tmpdir)

    # 签发
    status, body = asyncio.run(_drive(
        app, "POST", "/api/acp/tokens",
        body=json.dumps({"name": "memory-worker", "notes": "peer"}).encode(),
        headers=[(b"content-type", b"application/json")]))
    assert status == 201, body
    data = json.loads(body)
    assert data["ok"] is True
    code = data["token"]["acp_token"]
    assert code.startswith("acp_"), "签发前缀必须是 acp_"
    assert "仅显示一次" in data["warn"]
    tid = data["token"]["token_id"]

    # 列表：不得回明文
    status, body = asyncio.run(_drive(app, "GET", "/api/acp/tokens"))
    assert status == 200
    listed = json.loads(body)["tokens"]
    assert len(listed) == 1 and listed[0]["token_id"] == tid
    assert code not in body.decode(), "列表接口不得回明文令牌"

    # name 必填
    status, body = asyncio.run(_drive(
        app, "POST", "/api/acp/tokens", body=json.dumps({"notes": "x"}).encode(),
        headers=[(b"content-type", b"application/json")]))
    assert status == 400

    # 吊销
    status, body = asyncio.run(_drive(app, "POST", f"/api/acp/tokens/{tid}/revoke"))
    assert status == 200 and json.loads(body)["ok"] is True
    status, body = asyncio.run(_drive(app, "POST", "/api/acp/tokens/acp_ghost/revoke"))
    assert status == 404

    # 真删
    status, body = asyncio.run(_drive(app, "DELETE", f"/api/acp/tokens/{tid}"))
    assert status == 200
    status, body = asyncio.run(_drive(app, "DELETE", f"/api/acp/tokens/{tid}"))
    assert status == 404


# ───────────── §7 客户端：SSE 解析 / 假成功防护 ─────────────
class _FakeResp:
    """最小 urlopen 响应替身：支持 headers.get + 迭代行 + read()。"""

    def __init__(self, payload: bytes, content_type: str):
        self._buf = io.BytesIO(payload)
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._buf.readlines())

    def read(self):
        return self._buf.read()


def _sse(frames):
    out = b""
    for f in frames:
        out += b"event: message\ndata: " + json.dumps(f).encode() + b"\n\n"
    return out


def test_client_parses_sse_and_takes_completed_text(monkeypatch):
    stream = _sse([
        {"jsonrpc": "2.0", "method": "session_update",
         "params": {"sessionId": "s-1", "status": "running", "content": []}},
        {"jsonrpc": "2.0", "method": "session_update",
         "params": {"sessionId": "s-1", "status": "running",
                    "content": [{"type": "tool_call", "name": "recall",
                                 "arguments": {}, "result": "hit"}]}},
        {"jsonrpc": "2.0", "method": "session_update",
         "params": {"sessionId": "s-1", "status": "completed",
                    "content": [{"type": "text", "text": "主卧空调偏好 26 度"}]}},
    ])
    monkeypatch.setattr(acp_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(stream, "text/event-stream"))
    res = acp_client.prompt_acp("http://peer/acp", "acp_x", [{"role": "user", "content": "偏好?"}])
    assert res["ok"] is True
    assert res["status"] == "completed"
    assert res["text"] == "主卧空调偏好 26 度"
    assert res["session_id"] == "s-1"
    assert [b["type"] for b in res["blocks"]] == ["tool_call", "text"]


def test_client_rejects_non_sse_auth_failure(monkeypatch):
    """★对端鉴权失败按规格是 HTTP 200 + application/json 的 -32000。
    客户端若不识别，就会当成空 SSE 流返回 ok=True/text=''（静默假成功）。"""
    payload = json.dumps({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32000, "message": "unauthorized"}}).encode()
    monkeypatch.setattr(acp_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(payload, "application/json"))
    res = acp_client.prompt_acp("http://peer/acp", "acp_bad", [{"role": "user", "content": "hi"}])
    assert res["ok"] is False, "鉴权失败绝不能报成功"
    assert "-32000" in res["error"] or "32000" in res["error"]


def test_client_network_failure_is_friendly(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(acp_client.urllib.request, "urlopen", _boom)
    res = acp_client.prompt_acp("http://peer/acp", "acp_x", [])
    assert res["ok"] is False and "失败" in res["error"]


def test_client_aborted_status_breaks_stream(monkeypatch):
    stream = _sse([
        {"jsonrpc": "2.0", "method": "session_update",
         "params": {"sessionId": "s-2", "status": "aborted",
                    "content": [{"type": "text", "text": "已取消"}]}},
        {"jsonrpc": "2.0", "method": "session_update",
         "params": {"sessionId": "s-2", "status": "running",
                    "content": [{"type": "text", "text": "不该被读到"}]}},
    ])
    monkeypatch.setattr(acp_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(stream, "text/event-stream"))
    res = acp_client.prompt_acp("http://peer/acp", "acp_x", [])
    assert res["status"] == "aborted"
    assert res["text"] == "已取消", "终态帧后必须停止消费"
