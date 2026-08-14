"""T1/T2/T3：llm_client 单测（mock 后端验证 fallback 链 + 脱敏 + chat/stream）。

不启真实 LLM；用 _FakeProvider 替换后端 HTTP，覆盖：
  · A1 chat 返回文本 / stream_chat 逐块 yield
  · A2 单后端 429/5xx/鉴权失败 → 自动 fallback 下一后端；全部失败抛 LLMError
  · A3 ping 探测可用后端；reconfigure 从 env(cfg) 热更新多后端
  · A4 未配置 → configured=False，chat 抛友好 LLMError（不崩）
  · A6 ask_llm 工具：未配置/全失败返回 {ok:False} 友好；成功返回 {ok:True,text}
  · T3 _mask_secret 日志脱敏（密钥不落明文）
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import llm_client
from autoflow_gateway.llm_client import (
    LLMProvider, LLMRouter, LLMError, _mask_secret, normalize_chat_url, _num,
)


class TestHelpers(unittest.TestCase):
    def test_mask_secret(self):
        self.assertEqual(_mask_secret(""), "‹空›")
        self.assertEqual(_mask_secret("short"), "****")
        self.assertEqual(_mask_secret("abcdefghij"), "abcd****ghij")

    def test_normalize_chat_url(self):
        self.assertEqual(normalize_chat_url("https://api.openai.com/v1"),
                         "https://api.openai.com/v1/chat/completions")
        self.assertEqual(normalize_chat_url("https://x/v1/chat/completions"),
                         "https://x/v1/chat/completions")
        self.assertEqual(normalize_chat_url("https://x"), "https://x/chat/completions")

    def test_num(self):
        self.assertEqual(_num("1.5", 0.7), 1.5)
        self.assertEqual(_num("abc", 0.7), 0.7)
        self.assertEqual(_num(None, 4096), 4096)


class _FakeProvider(LLMProvider):
    """替换真实 HTTP：直接按构造参数返回文本或抛 LLMError。"""

    def __init__(self, name, result=None, err=None):
        super().__init__(url="https://x/v1", api_key="key12345678", model="m", name=name)
        self._result = result
        self._err = err

    async def chat(self, messages, **kw):
        if self._err is not None:
            raise self._err
        return self._result

    async def stream_chat(self, messages, **kw):
        if self._err is not None:
            raise self._err
        for piece in (self._result or ""):
            yield piece

    async def ping(self, timeout=15):
        # 不触真实网络：直接返回连通，供 LLMRouter.ping 测试
        return {"connected": True, "name": self.name, "model": self.model, "endpoint": self.url}


class _Cfg:
    pass


class TestReconfigure(unittest.TestCase):
    def test_reconfigure_backends(self):
        cfg = _Cfg()
        cfg.llm_backends = [{"url": "https://a/v1", "api_key": "k12345678", "model": "m1", "name": "A"}]
        cfg.llm_api_key = ""
        cfg.llm_api_url = ""
        cfg.llm_model = ""
        r = LLMRouter(cfg)
        self.assertTrue(r.configured)
        self.assertEqual(len(r._providers), 1)
        self.assertEqual(r._providers[0].name, "A")

    def test_reconfigure_single_fallback(self):
        cfg = _Cfg()
        cfg.llm_backends = []
        cfg.llm_api_key = "k12345678"
        cfg.llm_api_url = "https://b/v1"
        cfg.llm_model = "m2"
        r = LLMRouter(cfg)
        self.assertTrue(r.configured)
        self.assertEqual(len(r._providers), 1)
        self.assertEqual(r._providers[0].model, "m2")

    def test_reconfigure_unconfigured(self):
        cfg = _Cfg()
        cfg.llm_backends = []
        cfg.llm_api_key = ""
        cfg.llm_api_url = ""
        cfg.llm_model = ""
        r = LLMRouter(cfg)
        self.assertFalse(r.configured)


class TestRouterFallback(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_on_429(self):
        p1 = _FakeProvider("a", err=LLMError("后端 a 限流（429）"))
        p2 = _FakeProvider("b", result="ok-from-b")
        r = LLMRouter()
        r._providers = [p1, p2]
        out = await r.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "ok-from-b")

    async def test_fallback_on_5xx(self):
        p1 = _FakeProvider("a", err=LLMError("后端 a 服务端错误（500）"))
        p2 = _FakeProvider("b", result="ok")
        r = LLMRouter()
        r._providers = [p1, p2]
        out = await r.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "ok")

    async def test_all_fail_raises(self):
        p1 = _FakeProvider("a", err=LLMError("429"))
        p2 = _FakeProvider("b", err=LLMError("500"))
        r = LLMRouter()
        r._providers = [p1, p2]
        with self.assertRaises(LLMError):
            await r.chat([{"role": "user", "content": "hi"}])

    async def test_unconfigured_raises(self):
        r = LLMRouter()
        r._providers = []
        with self.assertRaises(LLMError):
            await r.chat([{"role": "user", "content": "hi"}])

    async def test_stream_yields(self):
        p = _FakeProvider("a", result="chunk1chunk2")
        r = LLMRouter()
        r._providers = [p]
        out = []
        async for piece in r.stream_chat([{"role": "user", "content": "hi"}]):
            out.append(piece)
        self.assertEqual("".join(out), "chunk1chunk2")

    async def test_ping(self):
        p = _FakeProvider("a", result="pong")
        r = LLMRouter()
        r._providers = [p]
        res = await r.ping()
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["backends"]), 1)
        self.assertEqual(res["backends"][0]["name"], "a")
        self.assertTrue(res["backends"][0]["connected"])


class TestAskLlmTool(unittest.IsolatedAsyncioTestCase):
    async def test_ask_llm_ok(self):
        from autoflow_gateway import mcp_server as ms
        fake = LLMRouter()
        fake._providers = [_FakeProvider("a", result="hello")]
        with patch.object(llm_client, "get_llm_router", return_value=fake):
            out = json.loads(await ms.autoflow_ask_llm("hi"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "hello")

    async def test_ask_llm_model_override(self):
        from autoflow_gateway import mcp_server as ms
        captured = {}

        async def fake_chat(messages, **kw):
            captured.update(kw)
            return "x"

        fake = LLMRouter()
        fake._providers = [_FakeProvider("a", result="x")]
        fake.chat = fake_chat
        with patch.object(llm_client, "get_llm_router", return_value=fake):
            out = json.loads(await ms.autoflow_ask_llm("hi", model="gpt-4o", system="be terse"))
        self.assertTrue(out["ok"])
        self.assertEqual(captured.get("model"), "gpt-4o")
        self.assertEqual(captured.get("system"), "be terse")

    async def test_ask_llm_unconfigured_friendly(self):
        from autoflow_gateway import mcp_server as ms
        fake = LLMRouter()
        fake._providers = []
        with patch.object(llm_client, "get_llm_router", return_value=fake):
            out = json.loads(await ms.autoflow_ask_llm("hi"))
        self.assertFalse(out["ok"])
        self.assertIn("未配置", out["error"])

    async def test_ask_llm_empty_prompt(self):
        from autoflow_gateway import mcp_server as ms
        out = json.loads(await ms.autoflow_ask_llm(""))
        self.assertFalse(out["ok"])
        self.assertIn("prompt 必填", out["error"])

    async def test_ask_llm_all_fail_friendly(self):
        from autoflow_gateway import mcp_server as ms
        fake = LLMRouter()
        fake._providers = [_FakeProvider("a", err=LLMError("boom"))]
        with patch.object(llm_client, "get_llm_router", return_value=fake):
            out = json.loads(await ms.autoflow_ask_llm("hi"))
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])


if __name__ == "__main__":
    unittest.main()
