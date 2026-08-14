#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEV-llm-webui-agent 测试：

单元（A3）：LLMRouter.chat_with_tools
  · mock OpenAI 返回 tool_calls → 正确解析 + 多轮循环至终态；
  · max_rounds 上限生效（防死循环，强制终态返回）。

WebUI（A1/A2/A5/A7）：
  · GET /api/llm/config 返脱敏（api_key 走 _mask_secret，绝不明文）；
  · PUT /api/llm/config 落盘 data/<env>/llm_config.json，密钥仅存盘不回显；
  · 启用开关免重启：LLM 未启用时 /api/llm/chat 返友好禁用提示（A2）；
  · 工具清单不含 _DEPLOY_KNIVES（部署/自检刀不可见不可调，A5）；
  · 助手走 executor → mcp.call_tool 执行真实用户面工具并回填。
"""
import os
import sys
import json
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import asyncio
import pytest

from autoflow_gateway import llm_client
from autoflow_gateway.llm_client import LLMRouter, LLMError, _mask_secret
from autoflow_gateway.config import GatewayConfig, load_llm_config, llm_config_path
from autoflow_gateway.gateway import Gateway

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway import mcp_server as _mcp_server
    _OK = True
    _ERR = ""
except Exception as _e:  # pragma: no cover
    _OK = False
    _ERR = str(_e)
    TestClient = build_webui_asgi = None

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要 starlette+mcp：{_ERR}")


# ── 单元：chat_with_tools ──
class _FakeProvider:
    configured = True
    enabled = True
    model = "fake"
    name = "fake"

    def __init__(self, responses):
        self._resp = list(responses)
        self.calls = 0

    async def chat_raw(self, messages, **kw):
        self.calls += 1
        return self._resp.pop(0)


def _router_with(responses):
    r = LLMRouter()
    r._providers = [_FakeProvider(responses)]
    return r


def test_chat_with_tools_multi_round():
    """mock OpenAI：第一轮带 tool_calls，第二轮终态文本 → 解析 + 多轮 + 回填。"""
    router = _router_with([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_entity_state", "arguments": '{"entity_id": "light.study"}'}}]},
        {"role": "assistant", "content": "灯是开着的", "tool_calls": []},
    ])
    calls = []

    async def executor(name, args):
        calls.append((name, args))
        return json.dumps({"state": "on"})

    out = asyncio.run(router.chat_with_tools(
        [{"role": "user", "content": "书房灯状态？"}],
        [{"type": "function", "function": {"name": "get_entity_state", "parameters": {}}}],
        executor, max_rounds=6))
    assert out == "灯是开着的"
    assert calls == [("get_entity_state", {"entity_id": "light.study"})]


def test_chat_with_tools_max_rounds_cap():
    """LLM 一直返回 tool_calls（不终态）→ 达 max_rounds 上限强制返回，不死循环。"""
    # 三轮都带 tool_calls；max_rounds=3 → 恰好跑 3 轮后强制终态
    resp = {"role": "assistant", "content": "中间态", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "dummy", "arguments": "{}"}}]}
    router = _router_with([dict(resp) for _ in range(3)])
    calls = []

    async def executor(name, args):
        calls.append(name)
        return "ok"

    out = asyncio.run(router.chat_with_tools(
        [{"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "dummy", "parameters": {}}}],
        executor, max_rounds=3))
    assert router._providers[0].calls == 3  # 正好 3 轮，无第 4 轮
    assert len(calls) == 3
    assert out == "中间态"


def test_chat_with_tools_no_tool_calls_terminal():
    """LLM 首轮即终态（无 tool_calls）→ 直接返回文本，executor 不被调用。"""
    router = _router_with([{"role": "assistant", "content": "你好", "tool_calls": []}])

    async def executor(name, args):
        raise AssertionError("不应执行工具")

    out = asyncio.run(router.chat_with_tools(
        [{"role": "user", "content": "hi"}], [], executor, max_rounds=6))
    assert out == "你好"


def test_chat_with_tools_unconfigured_errors():
    """未配置 → chat_with_tools 抛 LLMError（不崩）。"""
    router = LLMRouter()
    router._providers = []

    async def executor(name, args):
        return ""

    with pytest.raises(LLMError):
        asyncio.run(router.chat_with_tools([{"role": "user", "content": "x"}], [], executor))


# ── WebUI 端点 ──
@pytest.fixture
def env():
    tmp = tempfile.mkdtemp(prefix="af_llm_")
    cfg = GatewayConfig(data_dir=tmp, env="staging")
    gw = Gateway(cfg)
    app = build_webui_asgi(cfg, gateway=gw)
    client = TestClient(app)
    client.__enter__()
    yield client, gw, cfg
    client.__exit__(None, None, None)
    shutil.rmtree(tmp, ignore_errors=True)


def _fake_tools():
    """Fake MCP 工具清单：含一个正常工具 + 一个部署刀（应被排除）。"""
    class _T:
        def __init__(self, name, schema=None):
            self.name = name
            self.description = "desc " + name
            self.inputSchema = schema or {"type": "object", "properties": {}}
    return [_T("autoflow_list_entities"), _T("autoflow_verify_flow")]


def _patch_mcp(monkeypatch, tool_result="[{\"entity_id\":\"light.study\"}]"):
    class _Content:
        def __init__(self, text):
            self.text = text
    calls = []

    async def fake_list_tools():
        return _fake_tools()

    async def fake_call_tool(name, arguments):
        calls.append((name, arguments))
        return [_Content(tool_result)]

    monkeypatch.setattr(_mcp_server.mcp, "list_tools", fake_list_tools)
    monkeypatch.setattr(_mcp_server.mcp, "call_tool", fake_call_tool)
    return calls


def test_llm_config_get_masks_key(env):
    client, gw, cfg = env
    # 先写一份含明文密钥的配置
    cfg_path = llm_config_path(cfg)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"enabled": True, "api_url": "https://x/v1", "api_key": "sk-abcdefghij1234",
                   "model": "gpt-4o-mini", "backends": []}, f)
    r = client.get("/api/llm/config")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True
    assert d["api_key"] != "sk-abcdefghij1234"        # 绝不回显明文
    assert "****" in d["api_key"]                       # 走 _mask_secret 脱敏
    assert d["api_url"] == "https://x/v1"
    assert d["model"] == "gpt-4o-mini"


def test_llm_config_put_persists(env):
    client, gw, cfg = env
    r = client.put("/api/llm/config", json={
        "enabled": True, "api_url": "https://x/v1",
        "api_key": "sk-SECRETPLAINTEXT", "model": "gpt-4o-mini", "backends": []})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 落盘
    saved = load_llm_config(cfg)
    assert saved["enabled"] is True
    assert saved["api_key"] == "sk-SECRETPLAINTEXT"     # 仅存盘，GET 不返明文
    assert saved["model"] == "gpt-4o-mini"
    # GET 回显脱敏
    g = client.get("/api/llm/config").json()
    assert "SECRETPLAINTEXT" not in g["api_key"]


def test_llm_config_put_preserves_key_when_blank(env):
    """PUT 时省略 api_key 字段 → 保留文件中原密钥（防脱敏回写误清空）。"""
    client, gw, cfg = env
    client.put("/api/llm/config", json={"enabled": True, "api_url": "https://x/v1",
                                        "api_key": "sk-ORIGINAL", "model": "m", "backends": []})
    # 再次保存：省略 api_key（不传该字段）→ 应保留原密钥
    r = client.put("/api/llm/config", json={"enabled": True, "api_url": "https://x/v1",
                                            "model": "m2", "backends": []})
    assert r.status_code == 200
    assert load_llm_config(cfg)["api_key"] == "sk-ORIGINAL"
    assert load_llm_config(cfg)["model"] == "m2"


def test_llm_config_put_clears_key_when_empty(env):
    """PUT 时显式传空串 api_key → 清空密钥（#llm-ui-pool-test 修正的「无法清空」bug）。"""
    client, gw, cfg = env
    client.put("/api/llm/config", json={"enabled": True, "api_url": "https://x/v1",
                                        "api_key": "sk-ORIGINAL", "model": "m", "backends": []})
    r = client.put("/api/llm/config", json={"enabled": True, "api_url": "https://x/v1",
                                            "api_key": "", "model": "m2", "backends": []})
    assert r.status_code == 200
    assert load_llm_config(cfg)["api_key"] == ""   # 显式空串 → 清空
    assert load_llm_config(cfg)["model"] == "m2"


