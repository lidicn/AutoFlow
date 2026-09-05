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

from api_spec_fixture import make_spec, temp_api_spec


@unittest.skipUnless(_HAVE_WEB_DEPS, "A2 测试需要 starlette（缺失则 pip install starlette）。")
class TestLinkApiConfig(unittest.TestCase):
    def setUp(self):
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
        self.tmp = tempfile.mkdtemp(prefix="af_lacfg_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
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
        # self_use 能力 → 禁止通过此端点配置（403）。
        # 豆包系列已按用户决策移除，用临时 self_use spec 驱动同一代码路径。
        spec = make_spec(name="t_self_use", title="自测自拒绝", kind="link_out",
                         self_use=True, entry_link_id="af_selfuse_in")
        with temp_api_spec(spec):
            r = self.client.put(
                "/api/link-apis/t_self_use/config",
                json={"config": {"FOO": "bar"}},
            )
            self.assertEqual(r.status_code, 403)
            self.assertFalse(r.json()["ok"])

    def test_get_unknown_spec_404(self):
        r = self.client.get("/api/link-apis/does_not_exist/config")
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(_HAVE_WEB_DEPS, "A3 测试需要 starlette。")
class TestExprFieldPlaceholders(unittest.TestCase):
    """A3(#179)：extract / nr_assemble 里的 <ENV> 也要被推导为配置字段。

    这两个字段会被 build_nr_tab_flows 写进 change 节点（api_specs.py 的
    `to: spec.extract` / `to: spec.nr_assemble`），所以里面的占位符不替换
    就会带着裸 <ENV> 进 NR。现网 spec 暂未用到，故用合成 spec 覆盖。
    """

    SPEC_NAME = "t179_expr_probe"

    def setUp(self):
        from autoflow_gateway import api_specs as _as
        self._as = _as
        # 合成 spec：url 无占位符，占位符只藏在 extract / nr_assemble / headers。
        # 若推导只扫 url+body+headers，EXTRACT_KEY / ASSEMBLE_ROOM 就会漏掉。
        self.spec = _as.ApiSpec(
            name=self.SPEC_NAME,
            title="占位符探针",
            kind="link_out",
            url="https://example.invalid/probe",
            entry_link_id="t179_in",
            nr_downstream_link_id="t179_out",
            extract="payload.<EXTRACT_KEY>",
            nr_assemble='{"room": "<ASSEMBLE_ROOM>"}',
            nr_headers={"Authorization": "Bearer <HDR_TOKEN>"},
            nr_tab=True,
        )
        _as.API_SPECS.append(self.spec)

        self.tmp = tempfile.mkdtemp(prefix="af_expr_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self._as.API_SPECS.remove(self.spec)
        except ValueError:
            pass
        os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_and_assemble_placeholders_become_config_fields(self):
        r = self.client.get(f"/api/link-apis/{self.SPEC_NAME}/config")
        self.assertEqual(r.status_code, 200)
        fields = set(r.json()["config_fields"])
        self.assertIn("EXTRACT_KEY", fields, "extract 里的 <ENV> 未被推导为配置字段")
        self.assertIn("ASSEMBLE_ROOM", fields, "nr_assemble 里的 <ENV> 未被推导为配置字段")
        self.assertIn("HDR_TOKEN", fields)  # 原有 headers 覆盖不能回退
        self.assertEqual(fields, {"EXTRACT_KEY", "ASSEMBLE_ROOM", "HDR_TOKEN"})

    def test_declared_expr_fields_are_writable(self):
        """推导出来就必须写得进去，否则用户永远填不上这个字段（越权过滤用同一份清单）。"""
        r = self.client.put(
            f"/api/link-apis/{self.SPEC_NAME}/config",
            json={"config": {"EXTRACT_KEY": "temp_c", "ASSEMBLE_ROOM": "书房"}},
        )
        self.assertEqual(r.status_code, 200)
        g = self.client.get(f"/api/link-apis/{self.SPEC_NAME}/config").json()
        self.assertEqual(g["config"]["EXTRACT_KEY"], "temp_c")
        self.assertEqual(g["config"]["ASSEMBLE_ROOM"], "书房")

    def test_description_notes_not_scanned(self):
        """description/notes 是给人看的文档，扫它会造出幽灵配置字段。"""
        self.spec.description = "示例里会写 <SOME_TOKEN> 这种占位符做说明"
        self.spec.notes = "另见 <ANOTHER_ONE>"
        fields = set(self.client.get(
            f"/api/link-apis/{self.SPEC_NAME}/config").json()["config_fields"])
        self.assertNotIn("SOME_TOKEN", fields)
        self.assertNotIn("ANOTHER_ONE", fields)


if __name__ == "__main__":
    unittest.main()
