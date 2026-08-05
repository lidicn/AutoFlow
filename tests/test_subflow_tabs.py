# -*- coding: utf-8 -*-
"""A1 (#167) WebUI 双 Tab 拆分后端验证：list_subflows 按 kind 过滤。

- 子流程 Tab（kind=subflow）：history_* ×4 + bark_push 等。
- Link API Tab（kind=link_out / http_api）：彩云/anysearch 等，豆包（self_use）被后端排除。
"""
import unittest

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.identity import AgentStore

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


@unittest.skipUnless(_HAVE_WEB_DEPS, "需要 starlette")
class TestSubflowTabs(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="af_tab_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_rows(self, rows):
        self.gw.tasks.list_subflows = lambda source_type=None, status=None: list(rows)

    def test_list_subflows_excludes_self_use_and_splits_kind(self):
        rows = [
            {"key": "history_state_at", "title": "历史状态", "kind": "subflow",
             "source_type": "managed", "status": "active", "spec_ref": "history_state_at",
             "entry_link_id": None, "nr_subflow_id": "x", "input_schema": [],
             "env_requirements": []},
            {"key": "bark_push", "title": "Bark", "kind": "subflow",
             "source_type": "managed", "status": "active", "spec_ref": "bark_push",
             "entry_link_id": None, "nr_subflow_id": "y", "input_schema": [],
             "env_requirements": ["BARK_SERVER"]},
            {"key": "llm_caiyun_weather", "title": "彩云天气", "kind": "link_out",
             "source_type": "managed", "status": "active", "spec_ref": "llm_caiyun_weather",
             "entry_link_id": "af_weather_in", "nr_subflow_id": None, "input_schema": [],
             "env_requirements": []},
            {"key": "llm_doubao_say", "title": "豆包播报", "kind": "link_out",
             "source_type": "managed", "status": "active", "spec_ref": "llm_doubao_say",
             "entry_link_id": "af_apisay_in", "nr_subflow_id": None, "input_schema": [],
             "env_requirements": []},
        ]
        self._stub_rows(rows)
        r = self.client.get("/api/subflows")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["subflows"]
        keys = [s["key"] for s in data]
        # 子流程 Tab 内容（kind=subflow）
        self.assertIn("history_state_at", keys)
        self.assertIn("bark_push", keys)
        # Link API Tab 内容（link_out，非 self_use）
        self.assertIn("llm_caiyun_weather", keys)
        # 豆包系列 self_use 必须被后端排除（不进任何 Tab）
        self.assertNotIn("llm_doubao_say", keys, "self_use 豆包必须被排除出产品列表")
        # 子流程 Tab 条目数
        subflows = [s for s in data if s["kind"] == "subflow"]
        self.assertEqual(len(subflows), 2)
        # Link API Tab 条目数（仅非 self_use 的 link_out）
        link_apis = [s for s in data if s["kind"] in ("link_out", "http_api")]
        self.assertEqual(len(link_apis), 1)


if __name__ == "__main__":
    unittest.main()
