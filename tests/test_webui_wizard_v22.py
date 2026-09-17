"""v2.2.0 #10 Pro 引导式 DSL 向导端点单测：/api/wizard/resolve 与 /api/wizard/propose。

离线（进程内 starlette TestClient，不触真实 NR/HA）。复用 test_webui.py 的
「both 模式 + 固定令牌」通道：解析为 owner，覆盖全部 RBAC；data_dir 用临时目录隔离真实库。
完整 propose_dsl 链路（解析→编译→verify_flow 闸→建提案）由 test_gateway.py 覆盖，
本文件只验证 WebUI 端点的路由接线 + 入参校验 + 结构化错误透传。
"""
import os
import shutil
import tempfile
import unittest

from autoflow_gateway import config as _cfgmod
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.webui import build_webui_asgi

try:
    from starlette.testclient import TestClient
    _HAVE = True
except Exception:
    _HAVE = False


@unittest.skipUnless(_HAVE, "WebUI 测试需要 starlette（系统 Python 3.13.2 或 pip install starlette 后运行）。")
class TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_wz22_")
        self.cfg = _cfgmod.GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestWizardEndpointsV22(TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._wa = {k: os.environ.get(k) for k in
                    ("AF_WEBUI_TOKEN_MODE", "AF_WEBUI_TOKEN", "AF_WEBUI_OPEN_REGISTER")}
        os.environ["AF_WEBUI_TOKEN_MODE"] = "both"
        os.environ["AF_WEBUI_TOKEN"] = "test-webui-shared-token"
        os.environ["AF_WEBUI_OPEN_REGISTER"] = "1"
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.headers["Authorization"] = "Bearer test-webui-shared-token"
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()
        for k, v in self._wa.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_wizard_resolve_route_wired(self):
        # 离线 catalog 为空 → resolve_entity 返回 ok=False（"device_catalog 为空"），
        # 端点应把该结构化结果原样透传（验证路由接线 + 调 gw.resolve_entity）。
        r = self.client.get("/api/wizard/resolve?name=显示器灯&top_n=8")
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertIn("ok", j)
        self.assertFalse(j["ok"])  # 空 catalog，符合预期

    def test_wizard_propose_empty_title_400(self):
        r = self.client.post("/api/wizard/propose", json={"dsl": "场景: x\n触发: a on\n动作: light.turn_on(b)"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("title", r.json()["error"])

    def test_wizard_propose_invalid_dsl_422(self):
        # 非法 DSL 经 propose_dsl → stage=compile、ok=False；端点应透传为 422。
        r = self.client.post("/api/wizard/propose",
                              json={"title": "测试", "dsl": "这不是合法 DSL @@@"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(r.json().get("stage"), "compile")
