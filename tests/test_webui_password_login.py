#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebUI 账号密码登录改造 — 集成回归（对应 docs/PLAN_webui_password_login.md 第 7 节）。

直接打网关 HTTP 端点，验证：
  · 会话 Cookie 主通道（登录/放行/登出即时失效）
  · 旧令牌兼容通道（both / token_only）+ 默认 password_only 关闭令牌
  · 首开开放注册（零账号可注册，注册后永久关闭）
  · CSRF 三层防御（Cookie 写请求必须带 X-Requested-With；Bearer 写请求豁免）
  · 失败恒定响应 + 失败锁定（I-5 / I-6）
  · RBAC 多角色（viewer 不可批准，owner 才能用户管理）
  · S-4 不退化（零账号 + 远程 → 403；伪造 XFF 仍 403）
  · 密码哈希不存明文、盐随机、同密不同哈希（I-1）

进程内 starlette TestClient 驱动，不触真实 NR/HA。
"""
import os
import sys
import asyncio
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway import webui_auth as _wa

try:
    from starlette.testclient import TestClient  # noqa: F401
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = None


class TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_pwlogin_")
        self._saved = {}
        for k in ("AF_WEBUI_TOKEN_MODE", "AF_WEBUI_OPEN_REGISTER",
                  "AF_WEBUI_TOKEN", "AF_WEBUI_COOKIE_SECURE"):
            self._saved[k] = os.environ.pop(k, None)
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_client(self, mode="password_only", token=None):
        os.environ["AF_WEBUI_TOKEN_MODE"] = mode
        os.environ["AF_WEBUI_OPEN_REGISTER"] = "1"
        if token is not None:
            os.environ["AF_WEBUI_TOKEN"] = token
        else:
            os.environ.pop("AF_WEBUI_TOKEN", None)
        app = build_webui_asgi(self.cfg)
        return TestClient(app)

    def register_owner(self, client, user="owner", pw="Sup3rSecret!"):
        # 零账号时开放注册（默认 loopback 即可，设计即如此）
        r = client.post("/api/auth/register",
                        json={"username": user, "password": pw, "confirm": pw})
        assert r.status_code == 201, r.text
        return pw


def _raw_status(app, path, client_ip="203.0.113.5", headers=None, method="GET", body=b""):
    """直接构造 ASGI scope 打中间件（绕过 TestClient 的 cookie 状态），用于控制 IP/XFF。"""
    scope = {
        "type": "http", "method": method, "path": path,
        "query_string": b"", "headers": headers or [],
        "client": (client_ip, 1234),
    }
    captured = {}

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def _send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            captured["body"] = message.get("body", b"")

    asyncio.run(app(scope, _receive, _send))
    return captured.get("status"), captured.get("headers", []), captured.get("body", b"")


def _cookie_from(headers):
    for k, v in headers:
        if k.lower() == b"set-cookie":
            return v.decode("latin-1")
    return ""


@unittest.skipUnless(_HAVE_WEB_DEPS,
                      f"需要 starlette（缺失：{_WEB_DEP_MSG}）")
class TestPasswordLogin(TmpCfgMixin, unittest.TestCase):

    def test_register_then_login_session_cookie(self):
        c = self.make_client()
        pw = self.register_owner(c)
        # 注册即登录（含 Set-Cookie）
        # 重新登录拿干净 cookie
        r = c.post("/api/auth/login", json={"username": "owner", "password": pw})
        self.assertEqual(r.status_code, 200)
        sc = r.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", sc, "I-3: 会话 Cookie 必须 HttpOnly")
        self.assertIn("SameSite=Lax", sc, "I-3: 会话 Cookie 必须 SameSite=Lax")
        self.assertIn(_wa.SESSION_COOKIE, sc)
        # 带 cookie 访问受保护资源
        r2 = c.get("/api/pending")
        self.assertEqual(r2.status_code, 200)

    def test_wrong_password_constant_response(self):
        c = self.make_client()
        self.register_owner(c)
        r_none = c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        r_bad = c.post("/api/auth/login", json={"username": "owner", "password": "wrong"})
        self.assertEqual(r_none.status_code, 401)
        self.assertEqual(r_bad.status_code, 401)
        # I-5：两种失败响应体一致，不泄露用户是否存在
        self.assertEqual(r_none.json()["error"], r_bad.json()["error"])

    def test_logout_invalidates_session_server_side(self):
        c = self.make_client()
        pw = self.register_owner(c)
        c.post("/api/auth/login", json={"username": "owner", "password": pw})
        self.assertEqual(c.get("/api/pending").status_code, 200)
        self.assertEqual(c.post("/api/auth/logout").status_code, 200)
        # I-4：登出后旧会话立即失效（服务端删，不只是清前端）
        self.assertEqual(c.get("/api/pending").status_code, 401)

    def test_forgot_session_cookie_rejected(self):
        c = self.make_client()
        self.register_owner(c)
        # 不带 cookie 的远程请求 → 401
        st, _, _ = _raw_status(c.app, "/api/pending", client_ip="203.0.113.9")
        self.assertEqual(st, 401)

    def test_registration_closes_after_first_user(self):
        c = self.make_client()
        self.register_owner(c)
        # 已有一个账号 → 注册窗口关闭
        r = c.post("/api/auth/register",
                   json={"username": "hacker", "password": "Sup3rSecret!", "confirm": "Sup3rSecret!"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("注册已关闭", r.text)

    def test_csrf_blocks_cookie_write_without_header(self):
        c = self.make_client()
        pw = self.register_owner(c)
        c.post("/api/auth/login", json={"username": "owner", "password": pw})
        # 不带 X-Requested-With 的写请求 → 403（I-9）
        r = c.post("/api/auth/change-password",
                   json={"old_password": pw, "new_password": "NewPass#99", "confirm": "NewPass#99"})
        self.assertEqual(r.status_code, 403)
        # 带同源自定义头 → 200
        r2 = c.post("/api/auth/change-password",
                    headers={"X-Requested-With": "autoflow"},
                    json={"old_password": pw, "new_password": "NewPass#99", "confirm": "NewPass#99"})
        self.assertEqual(r2.status_code, 200)

    def test_legacy_token_compat_both_mode(self):
        c = self.make_client(mode="both", token="legacy-shared-token")
        self.register_owner(c)
        # 旧令牌（?token=）在 both 模式仍可用，且不受 CSRF 头约束
        r = c.get("/api/pending?token=legacy-shared-token")
        self.assertEqual(r.status_code, 200)
        # password_only 模式默认拒绝旧令牌（用无会话 Cookie 的新客户端验证）。
        # 注意：所有客户端共享同一 data_dir，owner 已由 c 建好，故 c3 命中
        # 「已初始化 + 回环」分支 → 401（而非 S-4 的 403）。
        c3 = self.make_client(mode="password_only", token="legacy-shared-token")
        self.assertEqual(c3.get("/api/pending?token=legacy-shared-token").status_code, 401)

    def test_token_only_rejects_session(self):
        # token_only 是回滚模式：先用密码模式建好账号，再切到 token_only 客户端
        c0 = self.make_client(mode="password_only", token="legacy-shared-token")
        pw = self.register_owner(c0)
        c = self.make_client(mode="token_only", token="legacy-shared-token")
        # token_only：密码登录不受理（密码子系统已关闭）
        r_sess = c.post("/api/auth/login", json={"username": "owner", "password": pw})
        self.assertEqual(r_sess.status_code, 401)
        # 旧令牌仍受理
        r_tok = c.get("/api/pending?token=legacy-shared-token")
        self.assertEqual(r_tok.status_code, 200)

    def test_failed_login_lockout(self):
        c = self.make_client()
        self.register_owner(c)
        # 连错 5 次 → 第 6 次即使密码对也锁（I-6）
        for _ in range(_wa.MAX_FAILED_USER):
            c.post("/api/auth/login", json={"username": "owner", "password": "wrong"})
        r = c.post("/api/auth/login", json={"username": "owner", "password": "Sup3rSecret!"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("锁定", r.json()["error"])

    def test_rbac_viewer_cannot_approve(self):
        c = self.make_client()
        pw = self.register_owner(c)
        # owner 建一个 viewer
        c.post("/api/auth/login", json={"username": "owner", "password": pw})
        c.post("/api/auth/users", headers={"X-Requested-With": "autoflow"},
               json={"username": "viewer1", "password": "View#2026!", "role": "viewer"})
        c.post("/api/auth/logout")
        # viewer 登录
        cv = self.make_client()
        cv.post("/api/auth/login", json={"username": "viewer1", "password": "View#2026!"})
        r = cv.post("/api/pending/whatever/approve",
                    headers={"X-Requested-With": "autoflow"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("forbidden", r.text)

    def test_s4_zero_users_remote_403_and_xff_spoof(self):
        c = self.make_client()  # 零账号、无令牌
        st, _, _ = _raw_status(c.app, "/api/pending", client_ip="203.0.113.7")
        self.assertEqual(st, 403, "S-4: 零账号 + 远程 → 403")
        # 伪造 XFF 回环仍 403
        st2, _, _ = _raw_status(
            c.app, "/api/pending", client_ip="203.0.113.7",
            headers=[(b"x-forwarded-for", b"127.0.0.1")])
        self.assertEqual(st2, 403, "S-4: 伪造 XFF 不被采信")

    def test_password_hash_not_plaintext_and_salted(self):
        h1 = _wa.hash_password("SamePass123")
        h2 = _wa.hash_password("SamePass123")
        self.assertNotEqual(h1, h2, "同密码应有不同盐 → 哈希不同")
        self.assertNotIn("SamePass123", h1, "I-1: 库里不得含明文密码")
        ok, _ = _wa.verify_password("SamePass123", h1)
        self.assertTrue(ok)
        bad, _ = _verify_bad(h1)
        self.assertFalse(bad)


def _verify_bad(stored):
    return _wa.verify_password("WrongPass!", stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
