#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OAuth 2.0 资源发现端点守卫（v2.0.12-1）。

背景：MCP 客户端拿到 401 后会按 RFC 9728 探测
`/.well-known/oauth-protected-resource`（resource 带路径时为 .../mcp）。
此前网关无此端点 → 刷新 404 噪声。本轮补两个只读匿名端点：

- `/.well-known/oauth-protected-resource[/<resource-path>]`  (RFC 9728)
- `/.well-known/oauth-authorization-server`                   (RFC 8414)

★诚实不变量：AutoFlow 不运行交互式 OAuth AS（身份码由人类在 WebUI 签发）。
因此 AS 元数据**绝不**出现 authorization_endpoint / token_endpoint —— 本测试
把「不虚构端点」也锁死，防止后人「顺手补全」把客户端诱导进注定失败的 OAuth 流程。

运行：pytest tests/test_oauth_discovery.py
"""
import os
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway.mcp_server import build_app
    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:  # 缺 starlette/mcp 时优雅 skip
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = build_app = None

_SCOPES = ["normal", "expert", "admin"]


class _TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_oauth_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(_HAVE_WEB_DEPS,
                     f"需要 starlette+mcp（缺失：{_WEB_DEP_MSG}）")
class TestOAuthDiscoveryEndpoints(_TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = build_webui_asgi(self.cfg)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    # ── RFC 9728 受保护资源元数据 ──
    def test_protected_resource_anonymous_200(self):
        # 发现端点必须匿名可达（无 Authorization 头）
        r = self.client.get("/.well-known/oauth-protected-resource")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["scopes_supported"], _SCOPES)
        self.assertEqual(d["bearer_methods_supported"], ["header"])
        self.assertTrue(d["resource"].endswith(self.cfg.mcp_path), d["resource"])
        self.assertEqual(d["resource_name"], "AutoFlow Gateway")
        # 三面板映射一次声明清楚
        self.assertEqual(d["autoflow_panels"], {
            "normal": self.cfg.mcp_path,
            "expert": self.cfg.mcp_white_path,
            "admin": self.cfg.mcp_admin_path,
        })
        self.assertEqual(d["autoflow_auth_model"], "pre-issued-bearer-token")
        # ★不声明 authorization_servers（无交互式 AS）
        self.assertNotIn("authorization_servers", d)

    def test_protected_resource_path_suffix_variant(self):
        # RFC 9728 §3.1：resource 带路径 → 发现 URL 追加 resource-path
        r = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scopes_supported"], _SCOPES)

    # ── RFC 8414 授权服务器元数据（诚实版）──
    def test_authorization_server_metadata_honest(self):
        r = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("issuer", d)
        self.assertEqual(d["scopes_supported"], _SCOPES)
        self.assertEqual(d["autoflow_authorization"], "none")
        # ★诚实锁：绝不虚构 OAuth 端点
        for forbidden in ("authorization_endpoint", "token_endpoint",
                          "registration_endpoint"):
            self.assertNotIn(forbidden, d, f"不得虚构 {forbidden}")

    def test_wellknown_requires_no_token_even_when_token_mode(self):
        # 即便 WebUI 开了令牌模式，发现端点仍匿名（否则客户端无从发现鉴权）
        saved = {k: os.environ.get(k) for k in ("AF_WEBUI_TOKEN_MODE", "AF_WEBUI_TOKEN")}
        os.environ["AF_WEBUI_TOKEN_MODE"] = "both"
        os.environ["AF_WEBUI_TOKEN"] = "secret-token"
        try:
            app = build_webui_asgi(self.cfg)
            c = TestClient(app)
            c.__enter__()
            try:
                self.assertEqual(
                    c.get("/.well-known/oauth-protected-resource").status_code, 200)
                self.assertEqual(
                    c.get("/.well-known/oauth-authorization-server").status_code, 200)
            finally:
                c.__exit__(None, None, None)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@unittest.skipUnless(_HAVE_WEB_DEPS,
                     f"需要 starlette+mcp（缺失：{_WEB_DEP_MSG}）")
class TestMcpUnauthorizedAdvertisesDiscovery(_TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = build_app(self.cfg, with_webui=True)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_401_carries_www_authenticate_resource_metadata(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        self.assertEqual(r.status_code, 401)
        wa = r.headers.get("www-authenticate", "")
        self.assertIn("resource_metadata=", wa)
        self.assertIn("/.well-known/oauth-protected-resource", wa)


if __name__ == "__main__":
    unittest.main()
