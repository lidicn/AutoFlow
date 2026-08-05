# -*- coding: utf-8 -*-
"""A5（#171）Bark 安装前置参数单测。

覆盖：
- GET /subflows 的 bark_push 行带 bark_ready 标记；未配置 BARK_SERVER/BARK_KEY 时为 False；
- POST /api/subflows/bark/install 在未配置时返回 400 并提示去连接设置；
- 配置齐（BARK_SERVER/BARK_KEY 有效）后：GET bark_ready=True，安装端点 200 且调用
  ensure_bark_subflow(allow_prod=True)（用内存 fake NR client 验证被调用 + 返回 created）。
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


class FakeNRClients:
    """内存 fake NR client：记录 ensure_bark_subflow 是否真的调了 generate。"""
    def __init__(self):
        self.generated = []
        self.list_calls = 0

    def list_flows(self):
        self.list_calls += 1
        return []  # 无既有 bark 子流程 → 触发生成

    def generate_subflow_from_spec(self, spec, allow_prod=False):
        self.generated.append({"spec_name": spec.get("name"), "allow_prod": allow_prod})
        return {"id": spec.get("id"), "created": True, "exists": False}


@unittest.skipUnless(_HAVE_WEB_DEPS, "A5 测试需要 starlette（缺失则 pip install starlette）。")
class TestBarkInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_bark_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bark_row(self):
        r = self.client.get("/api/subflows").json()
        return next((s for s in r["subflows"] if s["key"] == "bark_push"), None)

    def test_bark_row_has_ready_flag_unconfigured(self):
        # 未配置 → bark_ready False
        row = self._bark_row()
        self.assertIsNotNone(row, "bark_push 应出现在子流程列表")
        self.assertFalse(row["bark_ready"])

    def test_install_rejected_when_not_ready(self):
        r = self.client.post("/api/subflows/bark/install", json={})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])
        self.assertIn("BARK_SERVER", r.json()["error"])

    def test_install_success_when_ready(self):
        # 配置齐：注入 BARK_SERVER/BARK_KEY（任一来源有效即可）
        os.environ["BARK_SERVER"] = "https://bark.example.com"
        os.environ["BARK_KEY"] = "devkey123"
        try:
            # 列表里 bark_ready 应翻 True
            self.assertTrue(self._bark_row()["bark_ready"])
            # 给 gw.nr 注入 fake client（client 是无 setter 的缓存属性：
            # 直接置底层的 _client + 对齐 _client_rev，避免属性重建出真 client）。
            fake = FakeNRClients()
            self.gw.nr._client = fake
            self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)
            r = self.client.post("/api/subflows/bark/install", json={})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertTrue(r.json()["created"])
            # 真调了 ensure_bark_subflow（generate 被调用，且 allow_prod=True）
            self.assertEqual(len(fake.generated), 1)
            self.assertTrue(fake.generated[0]["allow_prod"])
        finally:
            os.environ.pop("BARK_SERVER", None)
            os.environ.pop("BARK_KEY", None)


if __name__ == "__main__":
    unittest.main()
