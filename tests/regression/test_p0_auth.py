#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C5 — P0 安全护栏回归（REG-C 配套，不计入 NR tab 节点数）。

直接打网关 HTTP 端点，验证三条 P0 护栏：
  1. 无 token 请求 /api/* → 403（本机/回环除外）。
  2. 伪造 X-Forwarded-For 不被采信（公网 Peer 伪造回环仍 403）。
  3. 非 ASCII token 走 hmac.compare_digest 转字节比较，不抛 500（返回干净 403）。

进程内 starlette ASGI scope 驱动，不触真实 NR/HA。
复用 test_webui.py 的鉴权中间件范式；本文件是 REG-C 的 pytest 伴生件。
"""
import os
import sys
import asyncio
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway

try:
    from starlette.testclient import TestClient  # noqa: F401  仅用于探测依赖
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = None


class TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_p0auth_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _raw_status(app, path, client_ip="203.0.113.5", headers=None, query=b""):
    """直接构造 ASGI scope 打中间件，返回 (status, body_bytes)。

    不走 TestClient，便于精确控制 client IP 与 X-Forwarded-For（XFF 伪造测试必需）。
    """
    scope = {
        "type": "http", "method": "GET", "path": path,
        "query_string": query, "headers": headers or [],
        "client": (client_ip, 1234),
    }
    captured = {}

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured["body"] = message.get("body", b"")

    asyncio.run(app(scope, _receive, _send))
    return captured.get("status"), captured.get("body", b"")


@unittest.skipUnless(_HAVE_WEB_DEPS,
                      f"C5 需要 starlette（缺失：{_WEB_DEP_MSG}）；用系统 Python 3.13.2 或 pip install starlette 后运行。")
class TestP0Auth(TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)

    # 1) 无 token → 403（本机/回环除外）
    def test_no_token_remote_403_local_ok(self):
        """P0-3 (S-4)：未配置/未带 token 时，公网 Peer 访问 /api 一律 403；本机放行。"""
        st_remote, _ = _raw_status(self.app, "/api/health", client_ip="203.0.113.5")
        self.assertEqual(st_remote, 403)
        st_local, _ = _raw_status(self.app, "/api/health", client_ip="127.0.0.1")
        self.assertEqual(st_local, 200)
        st_local6, _ = _raw_status(self.app, "/api/health", client_ip="::1")
        self.assertEqual(st_local6, 200)

    # 2) 伪造 XFF 不被采信
    def test_spoofed_xff_not_trusted(self):
        """S-4 反 spoofing：公网 Peer 伪造 XFF: 127.0.0.1 / ::1 仍 403（Peer 非回环，绝不采信 XFF）。

        反例：真反向代理（Peer 回环 + XFF 公网）应放行 —— 验证「只信 Peer，不信 XFF」。
        """
        st_spoof, _ = _raw_status(
            self.app, "/api/health",
            client_ip="203.0.113.7",
            headers=[(b"x-forwarded-for", b"127.0.0.1")],
        )
        self.assertEqual(st_spoof, 403)
        st_spoof6, _ = _raw_status(
            self.app, "/api/health",
            client_ip="198.51.100.9",
            headers=[(b"x-forwarded-for", b"::1")],
        )
        self.assertEqual(st_spoof6, 403)
        # 可信反向代理转发本机客户端：Peer 回环 + XFF 回环 → 放行（采纳 XFF 当真实客户端）
        st_proxy_local, _ = _raw_status(
            self.app, "/api/health",
            client_ip="127.0.0.1",
            headers=[(b"x-forwarded-for", b"127.0.0.1")],
        )
        self.assertEqual(st_proxy_local, 200)

    # 3) 非 ASCII token 不 500
    def test_non_ascii_token_no_500(self):
        """P0-1 (S-1) 加固：非 ASCII Bearer（café）应 403，绝不 500。

        hmac.compare_digest 对 str 要求纯 ASCII，非 ASCII 会抛 TypeError 致 500；
        加固后转字节比较，任意内容安全比较，响应为 403。
        """
        tok = "af_test_secret_token_xyz"
        os.environ["AF_WEBUI_TOKEN"] = tok
        try:
            app = build_webui_asgi(self.cfg, gateway=self.gw)
            st, _ = _raw_status(
                app, "/api/health",
                client_ip="203.0.113.5",
                headers=[(b"authorization", "Bearer café".encode("utf-8"))],
            )
            self.assertEqual(st, 403)
        finally:
            os.environ.pop("AF_WEBUI_TOKEN", None)
