# -*- coding: utf-8 -*-
"""A2（#168）Link API 配置表单 + 持久化（方案 B：api_configs 表）单测。

覆盖：
- GET /api/link-apis/{name}/config 返回 spec 推导的 config_fields
  （彩云 = CAIYUN_TOKEN/CAIYUN_LON/CAIYUN_LAT；anysearch = ANYSEARCH_API_KEY）；
- PUT 写入 api_configs 表，往返一致；
- PUT 只接收 spec 声明的字段（越权写无关 env 被丢弃）；
- self_use 能力（豆包系列）PUT 被拒（403）；
- 密钥不回显：GET 返回的 config 值与存储一致（前端用 password 输入框掩码，后端不额外脱敏，
  但绝不返回更多字段）。
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
from autoflow_gateway.api_config_store import ApiConfigStore

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


@unittest.skipUnless(_HAVE_WEB_DEPS, "A2 测试需要 starlette（缺失则 pip install starlette）。")
class TestLinkApiConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_lacfg_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        # 直接读 temp data_dir 的 autoflow.db，与 webui 内 api_configs 同库。
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self):
        return ApiConfigStore(config=SimpleNamespace(data_dir=self.tmp))

    def test_get_caiyun_config_fields(self):
        r = self.client.get("/api/link-apis/llm_caiyun_weather/config")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        # 彩云 url 含三个占位符 → 三个字段
        self.assertEqual(
            set(d["config_fields"]),
            {"CAIYUN_TOKEN", "CAIYUN_LON", "CAIYUN_LAT"},
        )
        # 初始未配置，config 为空
        self.assertEqual(d["config"], {})
        self.assertFalse(d["self_use"])

    def test_get_anysearch_config_fields(self):
        r = self.client.get("/api/link-apis/anysearch_batch/config")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        # anysearch 仅 headers 里的 Bearer <ANYSEARCH_API_KEY> → 一个字段
        self.assertEqual(d["config_fields"], ["ANYSEARCH_API_KEY"])
        self.assertEqual(d["config"], {})

    def test_put_then_get_roundtrip_and_db(self):
        body = {
            "config": {
                "CAIYUN_TOKEN": "Y2FpeXVu...",
                "CAIYUN_LON": "113.869565",
                "CAIYUN_LAT": "22.666851",
            }
        }
        r = self.client.put(
            "/api/link-apis/llm_caiyun_weather/config", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        # GET 回显
        g = self.client.get("/api/link-apis/llm_caiyun_weather/config").json()
        self.assertEqual(g["config"]["CAIYUN_TOKEN"], "Y2FpeXVu...")
        self.assertEqual(g["config"]["CAIYUN_LON"], "113.869565")

        # 直读 DB：确实落在 api_configs 表
        st = self._store()
        try:
            self.assertEqual(
                st.get_api_config("llm_caiyun_weather"),
                body["config"],
            )
        finally:
            st.close()

    def test_put_drops_undeclared_fields(self):
        # 越权：试图写入 spec 未声明的 env（如 PATH / EVIL）
        r = self.client.put(
            "/api/link-apis/llm_caiyun_weather/config",
            json={"config": {"CAIYUN_TOKEN": "x", "EVIL_ENV": "hack"}},
        )
        self.assertEqual(r.status_code, 200)
        st = self._store()
        try:
            saved = st.get_api_config("llm_caiyun_weather")
        finally:
            st.close()
        # EVIL_ENV 被丢弃，只保留声明的字段
        self.assertNotIn("EVIL_ENV", saved)
        self.assertEqual(saved.get("CAIYUN_TOKEN"), "x")

    def test_put_self_use_rejected(self):
        # 豆包系列打了 self_use 标记 → 禁止通过此端点配置（403）
        r = self.client.put(
            "/api/link-apis/llm_doubao_chat/config",
            json={"config": {"FOO": "bar"}},
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()["ok"])

    def test_get_unknown_spec_404(self):
        r = self.client.get("/api/link-apis/does_not_exist/config")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
