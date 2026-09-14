# -*- coding: utf-8 -*-
"""A1 (#167) WebUI 双 Tab 拆分后端验证：list_subflows 按 kind 过滤。

- 子流程 Tab（kind=subflow）：history_* ×4 + bark_push 等。
- Link API Tab（kind=link_out / http_api）：彩云/anysearch 等，豆包（self_use）被后端排除。
"""
import os
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

from api_spec_fixture import make_spec, temp_api_spec


@unittest.skipUnless(_HAVE_WEB_DEPS, "需要 starlette")
class TestSubflowTabs(unittest.TestCase):
    def setUp(self):
        import tempfile
        # 账号密码改造后默认 password_only：未认证请求一律 401。
        # 本测试不关心认证，设 token_only 让本机回环放行（与 test_connections_settings /
        # test_subflow_webui 同款配方）。备份原值并在 tearDown 还原，避免污染同会话后续用例。
        self._token_mode_backup = os.environ.get("AF_WEBUI_TOKEN_MODE")
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
        self.tmp = tempfile.mkdtemp(prefix="af_tab_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        # 还原 token_only 环境变量，避免污染同会话后续用例
        if self._token_mode_backup is None:
            os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        else:
            os.environ["AF_WEBUI_TOKEN_MODE"] = self._token_mode_backup
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_rows(self, rows):
        self.gw.tasks.list_subflows = lambda source_type=None, status=None: list(rows)

    def test_list_subflows_excludes_self_use_and_splits_kind(self):
        # 豆包系列已按用户决策移除，用临时 self_use spec 驱动同一排除代码路径。
        spec = make_spec(name="t_say", title="自测播报", kind="link_out",
                         self_use=True, entry_link_id="af_selfuse_in")
        with temp_api_spec(spec):
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
                {"key": "t_say", "title": "自测播报", "kind": "link_out",
                 "source_type": "managed", "status": "active", "spec_ref": "t_say",
                 "entry_link_id": "af_selfuse_in", "nr_subflow_id": None, "input_schema": [],
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
            # self_use 能力必须被后端排除（不进任何 Tab）
            self.assertNotIn("t_say", keys, "self_use 能力必须被排除出产品列表")
            # 子流程 Tab 条目数
            subflows = [s for s in data if s["kind"] == "subflow"]
            self.assertEqual(len(subflows), 2)
            # Link API Tab 条目数（仅非 self_use 的 link_out）
            link_apis = [s for s in data if s["kind"] in ("link_out", "http_api")]
            self.assertEqual(len(link_apis), 1)


if __name__ == "__main__":
    unittest.main()
