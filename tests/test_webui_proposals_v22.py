"""v2.2.0 前端对接端点单测：/api/proposals/{id}/summary 与 /api/proposals/batch_deploy。

离线（进程内 starlette TestClient，不触真实 NR/HA）。复用 test_webui.py 的
「both 模式 + 固定令牌」通道：解析为 owner，覆盖全部 RBAC；data_dir 用临时目录隔离真实库。
"""
import json
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
        self.tmp = tempfile.mkdtemp(prefix="af_wv22_")
        self.cfg = _cfgmod.GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestProposalsV22Endpoints(TmpCfgMixin, unittest.TestCase):
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

    def _submit_dsl(self):
        content = json.dumps({
            "type": "dsl",
            "dsl": "automation:\n  trigger: time 22:00\n  action: light.living_room on=false",
            "node_count": 2,
        })
        r = self.client.post("/api/proposals", json={
            "agent_id": "human", "title": "测试关灯", "spec": "晚上关客厅灯", "content": content})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["proposal"]["id"]

    def test_summary_endpoint_three_lines(self):
        pid = self._submit_dsl()
        r = self.client.post(f"/api/proposals/{pid}/summary")
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j["ok"], j)
        s = j["summary"]
        self.assertEqual(len(s["plain"]), 3, s)
        self.assertIn("意图", s["plain"][0])
        self.assertIn("验证", s["plain"][1])
        self.assertIn("影响设备", s["plain"][2])

    def test_summary_endpoint_missing_pid_404(self):
        r = self.client.post("/api/proposals/nope/summary")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertFalse(r.json()["ok"])

    def test_batch_deploy_empty_ids_400(self):
        r = self.client.post("/api/proposals/batch_deploy", json={"ids": []})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("ids", r.json()["error"])

    def test_batch_deploy_calls_gateway(self):
        captured = {}

        def fake(ids, agent_id="human", target="prod", force=False, validate=True,
                 allow_prod=True, require_e2e=None, dry_run=False):
            captured["ids"] = list(ids)
            return {"ok": True, "deployed": [{"id": i} for i in ids],
                    "failed": [], "rolled_back": False}

        self.gw.deploy_proposals = fake
        r = self.client.post("/api/proposals/batch_deploy", json={
            "ids": ["a", "b"], "target": "prod", "validate": True, "allow_prod": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(captured["ids"], ["a", "b"])
        self.assertEqual(len(r.json()["deployed"]), 2)
