# -*- coding: utf-8 -*-
"""测试 WebUI 验证证据交付卡端点 /api/verify-flow（议题九）。

薄封装：POST 一份 flow_json（或 ref）→ gw.verify_flow → 返回结构化验证证据。
仅验证「端点可用 + 返回体含四类证据字段（gate.layers / verdict / entity_reliability /
lint 概览）」以及「静态快检秒回路径（R17 硬伤 → verdict=block + fast_fail）」。
HA 闸在测试环境无 NR/HA 时会被网关安全 skip，不影响结构断言。
"""
import os
import sys
import tempfile
import unittest
from unittest import skipUnless

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway.gateway import Gateway
    from autoflow_gateway.config import GatewayConfig
    _HAVE_WEB_DEPS = True
    _DEP_MSG = ""
except ImportError as _e:  # pragma: no cover
    _HAVE_WEB_DEPS = False
    _DEP_MSG = str(_e)
    TestClient = build_webui_asgi = Gateway = GatewayConfig = None


@skipUnless(_HAVE_WEB_DEPS, f"WebUI 测试需要 starlette（缺失：{_DEP_MSG}）")
class TestVerifyFlowEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_vfep_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self._mode_backup = os.environ.get("AF_WEBUI_TOKEN_MODE")
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"  # 回环放行，不关心认证
        self.gw = Gateway(self.cfg)
        self.client = TestClient(build_webui_asgi(self.cfg, gateway=self.gw))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if self._mode_backup is None:
            os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        else:
            os.environ["AF_WEBUI_TOKEN_MODE"] = self._mode_backup

    def _clean_flow(self):
        return {"id": "f", "label": "l", "nodes": [
            {"id": "n1", "type": "inject", "z": "t", "wires": [["n2"]]},
            {"id": "n2", "type": "debug", "z": "t", "wires": []},
        ]}

    def _r17_flow(self):  # 悬空连线 → R17 静态硬伤
        return {"id": "f", "label": "l", "nodes": [
            {"id": "n1", "type": "inject", "z": "t", "wires": [["ghost"]]},
            {"id": "n2", "type": "debug", "z": "t", "wires": []},
        ]}

    def test_endpoint_returns_evidence_structure(self):
        # run_gate=False：本测试只验证「证据结构」四件套，不需要真跑 staging 闸（避免测试环境
        # 无 NR/HA 时的 60s 超时/偶发，降低全量套件下的不稳定）。
        r = self.client.post("/api/verify-flow",
                             json={"flow_json": self._clean_flow(), "run_gate": False})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d.get("ok"))
        self.assertIn("verdict", d)
        self.assertIn("gate", d)
        self.assertIn("layers", d["gate"])           # 覆盖项（各闸层）
        self.assertIn("entity_reliability", d)        # 不可靠设备标注
        self.assertIn("lint_error_count", d)           # lint 概览
        # 干净流：verdict 应为 pass（无 HA 动作，闸被 skip 不误伤）
        self.assertEqual(d["verdict"], "pass")

    def test_endpoint_fast_fail_on_static_block(self):
        r = self.client.post("/api/verify-flow", json={"flow_json": self._r17_flow()})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["verdict"], "block")
        self.assertTrue(d.get("fast_fail"))            # 静态快检秒回


if __name__ == "__main__":
    unittest.main()