def test_llm_chat_disabled(env):
    """LLM 未启用（enabled=False）→ /api/llm/chat 返友好禁用提示（A2）。"""
    client, gw, cfg = env
    client.put("/api/llm/config", json={"enabled": False, "api_url": "https://x/v1",
                                        "api_key": "sk-x", "model": "m", "backends": []})
    r = client.post("/api/llm/chat", json={"message": "书房灯状态？"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json().get("hint") == "llm_disabled"


def test_llm_chat_tools_exclude_deploy_knives(env, monkeypatch):
    """助手工具清单不含 _DEPLOY_KNIVES；且经 executor 调用真实用户面工具（A5）。"""
    client, gw, cfg = env
    calls = _patch_mcp(monkeypatch)
    client.put("/api/llm/config", json={"enabled": True, "api_url": "https://x/v1",
                                        "api_key": "sk-x", "model": "m", "backends": []})

    captured = {}

    class _FakeRouter:
        configured = True

        async def chat_with_tools(self, messages, tools, executor, *, model=None,
                                  system=None, max_rounds=6, **kw):
            names = [t["function"]["name"] for t in tools]
            # A5：部署/自检刀不可见
            assert "autoflow_verify_flow" not in names
            assert "autoflow_list_entities" in names
            # 经 executor 调真实用户面工具（内部走 mcp.call_tool）
            res = await executor("autoflow_list_entities", {"area": "书房"})
            captured["tools"] = names
            captured["exec_result"] = res
            return "书房有实体"

    monkeypatch.setattr(llm_client.LLMRouter, "from_dict", staticmethod(lambda d: _FakeRouter()))
    r = client.post("/api/llm/chat", json={"message": "书房有哪些设备？"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["text"] == "书房有实体"
    assert "autoflow_verify_flow" not in captured["tools"]
    # executor 真调了 mcp.call_tool（用户面工具）
    assert calls and calls[0][0] == "autoflow_list_entities"
